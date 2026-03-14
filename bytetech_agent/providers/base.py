"""
ByteTech Agent - abstract base for all metric providers.
Contains capability tracking, status reporting, and safe execution wrapper.
"""
from abc import ABC, abstractmethod
from typing import List, Dict
import logging
from bytetech_agent.models.metrics import (
    MetricData,
    ProviderContext,
    ProviderHealthInfo,
    ProviderStatus,
)

logger = logging.getLogger(__name__)


class BaseProvider(ABC):
    """
    Abstract base class for all metric providers.
    Each provider registers its capabilities and reports its health state.
    """

    def __init__(self, name: str):
        self._health = ProviderHealthInfo(name=name)

    @property
    def health(self) -> ProviderHealthInfo:
        return self._health

    @property
    def name(self) -> str:
        return self._health.name

    @property
    def is_available(self) -> bool:
        return self._health.status in (
            ProviderStatus.AVAILABLE,
            ProviderStatus.DEGRADED,
        )

    @abstractmethod
    def initialize(self) -> bool:
        """
        Initializes the provider (e.g., loads libraries, connects to WMI).
        Returns True if initialization was successful.
        Implementation should set self._health.capabilities.
        """
        ...

    @abstractmethod
    def _collect(self, context: ProviderContext) -> List[MetricData]:
        """
        Internal metric collection method.
        Implemented by specific providers.
        """
        ...

    def get_metrics(self, context: ProviderContext) -> List[MetricData]:
        """
        Safe wrapper for metric collection.
        Catches exceptions, updates health, logs errors.
        """
        if not self.is_available:
            return []

        try:
            metrics = self._collect(context)
            self._health.mark_success(count=len(metrics))
            return metrics
        except Exception as e:
            self._health.mark_error(str(e))
            logger.error(f"[{self.name}] Metric collection error: {e}")
            return []

    @abstractmethod
    def shutdown(self):
        """Cleans up resources when shutting down the agent."""
        ...
