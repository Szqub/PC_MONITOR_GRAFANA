from types import SimpleNamespace

from bytetech_agent.models.metrics import ProviderContext, ProviderStatus
from bytetech_agent.providers.presentmon_service_api import PresentMonApiSnapshot
from bytetech_agent.providers.presentmon_service_provider import PresentMonServiceProvider


class FakeServiceClient:
    def __init__(self, snapshots):
        self._snapshots = snapshots
        self.polled_pids = []

    def poll_process(self, pid):
        self.polled_pids.append(pid)
        value = self._snapshots.get(pid)
        if isinstance(value, list):
            if not value:
                return None
            next_value = value.pop(0)
            return next_value
        return value


def _snapshot(
    *,
    application_fps=0.0,
    displayed_fps=0.0,
    frametime_ms=0.0,
    cpu_busy_ms=None,
    gpu_busy_ms=None,
    display_latency_ms=None,
):
    return PresentMonApiSnapshot(
        application_fps=application_fps,
        displayed_fps=displayed_fps,
        frametime_ms=frametime_ms,
        cpu_busy_ms=cpu_busy_ms,
        gpu_busy_ms=gpu_busy_ms,
        display_latency_ms=display_latency_ms,
    )


def _provider(target_mode="smart_auto", process_name=None, process_id=None, snapshots=None):
    provider = PresentMonServiceProvider(
        fps_config=SimpleNamespace(backend="presentmon_service_api", fallback_backend="presentmon_console"),
        presentmon_config=SimpleNamespace(
            target_mode=target_mode,
            process_name=process_name,
            process_id=process_id,
        ),
        presentmon_service_config=SimpleNamespace(
            enabled=True,
            sdk_path=None,
            api_loader_dll=None,
            api_runtime_dll=None,
            service_dir=None,
            connect_timeout_ms=3000,
            poll_interval_ms=250,
        ),
    )
    provider._client = FakeServiceClient(snapshots or {})
    provider._health.status = ProviderStatus.AVAILABLE
    return provider


def _context():
    return ProviderContext(host_alias="MY-PC", host_name="my-pc")


def test_smart_auto_prefers_game_over_browser_helper():
    provider = _provider(
        snapshots={
            222: _snapshot(application_fps=91.0, displayed_fps=88.0, frametime_ms=10.99),
        }
    )
    provider._get_foreground_pid = lambda: 111
    provider._related_process_ids = lambda pid: {"parent": None, "children": [222], "siblings": []}
    provider._get_process_name = lambda pid: {111: "chrome.exe", 222: "helldivers2.exe"}[pid]
    provider._get_process_exe = lambda pid: {
        111: r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        222: r"C:\Program Files (x86)\Steam\steamapps\common\Helldivers 2\bin\helldivers2.exe",
    }[pid]
    provider._get_process_cmdline = lambda pid: ""

    metrics = provider.get_metrics(_context())

    assert len(metrics) == 1
    assert metrics[0].tags["pid"] == "222"
    assert metrics[0].fields["fps_application_now"] == 91.0
    assert metrics[0].fields["fps_now"] == 91.0
    assert provider._client.polled_pids == [222]


def test_smart_auto_keeps_last_good_game_during_alt_tab():
    provider = _provider(
        snapshots={
            777: _snapshot(application_fps=72.0, displayed_fps=70.0, frametime_ms=13.89),
        }
    )
    provider._get_foreground_pid = lambda: 555
    provider._related_process_ids = lambda pid: {"parent": None, "children": [], "siblings": []}
    provider._get_process_name = lambda pid: {555: "brave.exe", 777: "game.exe"}[pid]
    provider._get_process_exe = lambda pid: ""
    provider._get_process_cmdline = lambda pid: ""
    provider._pid_alive = lambda pid: pid == 777
    provider._last_good_target = SimpleNamespace(pid=777, process_name="game.exe")
    provider._last_good_target_monotonic = provider._last_good_target_monotonic = __import__("time").monotonic()

    metrics = provider.get_metrics(_context())

    assert len(metrics) == 1
    assert metrics[0].tags["pid"] == "777"
    assert provider._client.polled_pids == [777]


def test_invalid_target_produces_no_metrics():
    provider = _provider(target_mode="active_foreground")
    provider._get_foreground_pid = lambda: None

    metrics = provider.get_metrics(_context())

    assert metrics == []


def test_zero_snapshot_produces_no_metrics():
    provider = _provider(
        target_mode="active_foreground",
        snapshots={333: _snapshot()},
    )
    provider._get_foreground_pid = lambda: 333
    provider._get_process_name = lambda pid: "game.exe"

    metrics = provider.get_metrics(_context())

    assert metrics == []


def test_explicit_process_id_works():
    provider = _provider(
        target_mode="explicit_process_id",
        process_id=444,
        snapshots={444: _snapshot(application_fps=60.0, displayed_fps=59.5, frametime_ms=16.67)},
    )
    provider._get_process_name = lambda pid: "game.exe"

    metrics = provider.get_metrics(_context())

    assert len(metrics) == 1
    assert metrics[0].tags["pid"] == "444"


def test_explicit_process_name_works():
    provider = _provider(
        target_mode="explicit_process_name",
        process_name="game.exe",
        snapshots={445: _snapshot(application_fps=120.0, displayed_fps=118.0, frametime_ms=8.33)},
    )
    provider._find_process_by_name = lambda name: 445
    provider._get_process_name = lambda pid: "game.exe"

    metrics = provider.get_metrics(_context())

    assert len(metrics) == 1
    assert metrics[0].tags["pid"] == "445"


def test_denylisted_helper_is_rejected_when_valid_game_exists():
    provider = _provider(
        snapshots={
            901: _snapshot(application_fps=144.0, displayed_fps=141.0, frametime_ms=6.94),
        }
    )
    provider._get_foreground_pid = lambda: 900
    provider._related_process_ids = lambda pid: {"parent": None, "children": [901], "siblings": []}
    provider._get_process_name = lambda pid: {900: "steamwebhelper.exe", 901: "game.exe"}[pid]
    provider._get_process_exe = lambda pid: ""
    provider._get_process_cmdline = lambda pid: ""

    metrics = provider.get_metrics(_context())

    assert len(metrics) == 1
    assert metrics[0].tags["pid"] == "901"
    assert provider._client.polled_pids == [901]
