"""
ByteTech Agent – modele danych metryk, statusów providerów i kontekstu.
"""
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List
from enum import Enum
import time


class ProviderStatus(Enum):
    """Status providera danych."""
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DEGRADED = "degraded"
    FAILED = "failed"
    INITIALIZING = "initializing"


@dataclass
class MetricData:
    """Reprezentuje jeden punkt danych (measurement) do zapisu w InfluxDB."""
    measurement_name: str
    tags: Dict[str, str] = field(default_factory=dict)
    fields: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderHealthInfo:
    """Stan zdrowia pojedynczego providera."""
    name: str
    status: ProviderStatus = ProviderStatus.INITIALIZING
    last_success: Optional[float] = None
    last_error: Optional[str] = None
    last_error_time: Optional[float] = None
    metrics_collected: int = 0
    capabilities: Dict[str, bool] = field(default_factory=dict)

    def mark_success(self, count: int = 0):
        self.status = ProviderStatus.AVAILABLE
        self.last_success = time.time()
        self.metrics_collected += count

    def mark_error(self, error: str):
        self.last_error = str(error)
        self.last_error_time = time.time()
        if self.last_success is not None:
            self.status = ProviderStatus.DEGRADED
        else:
            self.status = ProviderStatus.FAILED

    def mark_unavailable(self, reason: str = ""):
        self.status = ProviderStatus.UNAVAILABLE
        self.last_error = reason
        self.last_error_time = time.time()


@dataclass
class ProviderContext:
    """Kontekst przekazywany do providerów przy każdym pobraniu metryk."""
    host_alias: str
    host_name: str
    site: str = ""
    owner: str = ""
