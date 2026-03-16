"""Helpers for installer-facing YAML rendering and test output parsing."""
from __future__ import annotations

from typing import Dict


def yaml_single_quoted_scalar(value: str) -> str:
    text = "" if value is None else str(value)
    return "'" + text.replace("'", "''") + "'"


def parse_installer_test_output(output: str) -> Dict[str, bool]:
    text = output or ""
    return {
        "config_load_ok": "CONFIG_LOAD_OK" in text,
        "config_load_error": "CONFIG_LOAD_ERROR:" in text,
        "health_ok": "HEALTH:pass" in text,
        "health_error": "HEALTH_ERROR:" in text,
        "write_ok": "WRITE:OK" in text,
        "write_error": "WRITE_ERROR:" in text,
    }
