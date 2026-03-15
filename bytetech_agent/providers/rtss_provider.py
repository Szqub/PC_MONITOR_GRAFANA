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
RTSS_SIGNATURE = int.from_bytes(b"RTSS", "big")
RTSS_SIGNATURE_LEGACY = int.from_bytes(b"RTSS", "little")
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
kernel32.VirtualQuery.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
kernel32.VirtualQuery.restype = ctypes.c_size_t


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


class RTSSSharedMemoryAppEntryPrefix(ctypes.Structure):
    _fields_ = [
        ("dwProcessID", ctypes.c_uint32),
        ("szProcessName", ctypes.c_char * MAX_PATH_CHARS),
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
        ("dwStatFramerateMin", ctypes.c_uint32),
        ("dwStatFramerateAvg", ctypes.c_uint32),
        ("dwStatFramerateMax", ctypes.c_uint32),
    ]


class RTSSSharedMemoryAppEntryLegacyGuess(ctypes.Structure):
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
    ]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.wintypes.DWORD),
        ("Protect", ctypes.wintypes.DWORD),
        ("Type", ctypes.wintypes.DWORD),
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
    mapping_name: Optional[str] = None


@dataclass(frozen=True)
class RtssHeaderInfo:
    signature: int
    version: int
    app_entry_size: int
    app_arr_offset: int
    app_arr_size: int
    osd_entry_size: int
    osd_arr_offset: int
    osd_arr_size: int
    osd_frame: int


@dataclass(frozen=True)
class RtssAppRecord:
    pid: int
    process_name: str
    fps: float
    frametime_ms: float
    source_quality: str
    last_tick_ms: int


@dataclass(frozen=True)
class RtssEntryDiagnostic:
    index: int
    pid: int
    process_name: str
    profile_name: str
    fps: float
    frametime_ms: float
    source_quality: str
    sample_tick_ms: int
    age_ms: Optional[int]
    kept: bool
    reject_reason: Optional[str]
    raw_fields: Dict[str, int]
    field_offsets: Dict[str, int]
    hexdumps: Dict[str, str]


