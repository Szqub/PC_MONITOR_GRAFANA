"""Configuration tests for ByteTech Agent."""
import os
import tempfile

import pytest
import yaml

from bytetech_agent.config import (
    AppConfig,
    BufferConfig,
    FpsConfig,
    InfluxConfig,
    LoggingConfig,
    MetadataConfig,
    OptionsConfig,
    PresentMonConfig,
    PresentMonServiceConfig,
    ProvidersConfig,
    RtssConfig,
    TimingConfig,
    load_config,
)


def _make_config_dict(**overrides):
    base = {
        "influx": {
            "url": "http://localhost:8086",
            "token": "test-token",
            "org": "test-org",
            "bucket": "test-bucket",
        },
        "metadata": {
            "host_alias": "TestPC",
            "site": "TestSite",
            "owner": "TestOwner",
        },
    }
    base.update(overrides)
    return base


def _write_yaml(data: dict) -> str:
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        yaml.dump(data, handle)
    return path


class TestInfluxConfig:
    def test_valid(self):
        cfg = InfluxConfig(url="http://x:8086", token="t", org="o", bucket="b")
        assert cfg.url == "http://x:8086"
        assert cfg.token == "t"

    def test_missing_required(self):
        with pytest.raises(Exception):
            InfluxConfig(url="http://x:8086")


class TestMetadataConfig:
    def test_valid(self):
        cfg = MetadataConfig(host_alias="PC1", site="Home", owner="Jan")
        assert cfg.host_alias == "PC1"

    def test_missing_required(self):
        with pytest.raises(Exception):
            MetadataConfig(host_alias="PC1")


class TestTimingConfig:
    def test_defaults(self):
        cfg = TimingConfig()
        assert cfg.hw_interval_sec == 2
        assert cfg.fps_interval_sec == 1

    def test_custom_values(self):
        cfg = TimingConfig(hw_interval_sec=30, fps_interval_sec=5)
        assert cfg.hw_interval_sec == 30


class TestProvidersConfig:
    def test_defaults(self):
        cfg = ProvidersConfig()
        assert cfg.lhm_enabled is True
        assert cfg.presentmon_enabled is True
        assert cfg.fps_enabled is True
        assert cfg.display_provider_enabled is True
        assert cfg.nvapi_provider_enabled is True
        assert cfg.system_provider_enabled is True

    def test_field_names_consistency(self):
        cfg = ProvidersConfig()
        assert hasattr(cfg, "nvapi_provider_enabled")
        assert hasattr(cfg, "display_provider_enabled")
        assert hasattr(cfg, "system_provider_enabled")

    def test_new_fps_provider_flag_overrides_legacy_presentmon_flag(self):
        cfg = ProvidersConfig(presentmon_enabled=False, fps_provider_enabled=True)
        assert cfg.fps_enabled is True


class TestBufferConfig:
    def test_defaults(self):
        cfg = BufferConfig()
        assert cfg.enabled is True
        assert cfg.max_memory_points == 10000
        assert cfg.max_spool_files == 50


class TestPresentMonConfig:
    def test_defaults(self):
        cfg = PresentMonConfig()
        assert cfg.target_mode == "smart_auto"
        assert cfg.process_name is None
        assert cfg.process_id is None

    def test_normalizes_explicit_pid_alias(self):
        cfg = PresentMonConfig(target_mode="explicit_pid", process_id=1234)
        assert cfg.target_mode == "explicit_process_id"
        assert cfg.process_id == 1234

    def test_rejects_invalid_target_mode(self):
        with pytest.raises(Exception):
            PresentMonConfig(target_mode="bad_mode")


class TestFpsConfig:
    def test_defaults(self):
        cfg = FpsConfig()
        assert cfg.backend == "presentmon_service_api"
        assert cfg.fallback_backend == "presentmon_console"

    def test_normalizes_aliases(self):
        cfg = FpsConfig(backend="rtss", fallback_backend="presentmon")
        assert cfg.backend == "rtss_shared_memory"
        assert cfg.fallback_backend == "presentmon_console"


