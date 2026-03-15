"""FPS backend router for RTSS primary backend and optional PresentMon fallback."""
from __future__ import annotations

import logging
from typing import List, Optional

from bytetech_agent.models.metrics import MetricData, ProviderContext, ProviderStatus
from bytetech_agent.providers.base import BaseProvider
from bytetech_agent.providers.presentmon_provider import PresentMonProvider
from bytetech_agent.providers.rtss_provider import RtssProvider

logger = logging.getLogger(__name__)


class FpsProvider(BaseProvider):
    def __init__(self, fps_config, rtss_config, presentmon_config):
        super().__init__(name="FPS")
        self._fps_config = fps_config
        self._rtss_config = rtss_config
        self._presentmon_config = presentmon_config
        self._primary = self._build_backend(fps_config.backend)
        self._fallback = self._build_backend(fps_config.fallback_backend) if fps_config.fallback_backend else None

    def initialize(self) -> bool:
        primary_ok = self._primary.initialize()
        fallback_ok = False
        if self._fallback:
            fallback_ok = self._fallback.initialize()

        self._health.capabilities = {
            "fps_now": True,
            "frametime_ms_now": True,
            "fps_avg_10s": True,
            "fps_avg_30s": True,
            "fps_1pct_30s": True,
            "fps_0_1pct_30s": True,
        }
        self._health.status = ProviderStatus.AVAILABLE if (primary_ok or fallback_ok) else ProviderStatus.DEGRADED
        logger.info(
            "FPS router initialized. primary=%s fallback=%s",
            self._fps_config.backend,
            self._fps_config.fallback_backend or "none",
        )
        return primary_ok or fallback_ok

    def _collect(self, context: ProviderContext) -> List[MetricData]:
        primary_metrics = self._primary.get_metrics(context)
        if primary_metrics:
            return primary_metrics

        if self._fallback:
            logger.debug(
                "FPS router switching to fallback backend. primary=%s fallback=%s",
                self._fps_config.backend,
                self._fps_config.fallback_backend,
            )
            fallback_metrics = self._fallback.get_metrics(context)
            if fallback_metrics:
                return fallback_metrics

        return []

    def shutdown(self):
        self._primary.shutdown()
        if self._fallback:
            self._fallback.shutdown()

    def _build_backend(self, backend_name: Optional[str]) -> BaseProvider:
        if backend_name == "presentmon_console":
            return PresentMonProvider(self._presentmon_config)
        return RtssProvider(self._fps_config, self._rtss_config, self._presentmon_config)
