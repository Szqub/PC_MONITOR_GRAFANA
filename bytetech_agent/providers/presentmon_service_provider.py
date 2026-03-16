"""
PresentMon Service API FPS provider.

Uses the official PresentMon Service API as the primary production FPS backend.
It connects to the already running PresentMon service via PresentMonAPI2.dll and
polls live telemetry for the selected target process.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

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
    PresentMonApiSnapshot,
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
MAX_SAMPLE_AGE_SECONDS = 30.0
LAST_GOOD_TARGET_GRACE_SECONDS = 7.5

PROCESS_DENYLIST = {
    "applicationframehost.exe",
    "brave.exe",
    "chatgpt.exe",
    "chrome.exe",
    "cmd.exe",
    "codex.exe",
    "conhost.exe",
    "explorer.exe",
    "firefox.exe",
    "msedge.exe",
    "powershell.exe",
    "pwsh.exe",
    "searchhost.exe",
    "startmenuexperiencehost.exe",
    "steamwebhelper.exe",
    "windowsterminal.exe",
}
PROCESS_HINT_DENYLIST = (
    "anticheat",
    "anti-cheat",
    "battleye",
    "bootstrap",
    "crashhandler",
    "discord",
    "easyanticheat",
    "eac",
    "helper",
    "launcher",
    "overlay",
    "updater",
    "webhelper",
)


@dataclass
class _Candidate:
    pid: int
    process_name: str
    source: str
    score: int
    reasons: List[str] = field(default_factory=list)


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
        self._last_good_target: Optional[PresentMonTarget] = None
        self._last_good_target_monotonic: Optional[float] = None

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
            "fps_application_now": True,
            "fps_displayed_now": True,
            "fps_now": True,
            "frametime_ms_now": True,
            "fps_avg_10s": True,
            "fps_avg_30s": True,
            "fps_1pct_30s": True,
            "fps_0_1pct_30s": True,
            "cpu_busy_ms_now": True,
            "gpu_busy_ms_now": True,
            "display_latency_ms_now": True,
            "cpu_busy_ms": True,
            "gpu_busy_ms": True,
            "display_latency_ms": True,
            "present_mode_name": False,
        }
        self._health.status = ProviderStatus.AVAILABLE
        logger.info(
            "PresentMon Service API ready. dll=%s api_version=%s target_mode=%s",
            self._client.paths.chosen_dll,
            self._client.get_api_version_string() or "unknown",
            self._presentmon_config.target_mode or "smart_auto",
        )
        return True

    def _collect(self, context: ProviderContext) -> List[MetricData]:
        if not self._client:
            return []

        now = time.monotonic()
        resolved = self._resolve_target_and_snapshot(now)
        if resolved is None:
            self._prune_stale_stats(now)
            return []

        target, snapshot = resolved
        self._active_target = target
        self._last_error = None
        self._remember_last_good(target, now)

        stats = self._stats_by_pid.get(target.pid)
        if stats is None:
            stats = RollingProcessStats(target.pid, target.process_name)
            self._stats_by_pid[target.pid] = stats
        stats.add_sample(
            PresentMonFrameSample(
                timestamp_monotonic=now,
                process_name=target.process_name,
                pid=target.pid,
                frametime_ms=self._frametime_for_snapshot(snapshot),
                cpu_busy_ms=snapshot.cpu_busy_ms,
                gpu_busy_ms=snapshot.gpu_busy_ms,
                display_latency_ms=snapshot.display_latency_ms,
                present_mode=None,
            )
        )

        self._prune_stale_stats(now)
        metric = self._build_metric(context, target, snapshot)
        return [metric] if metric else []

    def shutdown(self):
        if self._client:
            self._client.close()
            self._client = None

    def _resolve_target_and_snapshot(
        self,
        now: float,
    ) -> Optional[Tuple[PresentMonTarget, PresentMonApiSnapshot]]:
        mode = (self._presentmon_config.target_mode or "smart_auto").strip().lower()
        logger.debug("PresentMon Service API resolving target. mode=%s", mode)

        if mode == "explicit_process_id":
            target = self._resolve_explicit_pid_target()
            if target is None:
                return None
            snapshot = self._poll_snapshot(target, "explicit_process_id")
            if snapshot is None:
                return None
            return target, snapshot

        if mode == "explicit_process_name":
            target = self._resolve_explicit_name_target()
            if target is None:
                return None
            snapshot = self._poll_snapshot(target, "explicit_process_name")
            if snapshot is None:
                return None
            return target, snapshot

        if mode == "active_foreground":
            target = self._resolve_active_foreground_target()
            if target is None:
                return None
            snapshot = self._poll_snapshot(target, "active_foreground")
            if snapshot is None:
                return None
            return target, snapshot

        if mode == "smart_auto":
            return self._resolve_smart_auto_target(now)

        logger.debug("PresentMon Service API target_mode '%s' is not supported.", mode)
        return None

    def _resolve_explicit_pid_target(self) -> Optional[PresentMonTarget]:
        pid = int(getattr(self._presentmon_config, "process_id", 0) or 0)
        if pid <= 0:
            logger.debug("PresentMon explicit_process_id has no valid pid configured.")
            return None
        return PresentMonTarget(
            mode="explicit_process_id",
            filter_kind="process_id",
            filter_value=str(pid),
            pid=pid,
            process_name=self._get_process_name(pid),
        )

    def _resolve_explicit_name_target(self) -> Optional[PresentMonTarget]:
        process_name = (getattr(self._presentmon_config, "process_name", "") or "").strip()
        if not process_name:
            logger.debug("PresentMon explicit_process_name has no process_name configured.")
            return None
        pid = self._find_process_by_name(process_name) or 0
        if pid <= 0:
            logger.debug(
                "PresentMon explicit_process_name found no live pid. process_name=%s",
                process_name,
            )
            return None
        return PresentMonTarget(
            mode="explicit_process_name",
            filter_kind="process_name",
            filter_value=process_name,
            pid=pid,
            process_name=self._get_process_name(pid),
        )

    def _resolve_active_foreground_target(self) -> Optional[PresentMonTarget]:
        pid = self._get_foreground_pid()
        if not pid:
            logger.debug("PresentMon active_foreground found no foreground pid.")
            return None
        process_name = self._get_process_name(pid)
        return PresentMonTarget(
            mode="active_foreground",
            filter_kind="process_id",
            filter_value=str(pid),
            pid=pid,
            process_name=process_name,
        )

    def _resolve_smart_auto_target(
        self,
        now: float,
    ) -> Optional[Tuple[PresentMonTarget, PresentMonApiSnapshot]]:
        candidates = self._build_smart_auto_candidates(now)
        if not candidates:
            logger.debug("PresentMon smart_auto produced no candidates.")
            return None

        for candidate in candidates:
            target = PresentMonTarget(
                mode="smart_auto",
                filter_kind="process_id",
                filter_value=str(candidate.pid),
                pid=candidate.pid,
                process_name=candidate.process_name,
            )
            snapshot = self._poll_snapshot(target, f"smart_auto:{candidate.source}")
            if snapshot is None:
                self._log_candidate(
                    "rejected",
                    pid=candidate.pid,
                    process_name=candidate.process_name,
                    source=candidate.source,
                    score=candidate.score,
                    reason="no_valid_snapshot",
                )
                continue

            self._log_candidate(
                "accepted",
                pid=candidate.pid,
                process_name=candidate.process_name,
                source=candidate.source,
                score=candidate.score,
                application_fps=snapshot.application_fps,
                displayed_fps=snapshot.displayed_fps,
                frametime_ms=snapshot.frametime_ms,
            )
            return target, snapshot

        logger.debug("PresentMon smart_auto rejected all candidates after polling.")
        return None

    def _build_smart_auto_candidates(self, now: float) -> List[_Candidate]:
        foreground_pid = self._get_foreground_pid()
        scored: Dict[int, _Candidate] = {}

        def add_pid(pid: Optional[int], source: str, score: int):
            if not pid or pid <= 0 or pid in scored:
                return
            process_name = self._get_process_name(pid)
            candidate = _Candidate(pid=pid, process_name=process_name, source=source, score=score)
            reject_reason = self._candidate_reject_reason(pid, process_name)
            if reject_reason:
                candidate.reasons.append(reject_reason)
                self._log_candidate(
                    "rejected",
                    pid=pid,
                    process_name=process_name,
                    source=source,
                    score=score,
                    reason=reject_reason,
                )
                return
            scored[pid] = candidate
            self._log_candidate(
                "candidate",
                pid=pid,
                process_name=process_name,
                source=source,
                score=score,
            )

        last_good = self._get_grace_target(now)
        if last_good is not None:
            add_pid(last_good.pid, "last_good", 120)

        if foreground_pid:
            add_pid(foreground_pid, "foreground", 100)
            related = self._related_process_ids(foreground_pid)
            for pid in related.get("children", ()):
                add_pid(pid, "foreground_child", 95)
            if related.get("parent"):
                add_pid(related["parent"], "foreground_parent", 80)
            for pid in related.get("siblings", ()):
                add_pid(pid, "foreground_sibling", 70)
        else:
            logger.debug("PresentMon smart_auto found no foreground pid.")

        return sorted(scored.values(), key=lambda item: item.score, reverse=True)

    def _candidate_reject_reason(self, pid: int, process_name: str) -> Optional[str]:
        lower_name = (process_name or "").strip().lower()
        if pid <= 0:
            return "invalid_pid"
        if not lower_name or lower_name == "unknown":
            return "unknown_process_name"
        if lower_name in PROCESS_DENYLIST:
            return "denylist_exact"

        haystack = " ".join(
            value.lower()
            for value in (
                process_name,
                self._get_process_exe(pid),
                self._get_process_cmdline(pid),
            )
            if value
        )
        for token in PROCESS_HINT_DENYLIST:
            if token in haystack:
                return f"denylist_hint:{token}"
        return None

    def _poll_snapshot(
        self,
        target: PresentMonTarget,
        reason_prefix: str,
    ) -> Optional[PresentMonApiSnapshot]:
        try:
            snapshot = self._client.poll_process(target.pid) if self._client else None
        except PresentMonApiError as exc:
            self._last_error = str(exc)
            logger.debug(
                "PresentMon Service API poll failed. reason=%s target_pid=%s target_process=%s error=%s",
                reason_prefix,
                target.pid,
                target.process_name,
                exc,
            )
            return None

        if not snapshot or not snapshot.usable:
            logger.debug(
                "PresentMon Service API snapshot unusable. reason=%s target_pid=%s target_process=%s snapshot=%s",
                reason_prefix,
                target.pid,
                target.process_name,
                snapshot,
            )
            return None
        return snapshot

    def _build_metric(
        self,
        context: ProviderContext,
        target: PresentMonTarget,
        snapshot: PresentMonApiSnapshot,
    ) -> Optional[MetricData]:
        stats = self._stats_by_pid.get(target.pid)
        if stats is None:
            logger.debug(
                "PresentMon Service API has no rolling stats after snapshot. pid=%s process=%s",
                target.pid,
                target.process_name,
            )
            return None

        rolling = stats.snapshot()
        fps_now = snapshot.application_fps if snapshot.application_fps > 0 else snapshot.displayed_fps
        frametime_ms_now = self._frametime_for_snapshot(snapshot)
        if fps_now <= 0 or frametime_ms_now <= 0:
            logger.debug(
                "PresentMon Service API refusing to emit metric without positive live values. pid=%s process=%s snapshot=%s",
                target.pid,
                target.process_name,
                snapshot,
            )
            return None

        fields = {
            "fps_application_now": float(snapshot.application_fps),
            "fps_displayed_now": float(snapshot.displayed_fps),
            "fps_now": float(fps_now),
            "frametime_ms_now": float(frametime_ms_now),
            "fps_avg_10s": float(rolling.get("fps_avg_10s", 0.0)),
            "fps_avg_30s": float(rolling.get("fps_avg_30s", 0.0)),
            "fps_1pct_30s": float(rolling.get("fps_1pct_30s", 0.0)),
            "fps_0_1pct_30s": float(rolling.get("fps_0_1pct_30s", 0.0)),
        }

        if snapshot.cpu_busy_ms is not None:
            fields["cpu_busy_ms_now"] = float(snapshot.cpu_busy_ms)
            fields["cpu_busy_ms"] = float(snapshot.cpu_busy_ms)
        elif rolling.get("cpu_busy_ms") is not None:
            fields["cpu_busy_ms"] = float(rolling["cpu_busy_ms"])

        if snapshot.gpu_busy_ms is not None:
            fields["gpu_busy_ms_now"] = float(snapshot.gpu_busy_ms)
            fields["gpu_busy_ms"] = float(snapshot.gpu_busy_ms)
        elif rolling.get("gpu_busy_ms") is not None:
            fields["gpu_busy_ms"] = float(rolling["gpu_busy_ms"])

        if snapshot.display_latency_ms is not None:
            fields["display_latency_ms_now"] = float(snapshot.display_latency_ms)
            fields["display_latency_ms"] = float(snapshot.display_latency_ms)
        elif rolling.get("display_latency_ms") is not None:
            fields["display_latency_ms"] = float(rolling["display_latency_ms"])

        tags = {
            "host": context.host_alias,
            "process_name": target.process_name,
            "pid": str(target.pid),
            "app_mode": (self._presentmon_config.target_mode or "smart_auto"),
            "backend": BACKEND_NAME,
        }

        logger.debug(
            "PresentMon Service API metric values before MetricData: fields=%s tags=%s target=%s",
            fields,
            tags,
            target,
        )
        return MetricData(measurement_name="pc_fps", tags=tags, fields=fields)

    def _remember_last_good(self, target: PresentMonTarget, now: float):
        self._last_good_target = target
        self._last_good_target_monotonic = now

    def _get_grace_target(self, now: float) -> Optional[PresentMonTarget]:
        if self._last_good_target is None or self._last_good_target_monotonic is None:
            return None
        if (now - self._last_good_target_monotonic) > LAST_GOOD_TARGET_GRACE_SECONDS:
            return None
        if not self._pid_alive(self._last_good_target.pid):
            return None
        return self._last_good_target

    def _related_process_ids(self, pid: int) -> Dict[str, Iterable[int]]:
        related = {"parent": None, "children": [], "siblings": []}
        try:
            process = psutil.Process(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            return related

        try:
            parent = process.parent()
            if parent is not None:
                related["parent"] = parent.pid
                related["siblings"] = [child.pid for child in parent.children() if child.pid != pid]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        try:
            related["children"] = [child.pid for child in process.children()]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return related

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

    def _get_process_exe(self, pid: int) -> str:
        try:
            return psutil.Process(pid).exe() or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            return ""

    def _get_process_cmdline(self, pid: int) -> str:
        try:
            return " ".join(psutil.Process(pid).cmdline())
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            return ""

    def _pid_alive(self, pid: int) -> bool:
        try:
            return pid > 0 and psutil.pid_exists(pid)
        except Exception:
            return False

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

    def _frametime_for_snapshot(self, snapshot: PresentMonApiSnapshot) -> float:
        if snapshot.frametime_ms > 0:
            return float(snapshot.frametime_ms)
        fps = snapshot.application_fps if snapshot.application_fps > 0 else snapshot.displayed_fps
        return round(1000.0 / fps, 2) if fps > 0 else 0.0

    def _prune_stale_stats(self, now: float):
        stale_pids = [
            pid
            for pid, stats in self._stats_by_pid.items()
            if not stats.has_recent_samples(now, max_age_seconds=MAX_SAMPLE_AGE_SECONDS)
        ]
        for pid in stale_pids:
            del self._stats_by_pid[pid]

    def _log_candidate(self, event: str, **fields):
        details = " ".join(f"{key}={value}" for key, value in fields.items())
        logger.debug("PresentMon smart_auto %s %s", event, details)
