"""Tests for RTSS provider parsing and FPS backend fallback routing."""
import ctypes
from types import SimpleNamespace

from bytetech_agent.models.metrics import MetricData, ProviderContext, ProviderStatus
from bytetech_agent.providers.base import BaseProvider
from bytetech_agent.providers.fps_provider import FpsProvider
from bytetech_agent.providers.presentmon_provider import PresentMonProvider
from bytetech_agent.providers.rtss_provider import (
    RTSS_RING_BUFFER_VERSION,
    RTSS_SIGNATURE,
    RTSSSharedMemoryAppEntryPrefix,
    RTSSSharedMemoryHeader,
    RtssProvider,
    RtssSharedMemoryReader,
)


def _make_rtss_buffer(pid=4242, process_name="game.exe", framerate_tenths=600):
    header = RTSSSharedMemoryHeader()
    header.dwSignature = RTSS_SIGNATURE
    header.dwVersion = RTSS_RING_BUFFER_VERSION
    header.dwAppEntrySize = ctypes.sizeof(RTSSSharedMemoryAppEntryPrefix)
    header.dwAppArrOffset = ctypes.sizeof(RTSSSharedMemoryHeader)
    header.dwAppArrSize = 1
    header.dwOSDEntrySize = 0
    header.dwOSDArrOffset = 0
    header.dwOSDArrSize = 0
    header.dwOSDFrame = 0

    entry = RTSSSharedMemoryAppEntryPrefix()
    entry.dwProcessID = pid
    entry.szProcessName = process_name.encode("ascii")
    current_tick_ms = int(ctypes.windll.kernel32.GetTickCount64() & 0xFFFFFFFF)
    entry.dwTime0 = (current_tick_ms - 500) & 0xFFFFFFFF
    entry.dwTime1 = current_tick_ms
    entry.dwFrames = 30
    entry.dwStatFrameTimeBufFramerate = framerate_tenths

    total_size = ctypes.sizeof(header) + ctypes.sizeof(entry)
    blob = ctypes.create_string_buffer(total_size)
    ctypes.memmove(ctypes.addressof(blob), ctypes.addressof(header), ctypes.sizeof(header))
    ctypes.memmove(
        ctypes.addressof(blob) + ctypes.sizeof(header),
        ctypes.addressof(entry),
        ctypes.sizeof(entry),
    )
    return blob


def test_rtss_reader_parses_single_entry():
    reader = RtssSharedMemoryReader(shared_memory_name="RTSSSharedMemoryV2", stale_timeout_ms=5000)
    blob = _make_rtss_buffer()

    result = reader._parse_view(ctypes.addressof(blob), "RTSSSharedMemoryV2")

    assert result.status == "ok"
    assert len(result.entries) == 1
    assert result.entries[0].pid == 4242
    assert result.entries[0].process_name == "game.exe"
    assert result.entries[0].fps == 60.0
    assert result.entries[0].frametime_ms > 0


def test_rtss_provider_emits_pc_fps_metric_from_entries():
    provider = RtssProvider(
        fps_config=SimpleNamespace(backend="rtss_shared_memory"),
        rtss_config=SimpleNamespace(shared_memory_name="RTSSSharedMemoryV2", stale_timeout_ms=5000),
        presentmon_config=SimpleNamespace(target_mode="explicit_process_id", process_name=None, process_id=4242),
    )
    provider.initialize()
    provider._reader.read_entries = lambda: SimpleNamespace(
        status="ok",
        entries=[
            SimpleNamespace(
                pid=4242,
                process_name="game.exe",
                fps=72.0,
                frametime_ms=13.89,
                source_quality="rtss_ring_buffer_sampled",
                last_tick_ms=1,
            )
        ],
    )
    context = ProviderContext(host_alias="PC1", host_name="pc1")

    metrics = provider.get_metrics(context)

    assert len(metrics) == 1
    metric = metrics[0]
    assert metric.measurement_name == "pc_fps"
    assert metric.tags["backend"] == "rtss_shared_memory"
    assert metric.tags["pid"] == "4242"
    assert metric.fields["fps_now"] > 0
    assert metric.fields["source_quality"] == "rtss_ring_buffer_sampled"


class StubMetricsProvider(BaseProvider):
    def __init__(self, name, metrics):
        super().__init__(name=name)
        self._metrics = metrics

    def initialize(self):
        self._health.status = ProviderStatus.AVAILABLE
        return True

    def _collect(self, context):
        return self._metrics

    def shutdown(self):
        return None


def test_fps_router_uses_fallback_when_primary_returns_no_metrics():
    router = FpsProvider(
        fps_config=SimpleNamespace(backend="rtss_shared_memory", fallback_backend="presentmon_console"),
        rtss_config=SimpleNamespace(shared_memory_name="RTSSSharedMemoryV2", stale_timeout_ms=2000),
        presentmon_config=SimpleNamespace(target_mode="active_foreground", process_name=None, process_id=None),
    )
    router._primary = StubMetricsProvider(name="RTSS", metrics=[])
    router._fallback = StubMetricsProvider(
        name="PresentMon",
        metrics=[
            MetricData(
                measurement_name="pc_fps",
                tags={"backend": "presentmon_console_stdout"},
                fields={"fps_now": 60.0},
            )
        ],
    )
    router._primary.initialize()
    router._fallback.initialize()
    router._health.status = ProviderStatus.AVAILABLE
    context = ProviderContext(host_alias="PC1", host_name="pc1")

    metrics = router.get_metrics(context)

    assert len(metrics) == 1
    assert metrics[0].tags["backend"] == "presentmon_console_stdout"


def test_presentmon_provider_rejects_gui_path():
    provider = PresentMonProvider(
        SimpleNamespace(
            target_mode="active_foreground",
            process_name=None,
            process_id=None,
            executable_path=r"C:\Program Files\Intel\PresentMon\PresentMonApplication\PresentMon.exe",
        )
    )

    assert provider.initialize() is False
