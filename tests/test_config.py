"""Testy konfiguracji ByteTech Agent."""
import os
import tempfile
import pytest
import yaml

from bytetech_agent.config import (
    AppConfig,
    InfluxConfig,
    MetadataConfig,
    TimingConfig,
    ProvidersConfig,
    PresentMonConfig,
    LoggingConfig,
    BufferConfig,
    OptionsConfig,
    load_config,
)


def _make_config_dict(**overrides):
    """Helper: tworzy minimalny poprawny config dict."""
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
    """Zapisuje dict jako YAML do pliku tymczasowego, zwraca ścieżkę."""
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        yaml.dump(data, f)
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
        cfg = MetadataConfig(host_alias="PC1", site="Dom", owner="Jan")
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
        assert cfg.display_provider_enabled is True
        assert cfg.nvapi_provider_enabled is True
        assert cfg.system_provider_enabled is True

    def test_field_names_consistency(self):
        """Sprawdza, że field names są spójne – to co było bugiem w scheduler."""
        cfg = ProvidersConfig()
        # Sprawdzenie że atrybut istnieje i nie rzuca AttributeError
        assert hasattr(cfg, "nvapi_provider_enabled")
        assert hasattr(cfg, "display_provider_enabled")
        assert hasattr(cfg, "system_provider_enabled")


class TestBufferConfig:
    def test_defaults(self):
        cfg = BufferConfig()
        assert cfg.enabled is True
        assert cfg.max_memory_points == 10000
        assert cfg.max_spool_files == 50


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
            providers={"lhm_enabled": False, "presentmon_enabled": False,
                       "display_provider_enabled": True, "nvapi_provider_enabled": False,
                       "system_provider_enabled": True},
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
        """Sprawdza, że config.example.yaml jest poprawny (parsuje się bez błędów)."""
        example_path = os.path.join(
            os.path.dirname(__file__), "..", "examples", "config.example.yaml"
        )
        if os.path.exists(example_path):
            with open(example_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            # Powinno się sparsować bez wyjątku (token/url to stringi, nawet template)
            cfg = AppConfig(**data)
            assert cfg.influx.url is not None
