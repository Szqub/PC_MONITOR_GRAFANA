"""
PresentMon Service API FPS provider.

Uses the official PresentMon Service API as the primary production FPS backend.
It connects to the already running PresentMon service via PresentMonAPI2.dll and
polls live telemetry for the selected target process.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import psutil

from bytetech_agent.models.metrics import MetricData, ProviderContext, ProviderStatus
from bytetech_agent.providers.base import BaseProvider
from bytetech_agent.providers.presentmon_provider import (
    PresentMonFrameSample,
    PresentMonTarget,
    RollingProcessStats,
)
from bytetech_agent.providers.presentmon_service_api import (
    PresentMonApiError,
    PresentMonServiceApiClient,
    validate_presentmon_installation,
)

try:
    import win32gui
    import win32process
except ImportError:  # pragma: no cover
    win32gui = None
    win32process = None

logger = logging.getLogger(__name__)

BACKEND_NAME = "presentmon_service_api"
NON_GAME_FOREGROUND_PROCESSES = {
    "powershell.exe",
    "pwsh.exe",
    "cmd.exe",
    "windowsterminal.exe",
    "explorer.exe",
    "conhost.exe",
}
MAX_SAMPLE_AGE_SECONDS = 30.0


class PresentMonServiceProvider(BaseProvider):
    def __init__(self, fps_config, presentmon_config, presentmon_service_config):
        super().__init__(name="PresentMonService")
        self._fps_config = fps_config
        self._presentmon_config = presentmon_config
        self._service_config = presentmon_service_config
        self._client: Optional[PresentMonServiceApiClient] = None
        self._active_target: Optional[PresentMonTarget] = None
        self._stats_by_pid: Dict[int, RollingProcessStats] = {}
        self._last_error: Optional[str] = None

    def initialize(self) -> bool:
        if not getattr(self._service_config, "enabled", True):
            self._health.mark_unavailable("presentmon_service.enabled is false")
            logger.info("PresentMon Service API backend disabled in config.")
            return False

        validation = validate_presentmon_installation(self._service_config)
        if not validation["ok"]:
            reason = "; ".join(validation["errors"])
            self._health.mark_unavailable(reason)
            logger.warning("PresentMon Service API unavailable: %s", reason)
            return False

        self._client = PresentMonServiceApiClient(self._service_config)
        try:
            self._client.open()
        except PresentMonApiError as exc:
            self._health.mark_unavailable(str(exc))
            logger.warning("PresentMon Service API initialization failed: %s", exc)
            self._client = None
            return False

        self._health.capabilities = {
            "fps_now": True,
            "frametime_ms_now": True,
            "fps_avg_10s": True,
            "fps_avg_30s": True,
            "fps_1pct_30s": True,
            "fps_0_1pct_30s": True,
            "cpu_busy_ms": True,
            "gpu_busy_ms": True,
            "display_latency_ms": True,
            "present_mode_name": False,
        }
        self._health.status = ProviderStatus.AVAILABLE
        logger.info(
            "PresentMon Service API ready. dll=%s api_version=%s",
            self._client.paths.chosen_dll,
            self._client.get_api_version_string() or "unknown",
        )
        return True

    def _collect(self, context: ProviderContext) -> List[MetricData]:
        if not self._client:
            return []

        target = self._resolve_target()
        if target is None:
            return [self._build_metric(context, None)]

        try:
            snapshot = self._client.poll_process(target.pid)
            self._last_error = None
        except PresentMonApiError as exc:
            self._last_error = str(exc)
            logger.debug(
                "PresentMon Service API poll failed. target_pid=%s target_process=%s error=%s",
                target.pid,
                target.process_name,
                exc,
            )
            return []

        now = time.monotonic()
        self._active_target = target
        if snapshot:
            stats = self._stats_by_pid.get(target.pid)
            if stats is None:
                stats = RollingProcessStats(target.pid, target.process_name)
                self._stats_by_pid[target.pid] = stats
            stats.add_sample(
                PresentMonFrameSample(
                    timestamp_monotonic=now,
                    process_name=target.process_name,
                    pid=target.pid,
                    frametime_ms=snapshot.frametime_ms,
                    cpu_busy_ms=snapshot.cpu_busy_ms,
                    gpu_busy_ms=snapshot.gpu_busy_ms,
                    display_latency_ms=snapshot.display_latency_ms,
                    present_mode=None,
                )
            )

        self._prune_stale_stats(now)
        return [self._build_metric(context, target)]

    def shutdown(self):
        if self._client:
            self._client.close()
            self._client = None

    def _resolve_target(self) -> Optional[PresentMonTarget]:
        target_mode = (self._presentmon_config.target_mode or "active_foreground").strip().lower()

        if target_mode == "explicit_process_id":
            pid = int(getattr(self._presentmon_config, "process_id", 0) or 0)
            if pid <= 0:
                return None
            return PresentMonTarget(
                mode=target_mode,
                filter_kind="process_id",
                filter_value=str(pid),
                pid=pid,
                process_name=self._get_process_name(pid),
            )

        if target_mode == "explicit_process_name":
            process_name = (getattr(self._presentmon_config, "process_name", "") or "").strip()
            if not process_name:
                return None
            pid = self._find_process_by_name(process_name) or 0
            if pid <= 0:
                return None
            return PresentMonTarget(
                mode=target_mode,
                filter_kind="process_name",
                filter_value=process_name,
                pid=pid,
                process_name=self._get_process_name(pid),
            )

        if target_mode == "active_foreground":
            pid = self._get_foreground_pid()
            if not pid:
                return None
            process_name = self._get_process_name(pid)
            if process_name.lower() in NON_GAME_FOREGROUND_PROCESSES:
                logger.debug(
                    "PresentMon Service API ignored foreground process as non-game: pid=%s process=%s",
                    pid,
                    process_name,
                )
                return None
            return PresentMonTarget(
                mode=target_mode,
                filter_kind="process_id",
                filter_value=str(pid),
                pid=pid,
                process_name=process_name,
            )

        logger.debug("PresentMon Service API target_mode '%s' is not supported.", target_mode)
        return None

    def _find_process_by_name(self, process_name: str) -> Optional[int]:
        target_name = process_name.lower()
        for process in psutil.process_iter(["name", "pid"]):
            try:
                if (process.info.get("name") or "").lower() == target_name:
                    return process.info["pid"]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return None

    def _get_process_name(self, pid: int) -> str:
        if pid <= 0:
            return "unknown"
        try:
            return psutil.Process(pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            return "unknown"

    def _get_foreground_pid(self) -> Optional[int]:
        if win32gui is None or win32process is None:
            return None
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return None
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return pid or None
        except Exception:
            return None

    def _build_metric(self, context: ProviderContext, target: Optional[PresentMonTarget]) -> MetricData:
        snapshot = self._snapshot_for_target(target)
        process_name = snapshot.get("process_name") or (target.process_name if target else "unknown")
        pid = int(snapshot.get("pid") or (target.pid if target else 0) or 0)

        fields = {
            "fps_now": float(snapshot.get("fps_now", 0.0)),
            "frametime_ms_now": float(snapshot.get("frametime_ms_now", 0.0)),
            "fps_avg_10s": float(snapshot.get("fps_avg_10s", 0.0)),
            "fps_avg_30s": float(snapshot.get("fps_avg_30s", 0.0)),
            "fps_1pct_30s": float(snapshot.get("fps_1pct_30s", 0.0)),
            "fps_0_1pct_30s": float(snapshot.get("fps_0_1pct_30s", 0.0)),
        }
        for optional_field in ("cpu_busy_ms", "gpu_busy_ms", "display_latency_ms"):
            value = snapshot.get(optional_field)
            if value is not None:
                fields[optional_field] = float(value)

        tags = {
            "host": context.host_alias,
            "process_name": process_name,
            "pid": str(pid),
            "app_mode": (self._presentmon_config.target_mode or "active_foreground"),
            "backend": BACKEND_NAME,
        }

        logger.debug(
            "PresentMon Service API metric values before MetricData: fields=%s tags=%s target=%s error=%s",
            fields,
            tags,
            target,
            self._last_error,
        )
        return MetricData(measurement_name="pc_fps", tags=tags, fields=fields)

    def _snapshot_for_target(self, target: Optional[PresentMonTarget]) -> Dict[str, object]:
        if target is None:
            return {"process_name": "unknown", "pid": 0}
        stats = self._stats_by_pid.get(target.pid)
        if stats is None:
            return {"process_name": target.process_name, "pid": target.pid}
        snapshot = stats.snapshot()
        snapshot["process_name"] = stats.process_name or target.process_name
        snapshot["pid"] = stats.pid or target.pid
        return snapshot

    def _prune_stale_stats(self, now: float):
        stale_pids = [
            pid for pid, stats in self._stats_by_pid.items()
            if not stats.has_recent_samples(now, max_age_seconds=MAX_SAMPLE_AGE_SECONDS)
        ]
        for pid in stale_pids:
            del self._stats_by_pid[pid]
