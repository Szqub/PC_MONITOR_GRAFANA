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
from bytetech_agent.providers.presentmon_provider import FrameTimingBuffer


# ========================= TestFrameTimingBuffer =========================

class TestFrameTimingBuffer:
    def test_empty_stats(self):
        buf = FrameTimingBuffer()
        assert buf.get_stats(10.0) is None
        assert buf.get_latest() is None

    def test_add_and_get_latest(self):
        buf = FrameTimingBuffer()
        buf.add_sample(16.6)
        assert buf.get_latest() == 16.6

    def test_stats_calculation(self):
        buf = FrameTimingBuffer()
        # Dodaj 100 sampleów (frametime = 16.6ms → ~60 FPS)
        for _ in range(100):
            buf.add_sample(16.6)

        stats = buf.get_stats(10.0)
        assert stats is not None
        assert abs(stats["fps_avg"] - 60.24) < 1.0  # ~60 FPS
        assert stats["fps_1pct"] > 0
        assert stats["fps_0_1pct"] > 0
        assert stats["sample_count"] == 100

    def test_mixed_frametimes(self):
        buf = FrameTimingBuffer()
        # 90 sampleów @ 16.6ms + 10 sampleów @ 33.3ms (stutter)
        for _ in range(90):
            buf.add_sample(16.6)
        for _ in range(10):
            buf.add_sample(33.3)

        stats = buf.get_stats(10.0)
        assert stats is not None
        # Avg powinno być między 60 i 30 FPS
        assert 30 < stats["fps_avg"] < 65
        # 1% low powinno być bliżej 30 FPS (wolne ramki)
        assert stats["fps_1pct"] < stats["fps_avg"]

    def test_window_filtering(self):
        buf = FrameTimingBuffer(max_seconds=5)
        buf.add_sample(16.6)

        # Powinny być dane
        stats = buf.get_stats(10.0)
        # Tylko 1 sample – za mało
        assert stats is None  # Potrzeba min 2 sampleów

        buf.add_sample(16.6)
        stats = buf.get_stats(10.0)
        assert stats is not None

    def test_clear(self):
        buf = FrameTimingBuffer()
        buf.add_sample(16.6)
        buf.clear()
        assert buf.get_latest() is None


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