@dataclass(frozen=True)
class RtssProbeResult:
    mapping_name: str
    mapping_found: bool
    mapping_size: int
    status: str
    error: Optional[str]
    header: Optional[RtssHeaderInfo]
    entry_diagnostics: List[RtssEntryDiagnostic]

    @property
    def kept_entries(self) -> List[RtssAppRecord]:
        entries = []
        for entry in self.entry_diagnostics:
            if not entry.kept:
                continue
            entries.append(
                RtssAppRecord(
                    pid=entry.pid,
                    process_name=entry.process_name,
                    fps=entry.fps,
                    frametime_ms=entry.frametime_ms,
                    source_quality=entry.source_quality,
                    last_tick_ms=entry.sample_tick_ms,
                )
            )
        return entries


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
        self._last_status_log_monotonic = 0.0

    def read_entries(self) -> RtssReadResult:
        probe = self.read_probe()
        if probe.status == "ok":
            return RtssReadResult(
                status="ok",
                entries=probe.kept_entries,
                mapping_name=probe.mapping_name,
            )
        return RtssReadResult(
            status=probe.status,
            entries=[],
            error=probe.error,
            mapping_name=probe.mapping_name,
        )

    def read_probe(self) -> RtssProbeResult:
        probes = self.probe_mappings()
        for probe in probes:
            if probe.status == "ok":
                return probe

        attempted_names = ",".join(probe.mapping_name for probe in probes)
        opened_results = [probe for probe in probes if probe.mapping_found]
        if opened_results:
            return opened_results[-1]

        return RtssProbeResult(
            mapping_name=attempted_names,
            mapping_found=False,
            mapping_size=0,
            status="mapping_unavailable",
            error=f"RTSS shared memory mapping is not available. attempted_names={attempted_names}",
            header=None,
            entry_diagnostics=[],
        )

    def probe_mappings(
        self,
        inspect_entry_index: Optional[int] = None,
        inspect_pid: Optional[int] = None,
    ) -> List[RtssProbeResult]:
        return [
            self._probe_mapping(mapping_name, inspect_entry_index=inspect_entry_index, inspect_pid=inspect_pid)
            for mapping_name in self._candidate_mapping_names()
        ]

    def _candidate_mapping_names(self) -> List[str]:
        base_names = [
            self._shared_memory_name,
            "RTSSSharedMemoryV2",
            "RTSSSharedMemory",
        ]
        candidates = []
        for base_name in base_names:
            if not base_name:
                continue
            candidates.append(base_name)
            candidates.append(f"Global\\{base_name}")
            candidates.append(f"Local\\{base_name}")
        seen = []
        for candidate in candidates:
            if candidate not in seen:
                seen.append(candidate)
        return seen

    def _probe_mapping(
        self,
        mapping_name: str,
        inspect_entry_index: Optional[int] = None,
        inspect_pid: Optional[int] = None,
    ) -> RtssProbeResult:
        mapping = kernel32.OpenFileMappingW(FILE_MAP_READ, False, mapping_name)
        if not mapping:
            return RtssProbeResult(
                mapping_name=mapping_name,
                mapping_found=False,
                mapping_size=0,
                status="mapping_unavailable",
                error=None,
                header=None,
                entry_diagnostics=[],
            )

        view = kernel32.MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, 0)
        if not view:
            kernel32.CloseHandle(mapping)
            return RtssProbeResult(
                mapping_name=mapping_name,
                mapping_found=True,
                mapping_size=0,
                status="map_failed",
                error=f"Failed to map RTSS shared memory '{mapping_name}'.",
                header=None,
                entry_diagnostics=[],
            )

        try:
            mapping_size = self._query_view_region_size(view)
            result = self._parse_view(
                view,
                mapping_name,
                mapping_size,
                inspect_entry_index=inspect_entry_index,
                inspect_pid=inspect_pid,
            )
            if result.status != "ok":
                self._log_debug_once(
                    "RTSS mapping '%s' opened but parse failed: %s",
                    mapping_name,
                    result.error or result.status,
                )
            return result
        finally:
            kernel32.UnmapViewOfFile(view)
            kernel32.CloseHandle(mapping)

    def _parse_view(
        self,
        view: int,
        mapping_name: str,
        mapping_size: int = 0,
        inspect_entry_index: Optional[int] = None,
        inspect_pid: Optional[int] = None,
    ) -> RtssProbeResult:
        header = RTSSSharedMemoryHeader.from_address(view)
        header_info = RtssHeaderInfo(
            signature=header.dwSignature,
            version=header.dwVersion,
            app_entry_size=header.dwAppEntrySize,
            app_arr_offset=header.dwAppArrOffset,
            app_arr_size=header.dwAppArrSize,
            osd_entry_size=header.dwOSDEntrySize,
            osd_arr_offset=header.dwOSDArrOffset,
            osd_arr_size=header.dwOSDArrSize,
            osd_frame=header.dwOSDFrame,
        )
        if header.dwSignature not in (RTSS_SIGNATURE, RTSS_SIGNATURE_LEGACY):
            return RtssProbeResult(
                mapping_name=mapping_name,
                mapping_found=True,
                mapping_size=mapping_size,
                status="invalid_header",
                error=(
                    f"Unexpected RTSS header in mapping '{mapping_name}': "
                    f"signature=0x{header.dwSignature:08X} version=0x{header.dwVersion:08X} "
                    f"app_entry_size={header.dwAppEntrySize} app_arr_offset={header.dwAppArrOffset} "
                    f"app_arr_size={header.dwAppArrSize} expected_signatures="
                    f"[0x{RTSS_SIGNATURE:08X},0x{RTSS_SIGNATURE_LEGACY:08X}]"
                ),
                header=header_info,
                entry_diagnostics=[],
            )

        min_entry_size = ctypes.sizeof(RTSSSharedMemoryAppEntryPrefix)
        if header.dwAppEntrySize < min_entry_size:
            return RtssProbeResult(
                mapping_name=mapping_name,
                mapping_found=True,
                mapping_size=mapping_size,
                status="unsupported_layout",
                error=(
                    f"RTSS app entry size {header.dwAppEntrySize} is smaller than expected "
                    f"{min_entry_size} for required prefix layout."
                ),
                header=header_info,
                entry_diagnostics=[],
            )

        bounds_error = self._validate_app_bounds(header, mapping_size)
        if bounds_error:
            return RtssProbeResult(
                mapping_name=mapping_name,
                mapping_found=True,
                mapping_size=mapping_size,
                status="out_of_bounds",
                error=bounds_error,
                header=header_info,
                entry_diagnostics=[],
            )

        current_tick_ms = int(kernel32.GetTickCount64() & 0xFFFFFFFF)
        entry_diagnostics: List[RtssEntryDiagnostic] = []
        for index in range(header.dwAppArrSize):
            entry_address = view + header.dwAppArrOffset + index * header.dwAppEntrySize
            entry = RTSSSharedMemoryAppEntryPrefix.from_address(entry_address)
            legacy_entry = RTSSSharedMemoryAppEntryLegacyGuess.from_address(entry_address)
            process_name = self._decode_c_string(entry.szProcessName)
            fps, source_quality = self._extract_fps(header.dwVersion, entry)
            frametime_ms = self._extract_frametime_ms(entry, fps)
            age_ms = self._compute_age_ms(current_tick_ms, entry.dwTime1)
            reject_reason = self._classify_entry_rejection(
                pid=entry.dwProcessID,
                process_name=process_name,
                fps=fps,
                age_ms=age_ms,
            )
            include_detail = (inspect_entry_index is not None and inspect_entry_index == index) or (
                inspect_pid is not None and inspect_pid == entry.dwProcessID
            )
            hexdumps = self._build_entry_hexdumps(entry_address, header.dwAppEntrySize) if include_detail else {}
            entry_diagnostics.append(
                RtssEntryDiagnostic(
                    index=index,
                    pid=entry.dwProcessID,
                    process_name=process_name,
                    profile_name="",
                    fps=round(fps, 2),
                    frametime_ms=frametime_ms,
                    source_quality=source_quality,
                    sample_tick_ms=entry.dwTime1,
                    age_ms=age_ms,
                    kept=reject_reason is None,
                    reject_reason=reject_reason,
                    raw_fields={
                        "dwFlags": entry.dwFlags,
                        "dwTime0": entry.dwTime0,
                        "dwTime1": entry.dwTime1,
                        "dwFrames": entry.dwFrames,
                        "dwFrameTime": entry.dwFrameTime,
                        "dwStatFlags": entry.dwStatFlags,
                        "dwStatTime0": entry.dwStatTime0,
                        "dwStatTime1": entry.dwStatTime1,
                        "dwStatFrames": entry.dwStatFrames,
                        "dwStatCount": entry.dwStatCount,
                        "dwStatFramerateMin": entry.dwStatFramerateMin,
                        "dwStatFramerateAvg": entry.dwStatFramerateAvg,
                        "dwStatFramerateMax": entry.dwStatFramerateMax,
                        "legacy_dwFlags": legacy_entry.dwFlags,
                        "legacy_dwTime0": legacy_entry.dwTime0,
                        "legacy_dwTime1": legacy_entry.dwTime1,
                        "legacy_dwFrames": legacy_entry.dwFrames,
                        "legacy_dwFrameTime": legacy_entry.dwFrameTime,
                        "legacy_dwStatFlags": legacy_entry.dwStatFlags,
                        "legacy_dwStatTime0": legacy_entry.dwStatTime0,
                        "legacy_dwStatTime1": legacy_entry.dwStatTime1,
                        "legacy_dwStatFrames": legacy_entry.dwStatFrames,
                        "legacy_dwStatCount": legacy_entry.dwStatCount,
                        "legacy_dwStatFramerate": legacy_entry.dwStatFramerate,
                        "legacy_dwStatFrameTime": legacy_entry.dwStatFrameTime,
                        "legacy_dwStatFrameTimeBufFramerate": legacy_entry.dwStatFrameTimeBufFramerate,
                    },
                    field_offsets=self._field_offsets(),
                    hexdumps=hexdumps,
                )
            )

        kept_count = sum(1 for entry in entry_diagnostics if entry.kept)
        skipped_zero_pid = sum(1 for entry in entry_diagnostics if entry.reject_reason == "zero_pid")
        skipped_no_name = sum(1 for entry in entry_diagnostics if entry.reject_reason == "empty_name")
        skipped_no_fps = sum(1 for entry in entry_diagnostics if entry.reject_reason == "zero_fps")
        skipped_stale = sum(1 for entry in entry_diagnostics if entry.reject_reason == "stale")
        self._log_debug_once(
            "RTSS mapping '%s' parsed. version=0x%08X app_arr_size=%s kept=%s "
            "skipped_zero_pid=%s skipped_no_name=%s skipped_no_fps=%s skipped_stale=%s",
            mapping_name,
            header.dwVersion,
            header.dwAppArrSize,
            kept_count,
            skipped_zero_pid,
            skipped_no_name,
            skipped_no_fps,
            skipped_stale,
        )
        return RtssProbeResult(
            mapping_name=mapping_name,
            mapping_found=True,
            mapping_size=mapping_size,
            status="ok",
            error=None,
            header=header_info,
            entry_diagnostics=entry_diagnostics,
        )

    def _decode_c_string(self, buffer) -> str:
        return bytes(buffer).split(b"\0", 1)[0].decode("utf-8", errors="ignore").strip()

    def _extract_fps(self, version: int, entry: RTSSSharedMemoryAppEntryPrefix) -> tuple[float, str]:
        if entry.dwFrameTime > 0:
            return 1_000_000.0 / entry.dwFrameTime, "rtss_frame_time_instant"

        delta_ms = (entry.dwTime1 - entry.dwTime0) & 0xFFFFFFFF
        if delta_ms > 0 and entry.dwFrames > 0:
            fps = entry.dwFrames * 1000.0 / delta_ms
            return fps, "rtss_frame_counter_sampled"

        if entry.dwStatFramerateAvg > 0:
            return self._normalize_stat_fps(entry.dwStatFramerateAvg), "rtss_stat_framerate_avg"

        return 0.0, "rtss_no_fps"

    def _extract_frametime_ms(self, entry: RTSSSharedMemoryAppEntryPrefix, fps: float) -> float:
        if entry.dwFrameTime > 0:
            return round(entry.dwFrameTime / 1000.0, 2)
        if fps > 0:
            return round(1000.0 / fps, 2)
        return 0.0

    def _normalize_stat_fps(self, value: int) -> float:
        if value >= 1000:
            return value / 10.0
        return float(value)

    def _compute_age_ms(self, current_tick_ms: int, sample_tick_ms: int) -> Optional[int]:
        if sample_tick_ms == 0:
            return None
        age = (current_tick_ms - sample_tick_ms) & 0xFFFFFFFF
        if age > 0x7FFFFFFF:
            return None
        return int(age)

    def _is_stale(self, current_tick_ms: int, sample_tick_ms: int) -> bool:
        age = self._compute_age_ms(current_tick_ms, sample_tick_ms)
        return age is not None and age > self._stale_timeout_ms

    def _classify_entry_rejection(
        self,
        pid: int,
        process_name: str,
        fps: float,
        age_ms: Optional[int],
    ) -> Optional[str]:
        if pid == 0:
            return "zero_pid"
        if not process_name:
            return "empty_name"
        if fps <= 0:
            return "zero_fps"
        if age_ms is not None and age_ms > self._stale_timeout_ms:
            return "stale"
        return None

    def _query_view_region_size(self, view: int) -> int:
        info = MEMORY_BASIC_INFORMATION()
        result = kernel32.VirtualQuery(
            ctypes.c_void_p(view),
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not result:
            return 0
        return int(info.RegionSize)

    def _field_offsets(self) -> Dict[str, int]:
        return {
            "dwFlags": RTSSSharedMemoryAppEntryPrefix.dwFlags.offset,
            "dwTime0": RTSSSharedMemoryAppEntryPrefix.dwTime0.offset,
            "dwTime1": RTSSSharedMemoryAppEntryPrefix.dwTime1.offset,
            "dwFrames": RTSSSharedMemoryAppEntryPrefix.dwFrames.offset,
            "dwFrameTime": RTSSSharedMemoryAppEntryPrefix.dwFrameTime.offset,
            "dwStatFlags": RTSSSharedMemoryAppEntryPrefix.dwStatFlags.offset,
            "dwStatTime0": RTSSSharedMemoryAppEntryPrefix.dwStatTime0.offset,
            "dwStatTime1": RTSSSharedMemoryAppEntryPrefix.dwStatTime1.offset,
            "dwStatFrames": RTSSSharedMemoryAppEntryPrefix.dwStatFrames.offset,
            "dwStatCount": RTSSSharedMemoryAppEntryPrefix.dwStatCount.offset,
            "dwStatFramerateMin": RTSSSharedMemoryAppEntryPrefix.dwStatFramerateMin.offset,
            "dwStatFramerateAvg": RTSSSharedMemoryAppEntryPrefix.dwStatFramerateAvg.offset,
            "dwStatFramerateMax": RTSSSharedMemoryAppEntryPrefix.dwStatFramerateMax.offset,
            "legacy_dwFlags": RTSSSharedMemoryAppEntryLegacyGuess.dwFlags.offset,
            "legacy_dwTime0": RTSSSharedMemoryAppEntryLegacyGuess.dwTime0.offset,
            "legacy_dwTime1": RTSSSharedMemoryAppEntryLegacyGuess.dwTime1.offset,
            "legacy_dwFrames": RTSSSharedMemoryAppEntryLegacyGuess.dwFrames.offset,
            "legacy_dwFrameTime": RTSSSharedMemoryAppEntryLegacyGuess.dwFrameTime.offset,
            "legacy_dwStatFlags": RTSSSharedMemoryAppEntryLegacyGuess.dwStatFlags.offset,
            "legacy_dwStatTime0": RTSSSharedMemoryAppEntryLegacyGuess.dwStatTime0.offset,
            "legacy_dwStatTime1": RTSSSharedMemoryAppEntryLegacyGuess.dwStatTime1.offset,
            "legacy_dwStatFrames": RTSSSharedMemoryAppEntryLegacyGuess.dwStatFrames.offset,
            "legacy_dwStatCount": RTSSSharedMemoryAppEntryLegacyGuess.dwStatCount.offset,
            "legacy_dwStatFramerate": RTSSSharedMemoryAppEntryLegacyGuess.dwStatFramerate.offset,
            "legacy_dwStatFrameTime": RTSSSharedMemoryAppEntryLegacyGuess.dwStatFrameTime.offset,
            "legacy_dwStatFrameTimeBufFramerate": RTSSSharedMemoryAppEntryLegacyGuess.dwStatFrameTimeBufFramerate.offset,
        }

    def _build_entry_hexdumps(self, entry_address: int, entry_size: int) -> Dict[str, str]:
        current_start = max(0, self._field_offsets()["dwTime0"] - 16)
        legacy_start = max(0, self._field_offsets()["legacy_dwTime0"] - 16)
        current_len = min(96, max(0, entry_size - current_start))
        legacy_len = min(96, max(0, entry_size - legacy_start))
        return {
            "current_stat_region": self._hexdump(entry_address, current_start, current_len),
            "legacy_guess_region": self._hexdump(entry_address, legacy_start, legacy_len),
        }

    def _hexdump(self, entry_address: int, start_offset: int, length: int) -> str:
        if length <= 0:
            return ""
        data = ctypes.string_at(entry_address + start_offset, length)
        lines = []
        for chunk_start in range(0, len(data), 16):
            chunk = data[chunk_start : chunk_start + 16]
            hex_bytes = " ".join(f"{byte:02X}" for byte in chunk)
            lines.append(f"+0x{start_offset + chunk_start:04X}: {hex_bytes}")
        return "\n".join(lines)

    def _validate_app_bounds(self, header: RTSSSharedMemoryHeader, mapping_size: int) -> Optional[str]:
        if mapping_size <= 0:
            return None

        header_size = ctypes.sizeof(RTSSSharedMemoryHeader)
        if mapping_size < header_size:
            return (
                f"RTSS mapping is smaller than header size. mapping_size={mapping_size} "
                f"header_size={header_size}"
            )

        if header.dwAppArrOffset < header_size:
            return (
                f"RTSS app array offset points inside header. app_arr_offset={header.dwAppArrOffset} "
                f"header_size={header_size}"
            )

        if header.dwAppArrOffset > mapping_size:
            return (
                f"RTSS app array offset is outside mapping. app_arr_offset={header.dwAppArrOffset} "
                f"mapping_size={mapping_size}"
            )

        total_app_bytes = header.dwAppEntrySize * header.dwAppArrSize
        app_region_end = header.dwAppArrOffset + total_app_bytes
        if total_app_bytes < 0 or app_region_end > mapping_size:
            return (
                f"RTSS app array exceeds mapping bounds. app_arr_offset={header.dwAppArrOffset} "
                f"app_entry_size={header.dwAppEntrySize} app_arr_size={header.dwAppArrSize} "
                f"app_region_end={app_region_end} mapping_size={mapping_size}"
            )

        return None

    def _log_debug_once(self, message: str, *args):
        now = time.monotonic()
        if now - self._last_status_log_monotonic >= 5.0:
            logger.debug(message, *args)
            self._last_status_log_monotonic = now


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
        probe = self._reader.read_probe()
        now = time.monotonic()

        if probe.status != "ok":
            self._rolling.prune(now)
            self._log_reader_issue(probe.error or probe.status)
            return []

        result = RtssReadResult(
            status=probe.status,
            entries=probe.kept_entries,
            error=probe.error,
            mapping_name=probe.mapping_name,
        )
        rejected_counts = self._summarize_rejections(probe.entry_diagnostics)
        logger.debug(
            "RTSS read succeeded. mapping=%s mapping_size=%s kept=%s rejected=%s target=%s",
            probe.mapping_name,
            probe.mapping_size,
            len(result.entries),
            rejected_counts,
            target,
        )

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
            "RTSS metric values before MetricData: reason=%s fields=%s tags=%s target=%s entry_count=%s",
            reason,
            fields,
            tags,
            target,
            len(entries),
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

    def _summarize_rejections(self, entry_diagnostics: List[RtssEntryDiagnostic]) -> Dict[str, int]:
        summary = {
            "kept": 0,
            "zero_pid": 0,
            "empty_name": 0,
            "zero_fps": 0,
            "stale": 0,
        }
        for entry in entry_diagnostics:
            if entry.kept:
                summary["kept"] += 1
                continue
            if entry.reject_reason in summary:
                summary[entry.reject_reason] += 1
        return summary
