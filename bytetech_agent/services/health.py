"""
ByteTech Agent - Health Service.
Monitors the state of all providers and periodically emits status metrics
to the 'pc_state' measurement with info_type='agent_health'.
"""
import logging
import time
from typing import List, Dict

from bytetech_agent.models.metrics import (
    MetricData,
    ProviderHealthInfo,
    ProviderStatus,
)

logger = logging.getLogger(__name__)


class HealthService:
    """
    Manages the health state of the agent and providers.
    Emits metrics to pc_state (info_type=agent_health).
    """

    def __init__(self, host_alias: str):
        self._host_alias = host_alias
        self._providers: Dict[str, ProviderHealthInfo] = {}
        self._influx_connected: bool = False
        self._agent_start_time: float = time.time()
        self._last_emit_time: float = 0
        self._emit_count: int = 0

    def register_provider(self, health: ProviderHealthInfo):
        """Registers a provider in the health service."""
        self._providers[health.name] = health
        logger.debug(f"Health: registered provider '{health.name}'")

    def set_influx_status(self, connected: bool):
        """Sets the InfluxDB connection status."""
        self._influx_connected = connected

    @property
    def overall_status(self) -> str:
        """Returns the overall agent status."""
        if not self._providers:
            return "no_providers"

        statuses = [p.status for p in self._providers.values()]

        if all(s == ProviderStatus.AVAILABLE for s in statuses):
            return "healthy"
        elif any(s in (ProviderStatus.AVAILABLE, ProviderStatus.DEGRADED) for s in statuses):
            return "degraded"
        elif all(s in (ProviderStatus.UNAVAILABLE, ProviderStatus.FAILED) for s in statuses):
            return "critical"
        else:
            return "unknown"

    def get_provider_summary(self) -> Dict[str, dict]:
        """Returns a summary of provider states."""
        summary = {}
        for name, health in self._providers.items():
            summary[name] = {
                "status": health.status.value,
                "last_success": health.last_success,
                "last_error": health.last_error,
                "metrics_collected": health.metrics_collected,
                "capabilities": health.capabilities,
            }
        return summary

    def emit_health_metrics(self) -> List[MetricData]:
        """
        Generates agent health metrics to be saved in InfluxDB.
        Emits:
        - Overall agent status
        - Status of each provider
        - Capability flags
        """
        metrics: List[MetricData] = []
        now = time.time()

        # -- Overall agent status --
        agent_tags = {
            "host": self._host_alias,
            "info_type": "agent_health",
        }

        agent_fields = {
            "agent_status": self.overall_status,
            "agent_uptime_sec": round(now - self._agent_start_time, 0),
            "influx_connected": 1 if self._influx_connected else 0,
            "providers_total": len(self._providers),
            "providers_available": sum(
                1 for p in self._providers.values()
                if p.status == ProviderStatus.AVAILABLE
            ),
            "providers_degraded": sum(
                1 for p in self._providers.values()
                if p.status == ProviderStatus.DEGRADED
            ),
            "providers_failed": sum(
                1 for p in self._providers.values()
                if p.status in (ProviderStatus.FAILED, ProviderStatus.UNAVAILABLE)
            ),
        }

        metrics.append(MetricData(
            measurement_name="pc_state",
            tags=agent_tags,
            fields=agent_fields,
        ))

        # -- Individual provider status --
        for name, health in self._providers.items():
            prov_tags = {
                "host": self._host_alias,
                "info_type": "provider_health",
                "provider_name": name,
            }

            prov_fields: dict = {
                "status": health.status.value,
                "metrics_collected": health.metrics_collected,
            }

            if health.last_success is not None:
                prov_fields["last_success_ago_sec"] = round(now - health.last_success, 0)

            if health.last_error is not None:
                prov_fields["last_error"] = str(health.last_error)[:200]

            if health.last_error_time is not None:
                prov_fields["last_error_ago_sec"] = round(now - health.last_error_time, 0)

            # Capability flags
            for cap_name, cap_available in health.capabilities.items():
                prov_fields[f"cap_{cap_name}"] = 1 if cap_available else 0

            metrics.append(MetricData(
                measurement_name="pc_state",
                tags=prov_tags,
                fields=prov_fields,
            ))

        self._last_emit_time = now
        self._emit_count += 1

        return metrics

    def log_summary(self):
        """Logs a summary of provider states."""
        for name, health in self._providers.items():
            caps = [k for k, v in health.capabilities.items() if v]
            missing = [k for k, v in health.capabilities.items() if not v]

            status_icon = {
                ProviderStatus.AVAILABLE: "✅",
                ProviderStatus.DEGRADED: "⚠️",
                ProviderStatus.FAILED: "❌",
                ProviderStatus.UNAVAILABLE: "⛔",
                ProviderStatus.INITIALIZING: "⏳",
            }.get(health.status, "❓")

            logger.info(
                f"  {status_icon} {name}: {health.status.value} "
                f"| caps: {len(caps)} available"
                + (f", {len(missing)} unavailable" if missing else "")
            )

            if health.last_error:
                logger.debug(f"    Last error: {health.last_error}")
