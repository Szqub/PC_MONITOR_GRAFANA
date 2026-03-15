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
    RtssEntryDiagnostic,
    RtssProbeResult,
    RTSSSharedMemoryAppEntryPrefix,
    RTSSSharedMemoryHeader,
    RtssHeaderInfo,
    RtssProvider,
    RtssSharedMemoryReader,
)
from bytetech_agent.tools.rtss_probe import render_probe_results


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


def _make_live_rtss_v2_buffer():
    mapping_size = 5578752
    header = RTSSSharedMemoryHeader()
    header.dwSignature = 0x52545353
    header.dwVersion = 0x00020015
    header.dwAppEntrySize = 12416
    header.dwAppArrOffset = 2396256
    header.dwAppArrSize = 256
    header.dwOSDEntrySize = 299520
    header.dwOSDArrOffset = 96
    header.dwOSDArrSize = 8
    header.dwOSDFrame = 7301

    blob = ctypes.create_string_buffer(mapping_size)
    ctypes.memmove(ctypes.addressof(blob), ctypes.addressof(header), ctypes.sizeof(header))

    entry = RTSSSharedMemoryAppEntryPrefix()
    entry.dwProcessID = 4242
    entry.szProcessName = b"game.exe"
    current_tick_ms = int(ctypes.windll.kernel32.GetTickCount64() & 0xFFFFFFFF)
    entry.dwTime0 = (current_tick_ms - 500) & 0xFFFFFFFF
    entry.dwTime1 = current_tick_ms
    entry.dwFrames = 30
    entry.dwStatFrameTimeBufFramerate = 600
    ctypes.memmove(
        ctypes.addressof(blob) + header.dwAppArrOffset,
        ctypes.addressof(entry),
        ctypes.sizeof(entry),
    )
    return blob, mapping_size


def test_rtss_reader_parses_single_entry():
    reader = RtssSharedMemoryReader(shared_memory_name="RTSSSharedMemoryV2", stale_timeout_ms=5000)
    blob = _make_rtss_buffer()

    result = reader._parse_view(ctypes.addressof(blob), "RTSSSharedMemoryV2")

    assert result.status == "ok"
    assert len(result.kept_entries) == 1
    assert result.kept_entries[0].pid == 4242
    assert result.kept_entries[0].process_name == "game.exe"
    assert result.kept_entries[0].fps == 60.0
    assert result.kept_entries[0].frametime_ms > 0


def test_rtss_reader_probe_marks_stale_entry_as_rejected():
    reader = RtssSharedMemoryReader(shared_memory_name="RTSSSharedMemoryV2", stale_timeout_ms=1000)
    blob = _make_rtss_buffer()
    header = RTSSSharedMemoryHeader.from_address(ctypes.addressof(blob))
    entry_address = ctypes.addressof(blob) + header.dwAppArrOffset
    entry = RTSSSharedMemoryAppEntryPrefix.from_address(entry_address)
    current_tick_ms = int(ctypes.windll.kernel32.GetTickCount64() & 0xFFFFFFFF)
    entry.dwTime0 = (current_tick_ms - 6000) & 0xFFFFFFFF
    entry.dwTime1 = (current_tick_ms - 5000) & 0xFFFFFFFF

    result = reader._parse_view(ctypes.addressof(blob), "RTSSSharedMemoryV2")

    assert result.status == "ok"
    assert len(result.entry_diagnostics) == 1
    assert result.entry_diagnostics[0].kept is False
    assert result.entry_diagnostics[0].reject_reason == "stale"
    assert result.kept_entries == []


def test_rtss_reader_accepts_live_rtss_v2_header_when_bounds_are_safe():
    reader = RtssSharedMemoryReader(shared_memory_name="RTSSSharedMemoryV2", stale_timeout_ms=5000)
    blob, mapping_size = _make_live_rtss_v2_buffer()

    result = reader._parse_view(ctypes.addressof(blob), "RTSSSharedMemoryV2", mapping_size)

    assert result.status == "ok"
    assert result.header is not None
    assert result.header.signature == 0x52545353
    assert result.header.version == 0x00020015
    assert result.header.app_entry_size == 12416
    assert result.header.app_arr_offset == 2396256
    assert result.header.app_arr_size == 256
    assert len(result.entry_diagnostics) == 256
    assert result.kept_entries[0].pid == 4242
    assert result.kept_entries[0].process_name == "game.exe"


def test_rtss_provider_emits_pc_fps_metric_from_entries():
    provider = RtssProvider(
        fps_config=SimpleNamespace(backend="rtss_shared_memory"),
        rtss_config=SimpleNamespace(shared_memory_name="RTSSSharedMemoryV2", stale_timeout_ms=5000),
        presentmon_config=SimpleNamespace(target_mode="explicit_process_id", process_name=None, process_id=4242),
    )
    provider.initialize()
    provider._reader.read_probe = lambda: SimpleNamespace(
        status="ok",
        kept_entries=[
            SimpleNamespace(
                pid=4242,
                process_name="game.exe",
                fps=72.0,
                frametime_ms=13.89,
                source_quality="rtss_ring_buffer_sampled",
                last_tick_ms=1,
            )
        ],
        entry_diagnostics=[],
        error=None,
        mapping_name="RTSSSharedMemoryV2",
        mapping_size=4096,
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


def test_rtss_probe_renderer_outputs_entry_decision_and_raw_fields():
    rendered = render_probe_results(
        [
            RtssProbeResult(
                mapping_name="RTSSSharedMemoryV2",
                mapping_found=True,
                mapping_size=4096,
                status="ok",
                error=None,
                header=RtssHeaderInfo(
                    signature=RTSS_SIGNATURE,
                    version=RTSS_RING_BUFFER_VERSION,
                    app_entry_size=1232,
                    app_arr_offset=36,
                    app_arr_size=1,
                    osd_entry_size=0,
                    osd_arr_offset=0,
                    osd_arr_size=0,
                    osd_frame=0,
                ),
                entry_diagnostics=[
                    RtssEntryDiagnostic(
                        index=0,
                        pid=4242,
                        process_name="game.exe",
                        profile_name="game",
                        fps=60.0,
                        frametime_ms=16.67,
                        source_quality="rtss_ring_buffer_sampled",
                        sample_tick_ms=123,
                        age_ms=12,
                        kept=False,
                        reject_reason="zero_fps",
                        raw_fields={"dwFrames": 30, "dwStatFrameTimeBufFramerate": 600},
                    )
                ],
            )
        ]
    )

    assert "mapping=RTSSSharedMemoryV2" in rendered
    assert "decision=rejected" in rendered
    assert "reason=zero_fps" in rendered
    assert "raw_fields dwFrames=30 dwStatFrameTimeBufFramerate=600" in rendered


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
