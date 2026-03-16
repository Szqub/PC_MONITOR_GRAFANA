"""
Official PresentMon Service API discovery and ctypes wrapper.

This module follows the documented PresentMon Service SDK model:
- load PresentMonAPI2.dll dynamically from the installed host
- optionally prefer PresentMonAPI2Loader.dll when present and export-compatible
- connect to the already running PresentMon service as a client

Reference:
https://raw.githubusercontent.com/GameTechDev/PresentMon/main/README-Service.md
https://raw.githubusercontent.com/GameTechDev/PresentMon/main/IntelPresentMon/PresentMonAPI2/PresentMonAPI.h
"""
from __future__ import annotations

import ctypes
import os
import shutil
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

PM_STATUS_SUCCESS = 0
PM_STATUS_ALREADY_TRACKING_PROCESS = 7
PM_STATUS_INSUFFICIENT_BUFFER = 11

PM_METRIC_CPU_FRAME_TIME = 8
PM_METRIC_CPU_BUSY = 9
PM_METRIC_DISPLAYED_FPS = 11
PM_METRIC_GPU_BUSY = 14
PM_METRIC_DISPLAY_LATENCY = 24
PM_METRIC_APPLICATION_FPS = 62

PM_STAT_AVG = 1
PM_MAX_PATH = 260

PREFERRED_LOADER_DLL = "PresentMonAPI2Loader.dll"
PREFERRED_RUNTIME_DLL = "PresentMonAPI2.dll"


class PresentMonApiError(RuntimeError):
    """Structured error raised by the PresentMon Service API wrapper."""


@dataclass(frozen=True)
class PresentMonApiPaths:
    sdk_dir: Optional[str] = None
    service_dir: Optional[str] = None
    api_loader_dll: Optional[str] = None
    api_runtime_dll: Optional[str] = None
    chosen_dll: Optional[str] = None


@dataclass(frozen=True)
class PresentMonApiSnapshot:
    application_fps: float
    displayed_fps: float
    frametime_ms: float
    cpu_busy_ms: Optional[float]
    gpu_busy_ms: Optional[float]
    display_latency_ms: Optional[float]

    @property
    def fps(self) -> float:
        return self.application_fps if self.application_fps > 0 else self.displayed_fps

    @property
    def usable(self) -> bool:
        return self.application_fps > 0 or self.displayed_fps > 0 or self.frametime_ms > 0


class PM_QUERY_ELEMENT(ctypes.Structure):
    _fields_ = [
        ("metric", ctypes.c_uint32),
        ("stat", ctypes.c_uint32),
        ("deviceId", ctypes.c_uint32),
        ("arrayIndex", ctypes.c_uint32),
        ("dataOffset", ctypes.c_uint64),
        ("dataSize", ctypes.c_uint64),
    ]


class PM_VERSION(ctypes.Structure):
    _fields_ = [
        ("major", ctypes.c_uint16),
        ("minor", ctypes.c_uint16),
        ("patch", ctypes.c_uint16),
        ("tag", ctypes.c_char * 22),
        ("hash", ctypes.c_char * 8),
        ("config", ctypes.c_char * 4),
    ]


