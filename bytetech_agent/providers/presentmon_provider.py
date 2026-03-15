"""
ByteTech Agent - PresentMon provider backed by PresentMon console stdout.

This implementation intentionally avoids PresentMon Shared Service / API V2.
It launches the standalone PresentMon console application as a subprocess,
parses frame-level CSV rows from stdout, maintains rolling windows in memory,
and emits a single `pc_fps` measurement for the current target process.
"""
from __future__ import annotations

import csv
import logging
import math
import os
import shutil
import subprocess
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Deque, Dict, Iterable, List, Optional

import psutil

from bytetech_agent.models.metrics import MetricData, ProviderContext, ProviderStatus
from bytetech_agent.providers.base import BaseProvider

try:
    import win32gui
    import win32process
except ImportError:  # pragma: no cover - Windows deployments should have pywin32
    win32gui = None
    win32process = None

logger = logging.getLogger(__name__)

BACKEND_NAME = "presentmon_console_stdout"
WINDOW_NOW_SECONDS = 1.0
WINDOW_10S_SECONDS = 10.0
WINDOW_30S_SECONDS = 30.0
STARTUP_LINE_LOG_LIMIT = 5
RECORD_LOG_INTERVAL = 120
CREATE_NO_WINDOW = 0x08000000
LAUNCH_RETRY_BACKOFF_SECONDS = 5.0


