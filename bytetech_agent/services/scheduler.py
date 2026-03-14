"""
ByteTech Agent - Scheduler.
Agent lifecycle manager: provider init, metric collection loops,
normalization, InfluxDB write, health checks, graceful shutdown.
"""
import logging
import signal
import socket
import threading
import time
from typing import List

from bytetech_agent.config import AppConfig
from bytetech_agent.models.metrics import MetricData, ProviderContext
from bytetech_agent.writers.influx_writer import InfluxWriter
from bytetech_agent.normalizers.influx_formatter import InfluxFormatter
from bytetech_agent.services.health import HealthService
from bytetech_agent.providers.base import BaseProvider

# Providers
from bytetech_agent.providers.lhm_provider import LhmProvider
from bytetech_agent.providers.presentmon_provider import PresentMonProvider
from bytetech_agent.providers.display_provider import DisplayProvider
from bytetech_agent.providers.nvapi_provider import NvapiProvider
from bytetech_agent.providers.system_provider import SystemProvider

logger = logging.getLogger(__name__)


class AgentScheduler:
    """
    Agent lifecycle manager.
    Manages providers, metric collection loops, normalization,
    InfluxDB writes, and health checks.
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self._is_running = False
        self._shutdown_event = threading.Event()

        # Writer
        self._writer = InfluxWriter(config.influx, config.buffer)

        # Context
        self._context = ProviderContext(
            host_alias=config.metadata.host_alias,
            host_name=socket.gethostname(),
            site=config.metadata.site,
            owner=config.metadata.owner,
        )

        # Global tags
        self._global_tags = {
            "site": config.metadata.site,
            "owner": config.metadata.owner,
        }
        self._global_tags.update(config.options.tags_extra)

        # Health service
        self._health = HealthService(config.metadata.host_alias)

        # Providers - categorized
        self._hw_providers: List[BaseProvider] = []
        self._fps_providers: List[BaseProvider] = []
        self._state_providers: List[BaseProvider] = []

        # Register providers based on config
        if config.providers.lhm_enabled:
            lhm_json_url = config.lhm.json_url if hasattr(config, 'lhm') else "http://127.0.0.1:8085"
            self._hw_providers.append(LhmProvider(json_url=lhm_json_url))

        if config.providers.nvapi_provider_enabled:
            self._hw_providers.append(NvapiProvider())

        if config.providers.presentmon_enabled:
            self._fps_providers.append(PresentMonProvider(config.presentmon))

        if config.providers.display_provider_enabled:
            self._state_providers.append(DisplayProvider())

        if config.providers.system_provider_enabled:
            self._state_providers.append(SystemProvider())

    def _initialize_providers(self):
        """Initialize all registered providers."""
        all_providers = self._hw_providers + self._fps_providers + self._state_providers

        for provider in all_providers:
            try:
                success = provider.initialize()
                if success:
                    logger.info(f"Provider '{provider.name}' initialized successfully.")
                else:
                    logger.warning(f"Provider '{provider.name}' unavailable, agent continues.")
            except Exception as e:
                logger.error(f"Provider '{provider.name}' init error: {e}")

            # Register in health service regardless of result
            self._health.register_provider(provider.health)

    def _initialize(self):
        """Full agent initialization."""
        logger.info("=" * 60)
        logger.info("ByteTech Agent - Initialization")
        logger.info("=" * 60)

        # Writer
        self._writer.initialize()
        self._health.set_influx_status(self._writer.is_connected)

        # Providers
        self._initialize_providers()

        # Log summary
        logger.info("Provider status:")
        self._health.log_summary()
        logger.info("=" * 60)

    def _loop_hw(self):
        """Hardware metrics collection loop (slower interval)."""
        interval = self.config.timing.hw_interval_sec
        logger.info(f"HW loop started, interval: {interval}s")

        while not self._shutdown_event.is_set():
            metrics: List[MetricData] = []

            # Hardware providers
            for provider in self._hw_providers:
                try:
                    result = provider.get_metrics(self._context)
                    metrics.extend(result)
                except Exception as e:
                    logger.debug(f"[{provider.name}] HW collection error: {e}")

            # State providers (collected at HW interval)
            for provider in self._state_providers:
                try:
                    result = provider.get_metrics(self._context)
                    metrics.extend(result)
                except Exception as e:
                    logger.debug(f"[{provider.name}] State collection error: {e}")

            if metrics:
                # Normalization: raw -> curated
                raw_metrics = [m for m in metrics if m.measurement_name == "pc_hw_raw"]
                other_metrics = [m for m in metrics if m.measurement_name != "pc_hw_raw"]

                curated_metrics = InfluxFormatter.normalize_to_curated(raw_metrics)

                # Custom fields
                all_metrics = raw_metrics + curated_metrics + other_metrics
                all_metrics = InfluxFormatter.enrich_with_custom_fields(
                    all_metrics, self.config.options.custom_fields
                )

                self._writer.write_metrics(all_metrics, self._global_tags)

            # Health metrics (every HW interval)
            self._health.set_influx_status(self._writer.is_connected)
            health_metrics = self._health.emit_health_metrics()
            if health_metrics:
                self._writer.write_metrics(health_metrics, self._global_tags)

            self._shutdown_event.wait(interval)

    def _loop_fps(self):
        """FPS metrics collection loop (faster interval)."""
        interval = self.config.timing.fps_interval_sec
        logger.info(f"FPS loop started, interval: {interval}s")

        while not self._shutdown_event.is_set():
            metrics: List[MetricData] = []

            for provider in self._fps_providers:
                try:
                    result = provider.get_metrics(self._context)
                    metrics.extend(result)
                except Exception as e:
                    logger.debug(f"[{provider.name}] FPS collection error: {e}")

            if metrics:
                metrics = InfluxFormatter.enrich_with_custom_fields(
                    metrics, self.config.options.custom_fields
                )
                self._writer.write_metrics(metrics, self._global_tags)

            self._shutdown_event.wait(interval)

    def start(self):
        """Start the agent with collection loops in separate threads."""
        self._is_running = True
        self._shutdown_event.clear()
        self._initialize()

        # Threads
        threads = []

        hw_thread = threading.Thread(target=self._loop_hw, daemon=True, name="ByteTech-HW")
        hw_thread.start()
        threads.append(hw_thread)

        if self._fps_providers:
            fps_thread = threading.Thread(target=self._loop_fps, daemon=True, name="ByteTech-FPS")
            fps_thread.start()
            threads.append(fps_thread)

        logger.info("ByteTech Agent started and running.")

        # Wait for shutdown signal
        try:
            while not self._shutdown_event.is_set():
                self._shutdown_event.wait(1.0)
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received, shutting down...")
            self.stop()

    def stop(self):
        """Graceful agent shutdown."""
        logger.info("=" * 60)
        logger.info("ByteTech Agent - Shutdown")
        logger.info("=" * 60)

        self._is_running = False
        self._shutdown_event.set()

        # Shutdown providers
        all_providers = self._hw_providers + self._fps_providers + self._state_providers
        for provider in all_providers:
            try:
                provider.shutdown()
                logger.debug(f"Provider '{provider.name}' shut down.")
            except Exception as e:
                logger.debug(f"Error shutting down provider '{provider.name}': {e}")

        # Flush and close writer
        self._writer.shutdown()

        logger.info("ByteTech Agent stopped.")

    def request_shutdown(self):
        """Request graceful shutdown from outside (e.g., signal handler)."""
        self.stop()