def _normalize_dir(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    normalized = os.path.abspath(os.path.expandvars(str(path).strip()))
    return normalized if os.path.isdir(normalized) else None


def _normalize_file(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    normalized = os.path.abspath(os.path.expandvars(str(path).strip()))
    if not os.path.isfile(normalized):
        return None
    if _is_gui_only_presentmon_path(normalized):
        return None
    return normalized


def _is_gui_only_presentmon_path(path: str) -> bool:
    normalized = os.path.normcase(os.path.abspath(path))
    return normalized.endswith("presentmon.exe") and "presentmonapplication" in normalized


def _unique_paths(paths: Iterable[Optional[str]]) -> List[str]:
    seen = set()
    unique = []
    for path in paths:
        if not path:
            continue
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(os.path.abspath(path))
    return unique


def _search_path_for_file(filename: str) -> Optional[str]:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = _normalize_file(os.path.join(directory, filename))
        if candidate:
            return candidate
    return None


def _candidate_directories(config) -> List[str]:
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    env_sdk = os.environ.get("PRESENTMON_SDK_PATH")
    env_service_dir = os.environ.get("PRESENTMON_SERVICE_DIR")
    explicit_sdk = _normalize_dir(getattr(config, "sdk_path", None))
    explicit_service_dir = _normalize_dir(getattr(config, "service_dir", None))
    explicit_loader_dir = os.path.dirname(_normalize_file(getattr(config, "api_loader_dll", None)) or "")
    explicit_runtime_dir = os.path.dirname(_normalize_file(getattr(config, "api_runtime_dll", None)) or "")

    standard_root = os.path.join(program_files, "Intel", "PresentMon")
    service_dirs = [
        os.path.join(program_files, "Intel", "PresentMonSharedService"),
        os.path.join(program_files, "Intel", "PresentMonSharedServices"),
    ]

    return _unique_paths(
        [
            explicit_sdk,
            explicit_service_dir,
            _normalize_dir(explicit_loader_dir),
            _normalize_dir(explicit_runtime_dir),
            _normalize_dir(env_sdk),
            _normalize_dir(env_service_dir),
            _normalize_dir(os.path.join(standard_root, "SDK")),
            _normalize_dir(standard_root),
            *(_normalize_dir(path) for path in service_dirs),
        ]
    )


def resolve_presentmon_loader_dll(config) -> Optional[str]:
    explicit = _normalize_file(getattr(config, "api_loader_dll", None))
    if explicit:
        return explicit

    env_override = _normalize_file(os.environ.get("PRESENTMON_API_LOADER_DLL"))
    if env_override:
        return env_override

    for directory in _candidate_directories(config):
        candidate = _normalize_file(os.path.join(directory, PREFERRED_LOADER_DLL))
        if candidate:
            return candidate

    return _search_path_for_file(PREFERRED_LOADER_DLL)


def resolve_presentmon_runtime_dll(config) -> Optional[str]:
    explicit = _normalize_file(getattr(config, "api_runtime_dll", None))
    if explicit:
        return explicit

    env_override = _normalize_file(os.environ.get("PRESENTMON_API_RUNTIME_DLL"))
    if env_override:
        return env_override

    for directory in _candidate_directories(config):
        candidate = _normalize_file(os.path.join(directory, PREFERRED_RUNTIME_DLL))
        if candidate:
            return candidate

    return _search_path_for_file(PREFERRED_RUNTIME_DLL)


def resolve_presentmon_api_paths(config) -> PresentMonApiPaths:
    loader_dll = resolve_presentmon_loader_dll(config)
    runtime_dll = resolve_presentmon_runtime_dll(config)
    sdk_dir = _normalize_dir(getattr(config, "sdk_path", None))
    service_dir = _normalize_dir(getattr(config, "service_dir", None))

    if not sdk_dir and loader_dll:
        sdk_dir = os.path.dirname(loader_dll)
    if not service_dir and runtime_dll:
        service_dir = os.path.dirname(runtime_dll)

    chosen_dll = loader_dll or runtime_dll
    return PresentMonApiPaths(
        sdk_dir=sdk_dir,
        service_dir=service_dir,
        api_loader_dll=loader_dll,
        api_runtime_dll=runtime_dll,
        chosen_dll=chosen_dll,
    )


def validate_presentmon_installation(config) -> Dict[str, object]:
    paths = resolve_presentmon_api_paths(config)
    errors: List[str] = []

    explicit_loader = getattr(config, "api_loader_dll", None)
    explicit_runtime = getattr(config, "api_runtime_dll", None)

    if explicit_loader and not _normalize_file(explicit_loader):
        errors.append("Configured PresentMon API loader DLL path is invalid.")
    if explicit_runtime and not _normalize_file(explicit_runtime):
        errors.append("Configured PresentMon API runtime DLL path is invalid.")
    if not paths.chosen_dll:
        errors.append("No valid PresentMon API DLL was discovered on the host.")

    return {
        "ok": not errors,
        "errors": errors,
        "paths": paths,
    }


class PresentMonServiceApiClient:
    """Thin ctypes wrapper over the official PresentMon Service API."""

    def __init__(self, config):
        self._config = config
        self._paths = resolve_presentmon_api_paths(config)
        self._dll = None
        self._session = ctypes.c_void_p()
        self._query = ctypes.c_void_p()
        self._blob_size = 0
        self._max_swap_chains = 8
        self._dll_dir_handles = []
        self._tracked_pid: Optional[int] = None
        self._query_elements = self._build_query_elements()

    @property
    def paths(self) -> PresentMonApiPaths:
        return self._paths

    def open(self):
        if not self._paths.chosen_dll:
            raise PresentMonApiError("PresentMon API DLL was not discovered.")

        self._add_dll_search_dirs()
        try:
            self._dll = ctypes.WinDLL(self._paths.chosen_dll)
            self._bind_functions()
        except Exception as exc:
            raise PresentMonApiError(f"Unable to load PresentMon API DLL '{self._paths.chosen_dll}': {exc}") from exc

        self._call_status("pmOpenSession", self._dll.pmOpenSession(ctypes.byref(self._session)))
        poll_ms = int(getattr(self._config, "poll_interval_ms", 250) or 250)
        self._call_status(
            "pmSetTelemetryPollingPeriod",
            self._dll.pmSetTelemetryPollingPeriod(self._session, 0, poll_ms),
            allow_failure=True,
        )
        self._register_query()

    def close(self):
        if self._dll and self._tracked_pid:
            self._dll.pmStopTrackingProcess(self._session, ctypes.c_uint32(self._tracked_pid))
        if self._dll and self._query:
            self._dll.pmFreeDynamicQuery(self._query)
        if self._dll and self._session:
            self._dll.pmCloseSession(self._session)
        self._tracked_pid = None
        self._query = ctypes.c_void_p()
        self._session = ctypes.c_void_p()
        for handle in self._dll_dir_handles:
            try:
                handle.close()
            except Exception:
                pass
        self._dll_dir_handles = []

    def get_api_version_string(self) -> Optional[str]:
        if not self._dll:
            return None
        version = PM_VERSION()
        status = self._dll.pmGetApiVersion(ctypes.byref(version))
        if status != PM_STATUS_SUCCESS:
            return None
        tag = bytes(version.tag).split(b"\0", 1)[0].decode("utf-8", errors="ignore")
        return f"{version.major}.{version.minor}.{version.patch}{tag}"

    def ensure_tracking(self, pid: int):
        if pid <= 0:
            raise PresentMonApiError(f"Invalid target pid: {pid}")
        if self._tracked_pid == pid:
            return
        if self._tracked_pid:
            self._dll.pmStopTrackingProcess(self._session, ctypes.c_uint32(self._tracked_pid))
        status = self._dll.pmStartTrackingProcess(self._session, ctypes.c_uint32(pid))
        if status not in (PM_STATUS_SUCCESS, PM_STATUS_ALREADY_TRACKING_PROCESS):
            raise PresentMonApiError(f"pmStartTrackingProcess failed with status={status} for pid={pid}")
        self._tracked_pid = pid

    def poll_process(self, pid: int) -> Optional[PresentMonApiSnapshot]:
        self.ensure_tracking(pid)
        blob_size = self._blob_size * self._max_swap_chains
        blob = (ctypes.c_uint8 * blob_size)()
        num_swap_chains = ctypes.c_uint32(self._max_swap_chains)
        status = self._dll.pmPollDynamicQuery(
            self._query,
            ctypes.c_uint32(pid),
            blob,
            ctypes.byref(num_swap_chains),
        )
        if status == PM_STATUS_INSUFFICIENT_BUFFER:
            self._max_swap_chains *= 2
            return self.poll_process(pid)
        self._call_status("pmPollDynamicQuery", status)
        if num_swap_chains.value == 0:
            return None

        samples = []
        for swap_chain_index in range(num_swap_chains.value):
            base = swap_chain_index * self._blob_size
            application_fps = self._read_double(blob, base + 0)
            displayed_fps = self._read_double(blob, base + 8)
            cpu_frame_time_us = self._read_double(blob, base + 16)
            cpu_busy_us = self._read_double(blob, base + 24)
            gpu_busy_us = self._read_double(blob, base + 32)
            display_latency_us = self._read_double(blob, base + 40)
            fps = application_fps if application_fps > 0 else displayed_fps
            frametime_ms = cpu_frame_time_us / 1000.0 if cpu_frame_time_us > 0 else (1000.0 / fps if fps > 0 else 0.0)
            snapshot = PresentMonApiSnapshot(
                application_fps=round(application_fps, 2) if application_fps > 0 else 0.0,
                displayed_fps=round(displayed_fps, 2) if displayed_fps > 0 else 0.0,
                frametime_ms=round(frametime_ms, 2) if frametime_ms > 0 else 0.0,
                cpu_busy_ms=self._to_milliseconds(cpu_busy_us),
                gpu_busy_ms=self._to_milliseconds(gpu_busy_us),
                display_latency_ms=self._to_milliseconds(display_latency_us),
            )
            if snapshot.usable:
                samples.append(snapshot)

        if not samples:
            return None

        samples.sort(
            key=lambda sample: (sample.application_fps, sample.displayed_fps, sample.frametime_ms),
            reverse=True,
        )
        return samples[0]

    def _add_dll_search_dirs(self):
        for directory in _unique_paths(
            [
                self._paths.sdk_dir,
                self._paths.service_dir,
                os.path.dirname(self._paths.api_loader_dll) if self._paths.api_loader_dll else None,
                os.path.dirname(self._paths.api_runtime_dll) if self._paths.api_runtime_dll else None,
            ]
        ):
            if hasattr(os, "add_dll_directory") and os.path.isdir(directory):
                self._dll_dir_handles.append(os.add_dll_directory(directory))

    def _bind_functions(self):
        self._dll.pmOpenSession.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self._dll.pmOpenSession.restype = ctypes.c_uint32
        self._dll.pmCloseSession.argtypes = [ctypes.c_void_p]
        self._dll.pmCloseSession.restype = ctypes.c_uint32
        self._dll.pmGetApiVersion.argtypes = [ctypes.POINTER(PM_VERSION)]
        self._dll.pmGetApiVersion.restype = ctypes.c_uint32
        self._dll.pmSetTelemetryPollingPeriod.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
        self._dll.pmSetTelemetryPollingPeriod.restype = ctypes.c_uint32
        self._dll.pmStartTrackingProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._dll.pmStartTrackingProcess.restype = ctypes.c_uint32
        self._dll.pmStopTrackingProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._dll.pmStopTrackingProcess.restype = ctypes.c_uint32
        self._dll.pmRegisterDynamicQuery.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(PM_QUERY_ELEMENT),
            ctypes.c_uint64,
            ctypes.c_double,
            ctypes.c_double,
        ]
        self._dll.pmRegisterDynamicQuery.restype = ctypes.c_uint32
        self._dll.pmFreeDynamicQuery.argtypes = [ctypes.c_void_p]
        self._dll.pmFreeDynamicQuery.restype = ctypes.c_uint32
        self._dll.pmPollDynamicQuery.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self._dll.pmPollDynamicQuery.restype = ctypes.c_uint32

    def _register_query(self):
        elements_array_type = PM_QUERY_ELEMENT * len(self._query_elements)
        elements = elements_array_type(*self._query_elements)
        self._blob_size = max(int(element.dataOffset + element.dataSize) for element in self._query_elements)
        self._call_status(
            "pmRegisterDynamicQuery",
            self._dll.pmRegisterDynamicQuery(
                self._session,
                ctypes.byref(self._query),
                elements,
                len(self._query_elements),
                float(getattr(self._config, "poll_interval_ms", 250) or 250),
                0.0,
            ),
        )

    def _build_query_elements(self) -> List[PM_QUERY_ELEMENT]:
        query_spec = [
            (PM_METRIC_APPLICATION_FPS, 0),
            (PM_METRIC_DISPLAYED_FPS, 8),
            (PM_METRIC_CPU_FRAME_TIME, 16),
            (PM_METRIC_CPU_BUSY, 24),
            (PM_METRIC_GPU_BUSY, 32),
            (PM_METRIC_DISPLAY_LATENCY, 40),
        ]
        elements = []
        for metric_id, offset in query_spec:
            elements.append(
                PM_QUERY_ELEMENT(
                    metric=metric_id,
                    stat=PM_STAT_AVG,
                    deviceId=0,
                    arrayIndex=0,
                    dataOffset=offset,
                    dataSize=8,
                )
            )
        return elements

    def _read_double(self, blob, offset: int) -> float:
        return ctypes.c_double.from_buffer_copy(bytes(blob[offset : offset + 8])).value

    def _to_milliseconds(self, microseconds: float) -> Optional[float]:
        if microseconds <= 0:
            return None
        return round(microseconds / 1000.0, 2)

    def _call_status(self, function_name: str, status: int, allow_failure: bool = False):
        if status == PM_STATUS_SUCCESS:
            return
        if allow_failure:
            return
        raise PresentMonApiError(f"{function_name} failed with status={status}")
