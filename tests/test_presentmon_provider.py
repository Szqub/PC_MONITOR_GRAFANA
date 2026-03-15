"""Tests for PresentMon console stdout provider helpers."""
from types import SimpleNamespace
import time

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
    assert snapshot["present_mode_name"] == "Hardware: Independent Flip"


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


def test_provider_uses_safe_present_mode_field_name():
    provider = PresentMonProvider(
        SimpleNamespace(
            target_mode="active_foreground",
            process_name="",
            process_id=0,
            executable_path=None,
        )
    )
    context = ProviderContext(host_alias="PC1", host_name="pc1")
    provider._snapshot_for_target = lambda _: {
        "reason": "ok",
        "process_name": "game.exe",
        "pid": 777,
        "fps_now": 60.0,
        "frametime_ms_now": 16.67,
        "fps_avg_10s": 59.5,
        "fps_avg_30s": 58.0,
        "fps_1pct_30s": 41.0,
        "fps_0_1pct_30s": 32.0,
        "present_mode_name": "Hardware: Independent Flip",
    }

    metric = provider._build_metric(
        context,
        PresentMonTarget(
            mode="active_foreground",
            filter_kind="process_id",
            filter_value="777",
            pid=777,
            process_name="game.exe",
        ),
    )

    assert metric.fields["present_mode_name"] == "Hardware: Independent Flip"
    assert "present_mode" not in metric.fields


def test_build_command_uses_stdout_without_no_csv():
    provider = PresentMonProvider(
        SimpleNamespace(
            target_mode="explicit_process_id",
            process_name=None,
            process_id=1234,
            executable_path="C:\\PresentMon.exe",
        )
    )
    provider._exe_path = "C:\\PresentMon.exe"

    command = provider._build_command(
        PresentMonTarget(
            mode="explicit_process_id",
            filter_kind="process_id",
            filter_value="1234",
            pid=1234,
            process_name="game.exe",
        )
    )

    assert "--output_stdout" in command
    assert "--no_console_stats" in command
    assert "--process_id" in command
    assert "--terminate_on_proc_exit" in command
    assert "--no_csv" not in command


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


def test_provider_defers_restart_after_launch_failure():
    provider = PresentMonProvider(
        SimpleNamespace(
            target_mode="active_foreground",
            process_name="",
            process_id=0,
            executable_path=None,
        )
    )
    target = PresentMonTarget(
        mode="active_foreground",
        filter_kind="process_id",
        filter_value="123",
        pid=123,
        process_name="game.exe",
    )
    attempts = []

    provider._active_target = target
    provider._last_capture_error = "boom"
    provider._next_launch_retry_monotonic = time.monotonic() + 60.0
    provider._start_capture_locked = lambda arg: attempts.append(arg)

    provider._ensure_capture_target(target)

    assert attempts == []
