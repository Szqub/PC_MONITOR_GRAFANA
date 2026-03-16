"""Tests for installer-facing YAML and diagnostics helpers."""

import yaml

from bytetech_agent.installer_helpers import (
    parse_installer_test_output,
    yaml_single_quoted_scalar,
)


def test_yaml_single_quoted_scalar_preserves_windows_paths():
    rendered = yaml_single_quoted_scalar(r"C:\ByteTechAgent\bin\PresentMon.exe")
    assert rendered == r"'C:\ByteTechAgent\bin\PresentMon.exe'"
    loaded = yaml.safe_load(f"path: {rendered}\n")
    assert loaded["path"] == r"C:\ByteTechAgent\bin\PresentMon.exe"


def test_yaml_single_quoted_scalar_preserves_program_files_paths():
    rendered = yaml_single_quoted_scalar(
        r"C:\Program Files\Intel\PresentMon\SDK\PresentMonAPI2.dll"
    )
    loaded = yaml.safe_load(
        f"sdk_path: {rendered}\nservice_dir: {yaml_single_quoted_scalar(r'C:\Program Files\Intel\PresentMonSharedService')}\n"
    )
    assert loaded["sdk_path"] == r"C:\Program Files\Intel\PresentMon\SDK\PresentMonAPI2.dll"
    assert loaded["service_dir"] == r"C:\Program Files\Intel\PresentMonSharedService"


def test_parse_installer_output_detects_config_load_failure():
    parsed = parse_installer_test_output(
        "CONFIG_LOAD_ERROR:\nTraceback...\nyaml.scanner.ScannerError\n"
    )
    assert parsed["config_load_error"] is True
    assert parsed["health_error"] is False
    assert parsed["write_error"] is False


def test_parse_installer_output_detects_health_failure():
    parsed = parse_installer_test_output(
        "CONFIG_LOAD_OK\nHEALTH_ERROR:\nRuntimeError: health failed\n"
    )
    assert parsed["config_load_ok"] is True
    assert parsed["health_error"] is True
    assert parsed["write_error"] is False


def test_parse_installer_output_detects_write_failure():
    parsed = parse_installer_test_output(
        "CONFIG_LOAD_OK\nHEALTH:pass\nWRITE_ERROR:\nRuntimeError: write failed\n"
    )
    assert parsed["config_load_ok"] is True
    assert parsed["health_ok"] is True
    assert parsed["write_error"] is True


def test_parse_installer_output_detects_success():
    parsed = parse_installer_test_output("CONFIG_LOAD_OK\nHEALTH:pass\nWRITE:OK\n")
    assert parsed == {
        "config_load_ok": True,
        "config_load_error": False,
        "health_ok": True,
        "health_error": False,
        "write_ok": True,
        "write_error": False,
    }
