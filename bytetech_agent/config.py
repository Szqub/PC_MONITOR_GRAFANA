"""
ByteTech Agent - central configuration module.
Pydantic validation, YAML loading, consistent naming.
"""
from pydantic import BaseModel, Field, field_validator
from typing import Dict, Any, Optional, List
import yaml
import os


class InfluxConfig(BaseModel):
    url: str
    token: str
    org: str
    bucket: str = "metrics"


class MetadataConfig(BaseModel):
    host_alias: str
    site: str
    owner: str


class TimingConfig(BaseModel):
    hw_interval_sec: int = 2
    fps_interval_sec: int = 1


class ProvidersConfig(BaseModel):
    lhm_enabled: bool = True
    presentmon_enabled: bool = True
    fps_provider_enabled: Optional[bool] = None
    display_provider_enabled: bool = True
    nvapi_provider_enabled: bool = True
    system_provider_enabled: bool = True

    @property
    def fps_enabled(self) -> bool:
        if self.fps_provider_enabled is None:
            return self.presentmon_enabled
        return self.fps_provider_enabled


class LhmConfig(BaseModel):
    """LHM provider settings - JSON API fallback URL."""
    json_url: str = "http://127.0.0.1:8085"


class PresentMonConfig(BaseModel):
    target_mode: str = "active_foreground"
    process_name: Optional[str] = None
    process_id: Optional[int] = None
    executable_path: Optional[str] = None

    @field_validator("target_mode", mode="before")
    @classmethod
    def _normalize_target_mode(cls, value):
        normalized = str(value or "active_foreground").strip().lower()
        if normalized == "explicit_pid":
            normalized = "explicit_process_id"
        allowed = {
            "active_foreground",
            "explicit_process_name",
            "explicit_process_id",
        }
        if normalized not in allowed:
            raise ValueError(f"Unsupported presentmon.target_mode: {value!r}")
        return normalized

    @field_validator("process_name", mode="before")
    @classmethod
    def _normalize_process_name(cls, value):
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @field_validator("process_id", mode="before")
    @classmethod
    def _normalize_process_id(cls, value):
        if value in (None, "", 0, "0"):
            return None
        normalized = int(value)
        return normalized if normalized > 0 else None


class FpsConfig(BaseModel):
    backend: str = "presentmon_service_api"
    fallback_backend: Optional[str] = "presentmon_console"

    @field_validator("backend", mode="before")
    @classmethod
    def _normalize_backend(cls, value):
        normalized = str(value or "presentmon_service_api").strip().lower()
        aliases = {
            "rtss": "rtss_shared_memory",
            "presentmon": "presentmon_console",
            "presentmon_service": "presentmon_service_api",
        }
        normalized = aliases.get(normalized, normalized)
        allowed = {"presentmon_service_api", "rtss_shared_memory", "presentmon_console"}
        if normalized not in allowed:
            raise ValueError(f"Unsupported fps.backend: {value!r}")
        return normalized

    @field_validator("fallback_backend", mode="before")
    @classmethod
    def _normalize_fallback_backend(cls, value):
        if value in (None, "", "none", "null"):
            return None
        normalized = str(value).strip().lower()
        aliases = {
            "presentmon": "presentmon_console",
            "rtss": "rtss_shared_memory",
            "presentmon_service": "presentmon_service_api",
        }
        normalized = aliases.get(normalized, normalized)
        allowed = {"presentmon_service_api", "rtss_shared_memory", "presentmon_console"}
        if normalized not in allowed:
            raise ValueError(f"Unsupported fps.fallback_backend: {value!r}")
        return normalized


class RtssConfig(BaseModel):
    shared_memory_name: str = "RTSSSharedMemoryV2"
    stale_timeout_ms: int = 2000

    @field_validator("shared_memory_name", mode="before")
    @classmethod
    def _normalize_shared_memory_name(cls, value):
        normalized = str(value or "RTSSSharedMemoryV2").strip()
        return normalized or "RTSSSharedMemoryV2"

    @field_validator("stale_timeout_ms", mode="before")
    @classmethod
    def _normalize_stale_timeout_ms(cls, value):
        normalized = int(value or 2000)
        return max(250, normalized)


class PresentMonServiceConfig(BaseModel):
    enabled: bool = True
    sdk_path: Optional[str] = None
    api_loader_dll: Optional[str] = None
    api_runtime_dll: Optional[str] = None
    service_dir: Optional[str] = None
    connect_timeout_ms: int = 3000
    poll_interval_ms: int = 250

    @field_validator("connect_timeout_ms", "poll_interval_ms", mode="before")
    @classmethod
    def _normalize_positive_int(cls, value):
        normalized = int(value or 0)
        return max(100, normalized)


class LoggingConfig(BaseModel):
    level: str = "INFO"
    log_dir: str = "logs"


class BufferConfig(BaseModel):
    """Data buffer config for InfluxDB outage resilience."""
    enabled: bool = True
    max_memory_points: int = 10000
    spool_dir: str = "spool"
    max_spool_files: int = 50


class OptionsConfig(BaseModel):
    tags_extra: Dict[str, str] = Field(default_factory=dict)
    custom_fields: Dict[str, Any] = Field(default_factory=dict)
    # Note: retention_hint_days is an agent-side hint only.
    # Actual retention MUST be configured on the InfluxDB bucket itself.
    retention_hint_days: int = 2


class AppConfig(BaseModel):
    influx: InfluxConfig
    metadata: MetadataConfig
    timing: TimingConfig = Field(default_factory=TimingConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    fps: FpsConfig = Field(default_factory=FpsConfig)
    lhm: LhmConfig = Field(default_factory=LhmConfig)
    rtss: RtssConfig = Field(default_factory=RtssConfig)
    presentmon: PresentMonConfig = Field(default_factory=PresentMonConfig)
    presentmon_service: PresentMonServiceConfig = Field(default_factory=PresentMonServiceConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    buffer: BufferConfig = Field(default_factory=BufferConfig)
    options: OptionsConfig = Field(default_factory=OptionsConfig)


def load_config(config_path: str = "config.yaml") -> AppConfig:
    """Load and validate configuration from a YAML file."""
    if not os.path.exists(config_path):
        raise FileNotFoundError(
            f"Config not found: {config_path}. "
            f"Create one from config.example.yaml."
        )

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return AppConfig(**data)