class TestPresentMonServiceConfig:
    def test_defaults(self):
        cfg = PresentMonServiceConfig()
        assert cfg.enabled is True
        assert cfg.poll_interval_ms == 250
        assert cfg.connect_timeout_ms == 3000


class TestRtssConfig:
    def test_defaults(self):
        cfg = RtssConfig()
        assert cfg.shared_memory_name == "RTSSSharedMemoryV2"
        assert cfg.stale_timeout_ms == 2000


class TestOptionsConfig:
    def test_retention_hint_days(self):
        cfg = OptionsConfig()
        assert cfg.retention_hint_days == 2

    def test_custom_retention(self):
        cfg = OptionsConfig(retention_hint_days=90)
        assert cfg.retention_hint_days == 90


class TestAppConfig:
    def test_full_config(self):
        data = _make_config_dict()
        cfg = AppConfig(**data)
        assert cfg.influx.url == "http://localhost:8086"
        assert cfg.metadata.host_alias == "TestPC"
        assert cfg.timing.hw_interval_sec == 2
        assert cfg.providers.lhm_enabled is True
        assert cfg.buffer.enabled is True

    def test_with_overrides(self):
        data = _make_config_dict(
            timing={"hw_interval_sec": 30, "fps_interval_sec": 5},
            providers={
                "lhm_enabled": False,
                "presentmon_enabled": False,
                "display_provider_enabled": True,
                "nvapi_provider_enabled": False,
                "system_provider_enabled": True,
            },
        )
        cfg = AppConfig(**data)
        assert cfg.timing.hw_interval_sec == 30
        assert cfg.providers.lhm_enabled is False


class TestLoadConfig:
    def test_load_valid_yaml(self):
        data = _make_config_dict()
        path = _write_yaml(data)
        try:
            cfg = load_config(path)
            assert cfg.influx.bucket == "test-bucket"
        finally:
            os.unlink(path)

    def test_missing_file(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.yaml")

    def test_example_config_is_valid(self):
        example_path = os.path.join(
            os.path.dirname(__file__), "..", "examples", "config.example.yaml"
        )
        if os.path.exists(example_path):
            with open(example_path, "r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
            cfg = AppConfig(**data)
            assert cfg.influx.url is not None

    def test_load_yaml_with_single_quoted_windows_paths(self):
        yaml_text = """
influx:
  url: 'http://localhost:8086'
  token: 'test-token'
  org: 'bytetech'
  bucket: 'metrics'

metadata:
  host_alias: 'MY-PC'
  site: 'Home'
  owner: 'tester'

presentmon:
  executable_path: 'C:\\ByteTechAgent\\bin\\PresentMon.exe'

presentmon_service:
  sdk_path: 'C:\\Program Files\\Intel\\PresentMon\\SDK\\PresentMonAPI2.dll'
  api_loader_dll: 'C:\\Program Files\\Intel\\PresentMon\\SDK\\PresentMonAPI2Loader.dll'
  api_runtime_dll: 'C:\\Program Files\\Intel\\PresentMon\\SDK\\PresentMonAPI2.dll'
  service_dir: 'C:\\Program Files\\Intel\\PresentMonSharedService'
"""
        fd, path = tempfile.mkstemp(suffix=".yaml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(yaml_text)
            cfg = load_config(path)
            assert cfg.presentmon.executable_path == r"C:\ByteTechAgent\bin\PresentMon.exe"
            assert cfg.presentmon_service.sdk_path == r"C:\Program Files\Intel\PresentMon\SDK\PresentMonAPI2.dll"
            assert cfg.presentmon_service.api_loader_dll == r"C:\Program Files\Intel\PresentMon\SDK\PresentMonAPI2Loader.dll"
            assert cfg.presentmon_service.api_runtime_dll == r"C:\Program Files\Intel\PresentMon\SDK\PresentMonAPI2.dll"
            assert cfg.presentmon_service.service_dir == r"C:\Program Files\Intel\PresentMonSharedService"
        finally:
            os.unlink(path)
