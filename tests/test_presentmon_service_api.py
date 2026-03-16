"""Tests for PresentMon Service API path resolution and validation helpers."""
from pathlib import Path
from types import SimpleNamespace

from bytetech_agent.providers.presentmon_service_api import (
    resolve_presentmon_api_paths,
    resolve_presentmon_loader_dll,
    resolve_presentmon_runtime_dll,
    validate_presentmon_installation,
)


def _make_service_config(**overrides):
    base = {
        "sdk_path": None,
        "api_loader_dll": None,
        "api_runtime_dll": None,
        "service_dir": None,
        "poll_interval_ms": 250,
        "connect_timeout_ms": 3000,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_resolve_presentmon_api_paths_prefers_explicit_valid_dlls(tmp_path):
    sdk_dir = tmp_path / "SDK"
    sdk_dir.mkdir()
    loader_dll = sdk_dir / "PresentMonAPI2Loader.dll"
    runtime_dll = sdk_dir / "PresentMonAPI2.dll"
    loader_dll.write_bytes(b"loader")
    runtime_dll.write_bytes(b"runtime")

    config = _make_service_config(
        sdk_path=str(sdk_dir),
        api_loader_dll=str(loader_dll),
        api_runtime_dll=str(runtime_dll),
    )

    paths = resolve_presentmon_api_paths(config)

    assert paths.api_loader_dll == str(loader_dll)
    assert paths.api_runtime_dll == str(runtime_dll)
    assert paths.chosen_dll == str(loader_dll)


def test_resolve_presentmon_runtime_tolerates_sharedservice_and_sharedservices_names(tmp_path, monkeypatch):
    service_one = tmp_path / "PresentMonSharedService"
    service_two = tmp_path / "PresentMonSharedServices"
    service_one.mkdir()
    service_two.mkdir()
    (service_two / "PresentMonAPI2.dll").write_bytes(b"runtime")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))

    config = _make_service_config(service_dir=str(service_two))
    paths = resolve_presentmon_api_paths(config)
    assert paths.api_runtime_dll == str(service_two / "PresentMonAPI2.dll")


def test_validate_presentmon_installation_rejects_gui_exe_as_api_source(tmp_path):
    gui_dir = tmp_path / "PresentMonApplication"
    gui_dir.mkdir()
    gui_exe = gui_dir / "PresentMon.exe"
    gui_exe.write_bytes(b"gui")

    config = _make_service_config(api_runtime_dll=str(gui_exe))
    validation = validate_presentmon_installation(config)

    assert validation["ok"] is False
    assert "runtime DLL path is invalid" in validation["errors"][0]


def test_loader_and_runtime_helpers_accept_file_based_detection(tmp_path):
    sdk_dir = tmp_path / "Intel" / "PresentMon" / "SDK"
    sdk_dir.mkdir(parents=True)
    loader = sdk_dir / "PresentMonAPI2Loader.dll"
    runtime = sdk_dir / "PresentMonAPI2.dll"
    loader.write_bytes(b"loader")
    runtime.write_bytes(b"runtime")

    config = _make_service_config(sdk_path=str(sdk_dir))

    assert resolve_presentmon_loader_dll(config) == str(loader)
    assert resolve_presentmon_runtime_dll(config) == str(runtime)