def _safe_float(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.upper() in {"NA", "N/A", "NULL", "NONE"}:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def _safe_int(raw: Optional[str]) -> Optional[int]:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class PresentMonFrameSample:
    timestamp_monotonic: float
    process_name: str
    pid: int
    frametime_ms: float
    cpu_busy_ms: Optional[float] = None
    gpu_busy_ms: Optional[float] = None
    display_latency_ms: Optional[float] = None
    present_mode: Optional[str] = None


@dataclass(frozen=True)
class PresentMonTarget:
    mode: str
    filter_kind: str
    filter_value: str
    pid: int
    process_name: str

    @property
    def key(self) -> str:
        return f"{self.filter_kind}:{self.filter_value}"


class PresentMonCsvParser:
    """Parses PresentMon CSV rows written to stdout."""

    def __init__(self):
        self._header: Optional[List[str]] = None

    @property
    def header(self) -> Optional[List[str]]:
        return self._header

    def parse_line(self, line: str) -> Optional[PresentMonFrameSample]:
        stripped = line.strip()
        if not stripped:
            return None

        row = next(csv.reader([stripped]))
        if not row:
            return None

        if self._is_header_row(row):
            self._header = row
            return None

        if self._header is None:
            raise ValueError("PresentMon stdout row received before CSV header.")

        if len(row) != len(self._header):
            raise ValueError(
                f"Unexpected CSV column count {len(row)} != {len(self._header)}."
            )

        data = dict(zip(self._header, row))
        pid = _safe_int(data.get("ProcessID"))
        if pid is None or pid <= 0:
            raise ValueError(f"Invalid ProcessID value: {data.get('ProcessID')!r}")

        frametime_ms = self._extract_frametime_ms(data)
        if frametime_ms is None or frametime_ms <= 0:
            return None

        return PresentMonFrameSample(
            timestamp_monotonic=time.monotonic(),
            process_name=(data.get("Application") or "unknown").strip() or "unknown",
            pid=pid,
            frametime_ms=frametime_ms,
            cpu_busy_ms=self._extract_optional(data, "CPUBusy", "MsCPUBusy"),
            gpu_busy_ms=self._extract_optional(data, "GPUBusy", "MsGPUBusy"),
            display_latency_ms=self._extract_optional(data, "DisplayLatency", "MsUntilDisplayed"),
            present_mode=(data.get("PresentMode") or "").strip() or None,
        )

    def _is_header_row(self, row: List[str]) -> bool:
        return "Application" in row and "ProcessID" in row

    def _extract_optional(self, data: Dict[str, str], *keys: str) -> Optional[float]:
        for key in keys:
            value = _safe_float(data.get(key))
            if value is not None:
                return value
        return None

    def _extract_frametime_ms(self, data: Dict[str, str]) -> Optional[float]:
        for key in ("FrameTime", "MsBetweenPresents", "DisplayedTime"):
            value = _safe_float(data.get(key))
            if value is not None and value > 0:
                return value
        return None


class RollingProcessStats:
    """Maintains per-process rolling frame samples for 1s / 10s / 30s windows."""

    def __init__(self, pid: int, process_name: str):
        self.pid = pid
        self.process_name = process_name
        self.samples: Deque[PresentMonFrameSample] = deque()
        self.last_sample_monotonic: Optional[float] = None

    def add_sample(self, sample: PresentMonFrameSample):
        self.process_name = sample.process_name
        self.last_sample_monotonic = sample.timestamp_monotonic
        self.samples.append(sample)
        self._evict(sample.timestamp_monotonic)

    def snapshot(self, now: Optional[float] = None) -> Dict[str, object]:
        now = now if now is not None else time.monotonic()
        self._evict(now)

        one_second = self._slice_window(now, WINDOW_NOW_SECONDS)
        ten_seconds = self._slice_window(now, WINDOW_10S_SECONDS)
        thirty_seconds = self._slice_window(now, WINDOW_30S_SECONDS)

        fields: Dict[str, object] = {
            "fps_now": self._fps_from_samples(one_second),
            "frametime_ms_now": self._avg_frametime(one_second),
            "fps_avg_10s": self._fps_from_samples(ten_seconds),
            "fps_avg_30s": self._fps_from_samples(thirty_seconds),
            "fps_1pct_30s": self._low_percentile_fps(thirty_seconds, 0.01),
            "fps_0_1pct_30s": self._low_percentile_fps(thirty_seconds, 0.001),
            "sample_count_1s": len(one_second),
            "sample_count_10s": len(ten_seconds),
            "sample_count_30s": len(thirty_seconds),
        }

        cpu_busy = self._avg_optional(one_second, "cpu_busy_ms")
        gpu_busy = self._avg_optional(one_second, "gpu_busy_ms")
        display_latency = self._avg_optional(one_second, "display_latency_ms")
        present_mode_name = self._dominant_present_mode(one_second)

        if cpu_busy is not None:
            fields["cpu_busy_ms"] = cpu_busy
        if gpu_busy is not None:
            fields["gpu_busy_ms"] = gpu_busy
        if display_latency is not None:
            fields["display_latency_ms"] = display_latency
        if present_mode_name is not None:
            fields["present_mode_name"] = present_mode_name

        return fields

    def has_recent_samples(self, now: Optional[float] = None, max_age_seconds: float = WINDOW_30S_SECONDS) -> bool:
        now = now if now is not None else time.monotonic()
        if self.last_sample_monotonic is None:
            return False
        return (now - self.last_sample_monotonic) <= max_age_seconds

    def _evict(self, now: float):
        min_timestamp = now - WINDOW_30S_SECONDS
        while self.samples and self.samples[0].timestamp_monotonic < min_timestamp:
            self.samples.popleft()

    def _slice_window(self, now: float, window_seconds: float) -> List[PresentMonFrameSample]:
        start = now - window_seconds
        return [sample for sample in self.samples if sample.timestamp_monotonic >= start]

    def _avg_frametime(self, samples: Iterable[PresentMonFrameSample]) -> float:
        values = [sample.frametime_ms for sample in samples if sample.frametime_ms > 0]
        if not values:
            return 0.0
        return round(sum(values) / len(values), 2)

    def _fps_from_samples(self, samples: Iterable[PresentMonFrameSample]) -> float:
        values = [sample.frametime_ms for sample in samples if sample.frametime_ms > 0]
        if not values:
            return 0.0
        avg_frametime = sum(values) / len(values)
        if avg_frametime <= 0:
            return 0.0
        return round(1000.0 / avg_frametime, 2)

    def _low_percentile_fps(self, samples: Iterable[PresentMonFrameSample], percentile: float) -> float:
        fps_values = sorted(
            1000.0 / sample.frametime_ms
            for sample in samples
            if sample.frametime_ms > 0
        )
        if not fps_values:
            return 0.0
        if len(fps_values) == 1:
            return round(fps_values[0], 2)
        rank = max(0, math.ceil(len(fps_values) * percentile) - 1)
        rank = min(rank, len(fps_values) - 1)
        return round(fps_values[rank], 2)

    def _avg_optional(self, samples: Iterable[PresentMonFrameSample], attribute_name: str) -> Optional[float]:
        values = [
            value for value in
            (getattr(sample, attribute_name) for sample in samples)
            if value is not None
        ]
        if not values:
            return None
        return round(sum(values) / len(values), 2)

    def _dominant_present_mode(self, samples: Iterable[PresentMonFrameSample]) -> Optional[str]:
        values = [sample.present_mode for sample in samples if sample.present_mode]
        if not values:
            return None
        return Counter(values).most_common(1)[0][0]


class PresentMonProvider(BaseProvider):
    """FPS provider using PresentMon console stdout streaming."""

    def __init__(self, config):
        super().__init__(name="PresentMon")
        self.config = config
        self._exe_path: Optional[str] = None
        self._capture_process: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._process_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stats_by_pid: Dict[int, RollingProcessStats] = {}
        self._active_target: Optional[PresentMonTarget] = None
        self._records_processed = 0
        self._reader_generation = 0
        self._last_capture_error: Optional[str] = None
        self._next_launch_retry_monotonic = 0.0
        self._last_retry_skip_log_monotonic = 0.0

    def initialize(self) -> bool:
        self._exe_path = self._discover_presentmon_exe()
        if not self._exe_path:
            self._health.mark_unavailable("PresentMon.exe not found.")
            logger.warning(
                "PresentMon Provider unavailable: standalone PresentMon console executable not found."
            )
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
            "present_mode_name": True,
        }
        self._health.status = ProviderStatus.AVAILABLE
        logger.debug("PresentMon console provider ready. exe=%s", self._exe_path)
        return True

    def _collect(self, context: ProviderContext) -> List[MetricData]:
        target = self._resolve_target()
        self._ensure_capture_target(target)
        metric = self._build_metric(context, target)
        return [metric]

    def shutdown(self):
        self._ensure_capture_target(None)

    def _discover_presentmon_exe(self) -> Optional[str]:
        configured_path = getattr(self.config, "executable_path", None)
        if configured_path:
            if os.path.isfile(configured_path) and self._is_gui_presentmon_path(configured_path):
                logger.error(
                    "Configured PresentMon executable points to GUI PresentMonApplication build, "
                    "which is not supported as fallback executable: %s",
                    configured_path,
                )
                return None

        candidates = [
            configured_path,
            os.environ.get("PRESENTMON_EXE"),
            os.path.join("C:\\ByteTechAgent", "bin", "PresentMon.exe"),
            os.path.join(os.environ.get("ProgramFiles", ""), "PresentMon", "PresentMon.exe"),
            os.path.join(os.getcwd(), "PresentMon.exe"),
            shutil.which("PresentMon.exe"),
        ]
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                if self._is_gui_presentmon_path(candidate):
                    logger.error(
                        "PresentMon executable path points to GUI PresentMonApplication build, "
                        "which is not supported as fallback executable: %s",
                        candidate,
                    )
                    continue
                return candidate
        return None

    def _is_gui_presentmon_path(self, path: str) -> bool:
        normalized = os.path.normcase(os.path.abspath(path))
        return "presentmonapplication" in normalized

    def _resolve_target(self) -> Optional[PresentMonTarget]:
        target_mode = (self.config.target_mode or "active_foreground").strip().lower()

        if target_mode in {"explicit_pid", "explicit_process_id"}:
            pid = int(getattr(self.config, "process_id", 0) or 0)
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
            process_name = (getattr(self.config, "process_name", "") or "").strip()
            if not process_name:
                return None
            pid = self._find_process_by_name(process_name) or 0
            return PresentMonTarget(
                mode=target_mode,
                filter_kind="process_name",
                filter_value=process_name,
                pid=pid,
                process_name=process_name,
            )

        if target_mode == "active_foreground":
            pid = self._get_foreground_pid()
            if not pid:
                return None
            return PresentMonTarget(
                mode=target_mode,
                filter_kind="process_id",
                filter_value=str(pid),
                pid=pid,
                process_name=self._get_process_name(pid),
            )

        logger.debug("PresentMon target_mode '%s' is not supported.", target_mode)
        return None

    def _find_process_by_name(self, process_name: str) -> Optional[int]:
        process_name = process_name.lower()
        for process in psutil.process_iter(["name", "pid"]):
            try:
                name = (process.info.get("name") or "").lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if name == process_name:
                return process.info["pid"]
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

    def _ensure_capture_target(self, target: Optional[PresentMonTarget]):
        with self._process_lock:
            if self._targets_match(self._active_target, target) and self._process_running():
                return

            now = time.monotonic()
            if (
                self._targets_match(self._active_target, target)
                and target is not None
                and self._last_capture_error
                and now < self._next_launch_retry_monotonic
            ):
                if now - self._last_retry_skip_log_monotonic >= LAUNCH_RETRY_BACKOFF_SECONDS:
                    remaining = max(0.0, self._next_launch_retry_monotonic - now)
                    logger.debug(
                        "PresentMon launch retry deferred for %.1fs after previous start failure. "
                        "target=%s (%s) error=%s",
                        remaining,
                        target.process_name,
                        target.filter_value,
                        self._last_capture_error,
                    )
                    self._last_retry_skip_log_monotonic = now
                return

            if self._active_target and target and self._active_target.key != target.key:
                logger.debug(
                    "PresentMon target switch: %s (%s) -> %s (%s)",
                    self._active_target.process_name,
                    self._active_target.filter_value,
                    target.process_name,
                    target.filter_value,
                )
            elif self._active_target and target is None:
                logger.debug(
                    "PresentMon target cleared: %s (%s)",
                    self._active_target.process_name,
                    self._active_target.filter_value,
                )
            elif target and not self._active_target:
                logger.debug(
                    "PresentMon target acquired: %s (%s)",
                    target.process_name,
                    target.filter_value,
                )

            self._stop_capture_locked()
            self._active_target = target
            self._last_capture_error = None
            self._next_launch_retry_monotonic = 0.0
            self._last_retry_skip_log_monotonic = 0.0

            if target is not None:
                self._start_capture_locked(target)

    def _targets_match(
        self,
        left: Optional[PresentMonTarget],
        right: Optional[PresentMonTarget],
    ) -> bool:
        return left == right

    def _process_running(self) -> bool:
        return self._capture_process is not None and self._capture_process.poll() is None

    def _start_capture_locked(self, target: PresentMonTarget):
        if not self._exe_path:
            return

        command = self._build_command(target)
        self._reader_generation += 1
        generation = self._reader_generation

        logger.debug("PresentMon launch command: %s", subprocess.list2cmdline(command))

        creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self._capture_process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                universal_newlines=True,
                creationflags=creationflags,
            )
        except OSError as exc:
            self._last_capture_error = str(exc)
            self._next_launch_retry_monotonic = time.monotonic() + LAUNCH_RETRY_BACKOFF_SECONDS
            logger.error("PresentMon subprocess failed to start: %s", exc)
            self._capture_process = None
            return

        self._next_launch_retry_monotonic = 0.0
        self._last_retry_skip_log_monotonic = 0.0
        logger.debug("PresentMon subprocess started. pid=%s", self._capture_process.pid)

        self._stdout_thread = threading.Thread(
            target=self._stdout_reader_loop,
            args=(self._capture_process, generation),
            name=f"PresentMon-stdout-{generation}",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_reader_loop,
            args=(self._capture_process, generation),
            name=f"PresentMon-stderr-{generation}",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _stop_capture_locked(self):
        process = self._capture_process
        stdout_thread = self._stdout_thread
        stderr_thread = self._stderr_thread

        self._capture_process = None
        self._stdout_thread = None
        self._stderr_thread = None

        if process is None:
            return

        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        except Exception as exc:
            logger.debug("PresentMon subprocess stop error: %s", exc)
        finally:
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()

        if stdout_thread:
            stdout_thread.join(timeout=1)
        if stderr_thread:
            stderr_thread.join(timeout=1)

        logger.debug("PresentMon subprocess stopped. exit_code=%s", process.poll())

    def _build_command(self, target: PresentMonTarget) -> List[str]:
        command = [
            self._exe_path or "PresentMon.exe",
            "--output_stdout",
            "--no_console_stats",
            "--stop_existing_session",
            "--session_name",
            f"ByteTechAgent-{os.getpid()}",
        ]
        # `--output_stdout` and `--no_csv` are mutually exclusive in PresentMon CLI.

        if target.filter_kind == "process_id":
            command.extend(["--process_id", target.filter_value])
            command.append("--terminate_on_proc_exit")
        else:
            command.extend(["--process_name", target.filter_value])

        return command

    def _stdout_reader_loop(self, process: subprocess.Popen, generation: int):
        parser = PresentMonCsvParser()
        startup_lines_logged = 0

        if not process.stdout:
            return

        for raw_line in process.stdout:
            if generation != self._reader_generation:
                break

            line = raw_line.rstrip("\r\n")
            if startup_lines_logged < STARTUP_LINE_LOG_LIMIT:
                logger.debug("PresentMon stdout[%s]: %s", startup_lines_logged + 1, line)
                startup_lines_logged += 1

            try:
                sample = parser.parse_line(line)
            except Exception as exc:
                logger.debug("PresentMon stdout parser error: %s | line=%r", exc, line)
                continue

            if sample is None:
                continue

            with self._stats_lock:
                stats = self._stats_by_pid.get(sample.pid)
                if stats is None:
                    stats = RollingProcessStats(sample.pid, sample.process_name)
                    self._stats_by_pid[sample.pid] = stats
                stats.add_sample(sample)
                self._records_processed += 1
                if self._records_processed % RECORD_LOG_INTERVAL == 0:
                    logger.debug(
                        "PresentMon processed frame-level records=%s",
                        self._records_processed,
                    )

        logger.debug(
            "PresentMon stdout reader exiting. generation=%s exit_code=%s",
            generation,
            process.poll(),
        )

    def _stderr_reader_loop(self, process: subprocess.Popen, generation: int):
        startup_lines_logged = 0
        if not process.stderr:
            return

        for raw_line in process.stderr:
            if generation != self._reader_generation:
                break
            line = raw_line.rstrip("\r\n")
            if startup_lines_logged < STARTUP_LINE_LOG_LIMIT:
                logger.debug("PresentMon stderr[%s]: %s", startup_lines_logged + 1, line)
                startup_lines_logged += 1
            elif line:
                logger.debug("PresentMon stderr: %s", line)

        logger.debug(
            "PresentMon stderr reader exiting. generation=%s exit_code=%s",
            generation,
            process.poll(),
        )

    def _build_metric(
        self,
        context: ProviderContext,
        target: Optional[PresentMonTarget],
    ) -> MetricData:
        snapshot = self._snapshot_for_target(target)

        reason = snapshot.get("reason", "ok")
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

        present_mode_name = snapshot.get("present_mode_name")
        if present_mode_name:
            fields["present_mode_name"] = present_mode_name

        tags = {
            "host": context.host_alias,
            "process_name": process_name,
            "pid": str(pid),
            "app_mode": (self.config.target_mode or "active_foreground"),
            "backend": BACKEND_NAME,
        }

        logger.debug(
            "PresentMon metric values before MetricData: reason=%s fields=%s tags=%s",
            reason,
            fields,
            tags,
        )

        if reason != "ok":
            logger.debug("PresentMon sending zero/default metric because: %s", reason)

        return MetricData(measurement_name="pc_fps", tags=tags, fields=fields)

    def _snapshot_for_target(self, target: Optional[PresentMonTarget]) -> Dict[str, object]:
        now = time.monotonic()
        with self._stats_lock:
            self._prune_stale_stats(now)

            if target is None:
                return {"reason": "no_target", "process_name": "unknown", "pid": 0}

            if self._last_capture_error:
                return {
                    "reason": f"capture_start_failed: {self._last_capture_error}",
                    "process_name": target.process_name,
                    "pid": target.pid,
                }

            stats = self._select_stats_for_target(target, now)
            if stats is None:
                return {
                    "reason": "no_samples_for_target",
                    "process_name": target.process_name,
                    "pid": target.pid,
                }

            snapshot = stats.snapshot(now)
            snapshot["process_name"] = stats.process_name or target.process_name
            snapshot["pid"] = stats.pid or target.pid

            if snapshot["sample_count_30s"] == 0:
                snapshot["reason"] = "target_has_no_recent_frames"
            else:
                snapshot["reason"] = "ok"
            return snapshot

    def _select_stats_for_target(
        self,
        target: PresentMonTarget,
        now: float,
    ) -> Optional[RollingProcessStats]:
        if target.filter_kind == "process_id":
            stats = self._stats_by_pid.get(target.pid)
            if stats and stats.has_recent_samples(now):
                return stats
            return None

        candidates = [
            stats for stats in self._stats_by_pid.values()
            if stats.process_name.lower() == target.process_name.lower() and stats.has_recent_samples(now)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda stats: stats.last_sample_monotonic or 0.0)

    def _prune_stale_stats(self, now: float):
        stale_pids = [
            pid for pid, stats in self._stats_by_pid.items()
            if not stats.has_recent_samples(now, max_age_seconds=WINDOW_30S_SECONDS)
        ]
        for pid in stale_pids:
            del self._stats_by_pid[pid]
