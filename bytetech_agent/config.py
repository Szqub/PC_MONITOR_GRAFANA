"""
ByteTech Agent - central configuration module.
Pydantic validation, YAML loading, consistent naming.
"""
from pydantic import BaseModel, Field
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
    display_provider_enabled: bool = True
    nvapi_provider_enabled: bool = True
    system_provider_enabled: bool = True


class LhmConfig(BaseModel):
    """LHM provider settings - JSON API fallback URL."""
    json_url: str = "http://127.0.0.1:8085"


class PresentMonConfig(BaseModel):
    target_mode: str = "active_foreground"
    process_name: Optional[str] = None
    process_id: Optional[int] = None
    executable_path: Optional[str] = None


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
    lhm: LhmConfig = Field(default_factory=LhmConfig)
    presentmon: PresentMonConfig = Field(default_factory=PresentMonConfig)
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
