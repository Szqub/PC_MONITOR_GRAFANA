"""Testy Health Service."""
import pytest

from bytetech_agent.models.metrics import ProviderHealthInfo, ProviderStatus
from bytetech_agent.services.health import HealthService


def _make_health_service():
    return HealthService(host_alias="TestPC")


class TestHealthService:
    def test_empty_status(self):
        hs = _make_health_service()
        assert hs.overall_status == "no_providers"

    def test_all_healthy(self):
        hs = _make_health_service()

        h1 = ProviderHealthInfo(name="LHM")
        h1.status = ProviderStatus.AVAILABLE
        h2 = ProviderHealthInfo(name="System")
        h2.status = ProviderStatus.AVAILABLE

        hs.register_provider(h1)
        hs.register_provider(h2)

        assert hs.overall_status == "healthy"

    def test_degraded(self):
        hs = _make_health_service()

        h1 = ProviderHealthInfo(name="LHM")
        h1.status = ProviderStatus.AVAILABLE
        h2 = ProviderHealthInfo(name="PresentMon")
        h2.status = ProviderStatus.FAILED

        hs.register_provider(h1)
        hs.register_provider(h2)

        assert hs.overall_status == "degraded"

    def test_critical(self):
        hs = _make_health_service()

        h1 = ProviderHealthInfo(name="LHM")
        h1.status = ProviderStatus.FAILED
        h2 = ProviderHealthInfo(name="PM")
        h2.status = ProviderStatus.UNAVAILABLE

        hs.register_provider(h1)
        hs.register_provider(h2)

        assert hs.overall_status == "critical"

    def test_emit_health_metrics(self):
        hs = _make_health_service()
        hs.set_influx_status(True)

        h1 = ProviderHealthInfo(name="LHM")
        h1.status = ProviderStatus.AVAILABLE
        h1.capabilities = {"cpu_temp": True, "gpu_temp": True}
        hs.register_provider(h1)

        metrics = hs.emit_health_metrics()

        # Powinna być metryka agenta + 1 metryka providera
        assert len(metrics) == 2

        # Agent health metric
        agent_m = [m for m in metrics if m.tags.get("info_type") == "agent_health"]
        assert len(agent_m) == 1
        assert agent_m[0].fields["agent_status"] == "healthy"
        assert agent_m[0].fields["influx_connected"] == 1
        assert agent_m[0].fields["providers_total"] == 1
        assert agent_m[0].fields["providers_available"] == 1

        # Provider health metric
        prov_m = [m for m in metrics if m.tags.get("info_type") == "provider_health"]
        assert len(prov_m) == 1
        assert prov_m[0].tags["provider_name"] == "LHM"
        assert prov_m[0].fields["cap_cpu_temp"] == 1
        assert prov_m[0].fields["cap_gpu_temp"] == 1

    def test_provider_summary(self):
        hs = _make_health_service()

        h1 = ProviderHealthInfo(name="LHM")
        h1.status = ProviderStatus.AVAILABLE
        h1.mark_success(100)
        hs.register_provider(h1)

        summary = hs.get_provider_summary()
        assert "LHM" in summary
        assert summary["LHM"]["status"] == "available"
        assert summary["LHM"]["metrics_collected"] == 100
