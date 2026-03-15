"""Tests for PresentMon console stdout provider helpers."""
from types import SimpleNamespace

from bytetech_agent.models.metrics import ProviderContext
from bytetech_agent.providers.presentmon_provider import (
    PresentMonCsvParser,
    PresentMonProvider,
    PresentMonTarget,
    RollingProcessStats,
)


def test_parser_accepts_v2_header_and_row():
    parser = PresentMonCsvParser()
    assert parser.parse_line("Application,ProcessID,FrameTime,CPUBusy,GPUBusy,DisplayLatency,PresentMode") is None

    sample = parser.parse_line(
        "game.exe,4242,16.6,4.0,9.0,21.5,Hardware: Independent Flip"
    )

    assert sample is not None
    assert sample.process_name == "game.exe"
    assert sample.pid == 4242
    assert sample.frametime_ms == 16.6
    assert sample.cpu_busy_ms == 4.0
    assert sample.gpu_busy_ms == 9.0
    assert sample.display_latency_ms == 21.5


def test_parser_accepts_v1_header_and_row():
    parser = PresentMonCsvParser()
    assert parser.parse_line("Application,ProcessID,MsBetweenPresents,MsCPUBusy,MsGPUBusy,MsUntilDisplayed,PresentMode") is None

    sample = parser.parse_line(
        "dwm.exe,111,33.3,3.0,8.0,40.0,Composed: Flip"
    )

    assert sample is not None
    assert sample.process_name == "dwm.exe"
    assert sample.pid == 111
    assert sample.frametime_ms == 33.3
    assert sample.cpu_busy_ms == 3.0
    assert sample.gpu_busy_ms == 8.0
    assert sample.display_latency_ms == 40.0


def test_rolling_stats_compute_required_fields():
    stats = RollingProcessStats(pid=777, process_name="game.exe")

    for _ in range(30):
        parser = PresentMonCsvParser()
        parser.parse_line("Application,ProcessID,FrameTime,CPUBusy,GPUBusy,DisplayLatency,PresentMode")
        frame = parser.parse_line(
            "game.exe,777,16.6667,4.2,9.5,20.0,Hardware: Independent Flip"
        )
        stats.add_sample(frame)

    snapshot = stats.snapshot()

    assert snapshot["fps_now"] > 0
    assert snapshot["frametime_ms_now"] > 0
    assert snapshot["fps_avg_10s"] > 0
    assert snapshot["fps_avg_30s"] > 0
    assert snapshot["fps_1pct_30s"] > 0
    assert snapshot["fps_0_1pct_30s"] > 0
    assert snapshot["cpu_busy_ms"] > 0
    assert snapshot["gpu_busy_ms"] > 0
    assert snapshot["display_latency_ms"] > 0
    assert snapshot["present_mode"] == "Hardware: Independent Flip"


def test_provider_builds_zero_metric_when_target_missing():
    provider = PresentMonProvider(
        SimpleNamespace(
            target_mode="active_foreground",
            process_name="",
            process_id=0,
            executable_path=None,
        )
    )
    context = ProviderContext(host_alias="PC1", host_name="pc1")

    metric = provider._build_metric(context, None)

    assert metric.measurement_name == "pc_fps"
    assert metric.tags["backend"] == "presentmon_console_stdout"
    assert metric.fields["fps_now"] == 0.0
    assert metric.fields["fps_avg_30s"] == 0.0


def test_provider_selects_most_recent_pid_for_process_name_target():
    provider = PresentMonProvider(
        SimpleNamespace(
            target_mode="explicit_process_name",
            process_name="game.exe",
            process_id=0,
            executable_path=None,
        )
    )

    older = RollingProcessStats(pid=100, process_name="game.exe")
    newer = RollingProcessStats(pid=200, process_name="game.exe")

    parser = PresentMonCsvParser()
    parser.parse_line("Application,ProcessID,FrameTime")
    older.add_sample(parser.parse_line("game.exe,100,25.0"))
    newer.add_sample(parser.parse_line("game.exe,200,16.0"))
    older.last_sample_monotonic = 1.0
    newer.last_sample_monotonic = 2.0

    provider._stats_by_pid = {100: older, 200: newer}
    target = PresentMonTarget(
        mode="explicit_process_name",
        filter_kind="process_name",
        filter_value="game.exe",
        pid=0,
        process_name="game.exe",
    )

    selected = provider._select_stats_for_target(target, newer.last_sample_monotonic)

    assert selected is newer
