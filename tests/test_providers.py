"""Testy providerów – mock tests bazowe, FrameTimingBuffer."""
import pytest
import time

from bytetech_agent.models.metrics import (
    MetricData,
    ProviderContext,
    ProviderStatus,
    ProviderHealthInfo,
)
from bytetech_agent.providers.base import BaseProvider


# ========================= TestBaseProvider =========================



class StubProvider(BaseProvider):
    """Test provider do weryfikacji BaseProvider."""

    def __init__(self, should_init: bool = True, metrics_to_return=None, should_raise=False):
        super().__init__(name="StubProvider")
        self._should_init = should_init
        self._metrics_to_return = metrics_to_return or []
        self._should_raise = should_raise
        self._shutdown_called = False

    def initialize(self) -> bool:
        if self._should_init:
            self._health.status = ProviderStatus.AVAILABLE
            self._health.capabilities = {"test_metric": True}
            return True
        else:
            self._health.mark_unavailable("Test unavailable")
            return False

    def _collect(self, context):
        if self._should_raise:
            raise RuntimeError("Test error")
        return self._metrics_to_return

    def shutdown(self):
        self._shutdown_called = True


class TestBaseProvider:
    def test_init_success(self):
        p = StubProvider(should_init=True)
        assert p.initialize() is True
        assert p.is_available is True
        assert p.name == "StubProvider"

    def test_init_failure(self):
        p = StubProvider(should_init=False)
        assert p.initialize() is False
        assert p.is_available is False

    def test_get_metrics_returns_data(self):
        ctx = ProviderContext(host_alias="PC", host_name="pc")
        test_metrics = [MetricData("test", {"host": "PC"}, {"value": 1.0})]
        p = StubProvider(metrics_to_return=test_metrics)
        p.initialize()

        result = p.get_metrics(ctx)
        assert len(result) == 1
        assert p.health.metrics_collected == 1

    def test_get_metrics_catches_exception(self):
        ctx = ProviderContext(host_alias="PC", host_name="pc")
        p = StubProvider(should_raise=True)
        p.initialize()

        result = p.get_metrics(ctx)
        assert result == []
        assert p.health.status in (ProviderStatus.DEGRADED, ProviderStatus.FAILED)
        assert p.health.last_error is not None

    def test_unavailable_provider_returns_empty(self):
        ctx = ProviderContext(host_alias="PC", host_name="pc")
        p = StubProvider(should_init=False)
        p.initialize()

        result = p.get_metrics(ctx)
        assert result == []

    def test_shutdown(self):
        p = StubProvider()
        p.shutdown()
        assert p._shutdown_called is True


# ========================= TestProviderHealthInfo =========================

class TestProviderHealthInfo:
    def test_mark_success(self):
        h = ProviderHealthInfo(name="test")
        h.mark_success(count=5)
        assert h.status == ProviderStatus.AVAILABLE
        assert h.metrics_collected == 5
        assert h.last_success is not None

    def test_mark_error_first_time(self):
        h = ProviderHealthInfo(name="test")
        h.mark_error("connection refused")
        assert h.status == ProviderStatus.FAILED
        assert h.last_error == "connection refused"

    def test_mark_error_after_success(self):
        h = ProviderHealthInfo(name="test")
        h.mark_success()
        h.mark_error("timeout")
        assert h.status == ProviderStatus.DEGRADED

    def test_mark_unavailable(self):
        h = ProviderHealthInfo(name="test")
        h.mark_unavailable("no DLL")
        assert h.status == ProviderStatus.UNAVAILABLE
