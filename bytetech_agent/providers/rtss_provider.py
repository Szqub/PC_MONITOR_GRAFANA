"""
ByteTech Agent - RTSS shared memory FPS provider.

RTSS is treated as the primary production FPS backend. It provides live
sampled framerate telemetry through shared memory. Rolling 10s/30s averages
and low-percentile values are approximations derived from sampled RTSS values,
not from a full raw frame-event trace.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Iterable, List, Optional

import psutil

from bytetech_agent.models.metrics import MetricData, ProviderContext, ProviderStatus
from bytetech_agent.providers.base import BaseProvider

try:
    import win32gui
    import win32process
except ImportError:  # pragma: no cover
    win32gui = None
    win32process = None

logger = logging.getLogger(__name__)

BACKEND_NAME = "rtss_shared_memory"
FILE_MAP_READ = 0x0004
WINDOW_NOW_SECONDS = 1.0
WINDOW_10S_SECONDS = 10.0
WINDOW_30S_SECONDS = 30.0
RTSS_SIGNATURE = int.from_bytes(b"RTSS", "little")
RTSS_MIN_VERSION = 0x00020000
RTSS_RING_BUFFER_VERSION = 0x00020005
MAX_PATH_CHARS = 260

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenFileMappingW.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
kernel32.OpenFileMappingW.restype = ctypes.wintypes.HANDLE
kernel32.MapViewOfFile.argtypes = [
    ctypes.wintypes.HANDLE,
    ctypes.wintypes.DWORD,
    ctypes.wintypes.DWORD,
    ctypes.wintypes.DWORD,
    ctypes.c_size_t,
]
kernel32.MapViewOfFile.restype = ctypes.c_void_p
kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
kernel32.UnmapViewOfFile.restype = ctypes.wintypes.BOOL
kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
kernel32.GetTickCount64.argtypes = []
kernel32.GetTickCount64.restype = ctypes.c_ulonglong


class RTSSSharedMemoryHeader(ctypes.Structure):
    _fields_ = [
        ("dwSignature", ctypes.c_uint32),
        ("dwVersion", ctypes.c_uint32),
        ("dwAppEntrySize", ctypes.c_uint32),
        ("dwAppArrOffset", ctypes.c_uint32),
        ("dwAppArrSize", ctypes.c_uint32),
        ("dwOSDEntrySize", ctypes.c_uint32),
        ("dwOSDArrOffset", ctypes.c_uint32),
        ("dwOSDArrSize", ctypes.c_uint32),
        ("dwOSDFrame", ctypes.c_uint32),
    ]


class RTSSSharedMemoryAppEntry(ctypes.Structure):
    _fields_ = [
        ("dwProcessID", ctypes.c_uint32),
        ("szProcessName", ctypes.c_char * MAX_PATH_CHARS),
        ("szProfileName", ctypes.c_char * MAX_PATH_CHARS),
        ("dwFlags", ctypes.c_uint32),
        ("dwTime0", ctypes.c_uint32),
        ("dwTime1", ctypes.c_uint32),
        ("dwFrames", ctypes.c_uint32),
        ("dwFrameTime", ctypes.c_uint32),
        ("dwStatFlags", ctypes.c_uint32),
        ("dwStatTime0", ctypes.c_uint32),
        ("dwStatTime1", ctypes.c_uint32),
        ("dwStatFrames", ctypes.c_uint32),
        ("dwStatCount", ctypes.c_uint32),
        ("dwStatFramerate", ctypes.c_uint32),
        ("dwStatFrameTime", ctypes.c_uint32),
        ("dwStatFrameTimeBufFramerate", ctypes.c_uint32),
        ("dwStatFrameTimeBuf", ctypes.c_uint32),
        ("dwStatFrameTimeBufPos", ctypes.c_uint32),
        ("dwOSDX", ctypes.c_uint32),
        ("dwOSDY", ctypes.c_uint32),
    ]


@dataclass(frozen=True)
class RtssSample:
    timestamp_monotonic: float
    process_name: str
    pid: int
    fps: float
    frametime_ms: float
    source_quality: str


@dataclass(frozen=True)
class RtssTarget:
    mode: str
    pid: int
    process_name: str


@dataclass(frozen=True)
class RtssReadResult:
    status: str
    entries: List["RtssAppRecord"]
    error: Optional[str] = None


@dataclass(frozen=True)
class RtssAppRecord:
    pid: int
    process_name: str
    fps: float
    frametime_ms: float
    source_quality: str
    last_tick_ms: int


class RtssRollingStats:
    def __init__(self):
        self._samples_by_pid: Dict[int, Deque[RtssSample]] = {}
        self._last_process_names: Dict[int, str] = {}

    def add_sample(self, sample: RtssSample):
        bucket = self._samples_by_pid.setdefault(sample.pid, deque())
        bucket.append(sample)
        self._last_process_names[sample.pid] = sample.process_name
        self._evict_bucket(bucket, sample.timestamp_monotonic)

    def snapshot(self, pid: int, now: Optional[float] = None) -> Optional[Dict[str, object]]:
        now = now if now is not None else time.monotonic()
        bucket = self._samples_by_pid.get(pid)
        if not bucket:
            return None
        self._evict_bucket(bucket, now)
        if not bucket:
            return None

        one_second = [sample for sample in bucket if sample.timestamp_monotonic >= now - WINDOW_NOW_SECONDS]
        ten_seconds = [sample for sample in bucket if sample.timestamp_monotonic >= now - WINDOW_10S_SECONDS]
        thirty_seconds = [sample for sample in bucket if sample.timestamp_monotonic >= now - WINDOW_30S_SECONDS]

        if not thirty_seconds:
            return None

        source_quality = thirty_seconds[-1].source_quality
        return {
            "pid": pid,
            "process_name": self._last_process_names.get(pid, "unknown"),
            "fps_now": self._avg_fps(one_second),
            "frametime_ms_now": self._avg_frametime(one_second),
            "fps_avg_10s": self._avg_fps(ten_seconds),
            "fps_avg_30s": self._avg_fps(thirty_seconds),
            "fps_1pct_30s": self._low_percentile_fps(thirty_seconds, 0.01),
            "fps_0_1pct_30s": self._low_percentile_fps(thirty_seconds, 0.001),
            "sample_count_10s": len(ten_seconds),
            "sample_count_30s": len(thirty_seconds),
            "source_quality": source_quality,
        }

    def prune(self, now: Optional[float] = None):
        now = now if now is not None else time.monotonic()
        stale = []
        for pid, bucket in self._samples_by_pid.items():
            self._evict_bucket(bucket, now)
            if not bucket:
                stale.append(pid)
        for pid in stale:
            del self._samples_by_pid[pid]
            self._last_process_names.pop(pid, None)

    def _evict_bucket(self, bucket: Deque[RtssSample], now: float):
        cutoff = now - WINDOW_30S_SECONDS
        while bucket and bucket[0].timestamp_monotonic < cutoff:
            bucket.popleft()

    def _avg_fps(self, samples: Iterable[RtssSample]) -> float:
        values = [sample.fps for sample in samples if sample.fps > 0]
        if not values:
            return 0.0
        return round(sum(values) / len(values), 2)

    def _avg_frametime(self, samples: Iterable[RtssSample]) -> float:
        values = [sample.frametime_ms for sample in samples if sample.frametime_ms > 0]
        if not values:
            return 0.0
        return round(sum(values) / len(values), 2)

    def _low_percentile_fps(self, samples: Iterable[RtssSample], percentile: float) -> float:
        values = sorted(sample.fps for sample in samples if sample.fps > 0)
        if not values:
            return 0.0
        if len(values) == 1:
            return round(values[0], 2)
        rank = max(0, math.ceil(len(values) * percentile) - 1)
        rank = min(rank, len(values) - 1)
        return round(values[rank], 2)


class RtssSharedMemoryReader:
    def __init__(self, shared_memory_name: str, stale_timeout_ms: int):
        self._shared_memory_name = shared_memory_name
        self._stale_timeout_ms = stale_timeout_ms

    def read_entries(self) -> RtssReadResult:
        mapping = kernel32.OpenFileMappingW(FILE_MAP_READ, False, self._shared_memory_name)
        if not mapping:
            return RtssReadResult(
                status="mapping_unavailable",
                entries=[],
                error=f"RTSS shared memory '{self._shared_memory_name}' is not available.",
            )

        view = kernel32.MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, 0)
        if not view:
            kernel32.CloseHandle(mapping)
            return RtssReadResult(
                status="map_failed",
                entries=[],
                error=f"Failed to map RTSS shared memory '{self._shared_memory_name}'.",
            )

        try:
            return self._parse_view(view)
        finally:
            kernel32.UnmapViewOfFile(view)
            kernel32.CloseHandle(mapping)

    def _parse_view(self, view: int) -> RtssReadResult:
        header = RTSSSharedMemoryHeader.from_address(view)
        if header.dwSignature != RTSS_SIGNATURE or header.dwVersion < RTSS_MIN_VERSION:
            return RtssReadResult(
                status="invalid_header",
                entries=[],
                error=(
                    f"Unexpected RTSS header signature/version: "
                    f"signature=0x{header.dwSignature:08X} version=0x{header.dwVersion:08X}"
                ),
            )

        if header.dwAppEntrySize < ctypes.sizeof(RTSSSharedMemoryAppEntry):
            return RtssReadResult(
                status="unsupported_layout",
                entries=[],
                error=(
                    f"RTSS app entry size {header.dwAppEntrySize} is smaller than expected "
                    f"{ctypes.sizeof(RTSSSharedMemoryAppEntry)}."
                ),
            )

        entries: List[RtssAppRecord] = []
        current_tick_ms = int(kernel32.GetTickCount64() & 0xFFFFFFFF)
        for index in range(header.dwAppArrSize):
            entry_address = view + header.dwAppArrOffset + index * header.dwAppEntrySize
            entry = RTSSSharedMemoryAppEntry.from_address(entry_address)
            if entry.dwProcessID == 0:
                continue

            process_name = self._decode_c_string(entry.szProcessName)
            if not process_name:
                continue

            fps, source_quality = self._extract_fps(header.dwVersion, entry)
            if fps <= 0:
                continue

            if self._is_stale(current_tick_ms, entry.dwTime1):
                continue

            entries.append(
                RtssAppRecord(
                    pid=entry.dwProcessID,
                    process_name=process_name,
                    fps=round(fps, 2),
                    frametime_ms=round(1000.0 / fps, 2),
                    source_quality=source_quality,
                    last_tick_ms=entry.dwTime1,
                )
            )

        return RtssReadResult(status="ok", entries=entries)

    def _decode_c_string(self, buffer) -> str:
        return bytes(buffer).split(b"\0", 1)[0].decode("utf-8", errors="ignore").strip()

    def _extract_fps(self, version: int, entry: RTSSSharedMemoryAppEntry) -> tuple[float, str]:
        if version >= RTSS_RING_BUFFER_VERSION and entry.dwStatFrameTimeBufFramerate > 0:
            return entry.dwStatFrameTimeBufFramerate / 10.0, "rtss_ring_buffer_sampled"

        delta_ms = (entry.dwTime1 - entry.dwTime0) & 0xFFFFFFFF
        if delta_ms > 0 and entry.dwFrames > 0:
            fps = entry.dwFrames * 1000.0 / delta_ms
            return fps, "rtss_frame_counter_sampled"

        return 0.0, "rtss_no_fps"

    def _is_stale(self, current_tick_ms: int, sample_tick_ms: int) -> bool:
        age = (current_tick_ms - sample_tick_ms) & 0xFFFFFFFF
        return age > self._stale_timeout_ms


class RtssProvider(BaseProvider):
    def __init__(self, fps_config, rtss_config, presentmon_config):
        super().__init__(name="RTSS")
        self._fps_config = fps_config
        self._rtss_config = rtss_config
        self._presentmon_config = presentmon_config
        self._reader = RtssSharedMemoryReader(
            shared_memory_name=rtss_config.shared_memory_name,
            stale_timeout_ms=rtss_config.stale_timeout_ms,
        )
        self._rolling = RtssRollingStats()
        self._last_reader_error: Optional[str] = None
        self._last_reader_error_log_monotonic = 0.0

    def initialize(self) -> bool:
        self._health.capabilities = {
            "fps_now": True,
            "frametime_ms_now": True,
            "fps_avg_10s": True,
            "fps_avg_30s": True,
            "fps_1pct_30s": True,
            "fps_0_1pct_30s": True,
            "source_quality": True,
            "sample_count_10s": True,
            "sample_count_30s": True,
            "present_mode_name": False,
        }
        self._health.status = ProviderStatus.DEGRADED
        logger.debug(
            "RTSS provider initialized. shared_memory_name=%s stale_timeout_ms=%s",
            self._rtss_config.shared_memory_name,
            self._rtss_config.stale_timeout_ms,
        )
        return True

    def _collect(self, context: ProviderContext) -> List[MetricData]:
        target = self._resolve_target()
        result = self._reader.read_entries()
        now = time.monotonic()

        if result.status != "ok":
            self._rolling.prune(now)
            self._log_reader_issue(result.error or result.status)
            return []

        self._last_reader_error = None
        for entry in result.entries:
            self._rolling.add_sample(
                RtssSample(
                    timestamp_monotonic=now,
                    process_name=entry.process_name,
                    pid=entry.pid,
                    fps=entry.fps,
                    frametime_ms=entry.frametime_ms,
                    source_quality=entry.source_quality,
                )
            )
        self._rolling.prune(now)

        metric = self._build_metric(context, target, result.entries, now)
        return [metric]

    def shutdown(self):
        return None

    def _resolve_target(self) -> Optional[RtssTarget]:
        target_mode = self._fps_config.backend
        presentmon_mode = getattr(self._presentmon_config, "target_mode", "active_foreground")
        resolved_mode = presentmon_mode if presentmon_mode else "active_foreground"

        if resolved_mode == "explicit_process_id":
            pid = int(getattr(self._presentmon_config, "process_id", 0) or 0)
            if pid <= 0:
                return None
            return RtssTarget(mode=resolved_mode, pid=pid, process_name=self._get_process_name(pid))

        if resolved_mode == "explicit_process_name":
            process_name = (getattr(self._presentmon_config, "process_name", "") or "").strip()
            if not process_name:
                return None
            pid = self._find_process_by_name(process_name) or 0
            return RtssTarget(mode=resolved_mode, pid=pid, process_name=process_name)

        if resolved_mode == "active_foreground":
            pid = self._get_foreground_pid()
            if not pid:
                return None
            return RtssTarget(mode=resolved_mode, pid=pid, process_name=self._get_process_name(pid))

        logger.debug("RTSS target_mode '%s' is not supported.", resolved_mode)
        return None

    def _build_metric(
        self,
        context: ProviderContext,
        target: Optional[RtssTarget],
        entries: List[RtssAppRecord],
        now: float,
    ) -> MetricData:
        record = self._select_record(target, entries)
        if record:
            snapshot = self._rolling.snapshot(record.pid, now) or {}
            process_name = snapshot.get("process_name", record.process_name)
            pid = int(snapshot.get("pid", record.pid))
            reason = "ok"
        elif target is None:
            snapshot = {}
            process_name = "unknown"
            pid = 0
            reason = "no_target"
        else:
            snapshot = self._rolling.snapshot(target.pid, now) or {}
            process_name = target.process_name or snapshot.get("process_name", "unknown")
            pid = target.pid
            reason = "no_rtss_sample_for_target"

        fields = {
            "fps_now": float(snapshot.get("fps_now", 0.0)),
            "frametime_ms_now": float(snapshot.get("frametime_ms_now", 0.0)),
            "fps_avg_10s": float(snapshot.get("fps_avg_10s", 0.0)),
            "fps_avg_30s": float(snapshot.get("fps_avg_30s", 0.0)),
            "fps_1pct_30s": float(snapshot.get("fps_1pct_30s", 0.0)),
            "fps_0_1pct_30s": float(snapshot.get("fps_0_1pct_30s", 0.0)),
        }
        if "source_quality" in snapshot:
            fields["source_quality"] = snapshot["source_quality"]
        if "sample_count_10s" in snapshot:
            fields["sample_count_10s"] = int(snapshot["sample_count_10s"])
        if "sample_count_30s" in snapshot:
            fields["sample_count_30s"] = int(snapshot["sample_count_30s"])

        tags = {
            "host": context.host_alias,
            "process_name": process_name or "unknown",
            "pid": str(pid),
            "app_mode": getattr(self._presentmon_config, "target_mode", "active_foreground"),
            "backend": BACKEND_NAME,
        }

        logger.debug(
            "RTSS metric values before MetricData: reason=%s fields=%s tags=%s",
            reason,
            fields,
            tags,
        )

        return MetricData(measurement_name="pc_fps", tags=tags, fields=fields)

    def _select_record(self, target: Optional[RtssTarget], entries: List[RtssAppRecord]) -> Optional[RtssAppRecord]:
        if target is None:
            return None

        if target.mode == "explicit_process_name":
            matching = [entry for entry in entries if entry.process_name.lower() == target.process_name.lower()]
            if not matching:
                return None
            if target.pid:
                for entry in matching:
                    if entry.pid == target.pid:
                        return entry
            return matching[0]

        for entry in entries:
            if entry.pid == target.pid:
                return entry
        return None

    def _find_process_by_name(self, process_name: str) -> Optional[int]:
        name_lower = process_name.lower()
        for process in psutil.process_iter(["name", "pid"]):
            try:
                if (process.info.get("name") or "").lower() == name_lower:
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

    def _log_reader_issue(self, message: str):
        now = time.monotonic()
        self._last_reader_error = message
        if now - self._last_reader_error_log_monotonic >= 10.0:
            logger.debug("RTSS shared memory unavailable: %s", message)
            self._last_reader_error_log_monotonic = now
