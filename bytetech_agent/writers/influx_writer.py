"""
ByteTech Agent - InfluxDB Writer with Buffer.
Writes data to InfluxDB 2.x with:
- batching
- retry/backoff
- memory buffer
- durable disk spool for InfluxDB outages
"""
import json
import logging
import os
import time
import threading
import glob
from typing import List, Optional
from collections import deque

from influxdb_client import InfluxDBClient, WriteOptions, Point
from influxdb_client.client.exceptions import InfluxDBError

from bytetech_agent.config import InfluxConfig, BufferConfig
from bytetech_agent.models.metrics import MetricData

logger = logging.getLogger(__name__)


class DurableSpool:
    """
    Disk buffer (spool) for InfluxDB outages.
    Saves measurements as JSON files in the spool directory.
    Retries writes when connection is restored.
    """

    def __init__(self, spool_dir: str, max_files: int = 50):
        self._spool_dir = spool_dir
        self._max_files = max_files
        self._lock = threading.Lock()

        os.makedirs(self._spool_dir, exist_ok=True)

    def store(self, metrics: List[MetricData], extra_tags: Optional[dict] = None):
        """Stores metrics to a spool file."""
        with self._lock:
            # Check file limit
            existing = self._list_spool_files()
            if len(existing) >= self._max_files:
                # Remove oldest
                try:
                    os.remove(existing[0])
                except OSError:
                    pass

            filename = f"spool_{int(time.time() * 1000)}.json"
            filepath = os.path.join(self._spool_dir, filename)

            data = {
                "timestamp": time.time(),
                "extra_tags": extra_tags or {},
                "metrics": [
                    {
                        "measurement": m.measurement_name,
                        "tags": m.tags,
                        "fields": m.fields,
                    }
                    for m in metrics
                ],
            }

            try:
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                logger.debug(f"Spool: saved {len(metrics)} metrics to {filename}")
            except Exception as e:
                logger.error(f"Spool: failed to save to {filepath}: {e}")

    def load_and_clear(self) -> List[dict]:
        """Loads and deletes spool files. Returns connection retry data."""
        with self._lock:
            results = []
            for filepath in self._list_spool_files():
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    results.append(data)
                    os.remove(filepath)
                except Exception as e:
                    logger.debug(f"Spool: read error {filepath}: {e}")
            return results

    def _list_spool_files(self) -> List[str]:
        """Returns sorted list of spool files."""
        pattern = os.path.join(self._spool_dir, "spool_*.json")
        files = sorted(glob.glob(pattern))
        return files

    @property
    def pending_count(self) -> int:
        return len(self._list_spool_files())


class InfluxWriter:
    """
    Writes data to InfluxDB 2.x.
    Uses batching, retries, memory buffers, and durable disk spools.
    """

    def __init__(self, config: InfluxConfig, buffer_config: Optional[BufferConfig] = None):
        self._config = config
        self._buffer_config = buffer_config or BufferConfig()
        self._client: Optional[InfluxDBClient] = None
        self._write_api = None
        self._connected = False

        # Memory buffer
        self._memory_buffer: deque = deque(maxlen=self._buffer_config.max_memory_points)
        self._buffer_lock = threading.Lock()

        # Disk spool
        self._spool: Optional[DurableSpool] = None
        if self._buffer_config.enabled:
            self._spool = DurableSpool(
                spool_dir=self._buffer_config.spool_dir,
                max_files=self._buffer_config.max_spool_files,
            )

        # Retry state
        self._consecutive_failures = 0
        self._last_failure_time: float = 0
        self._retry_backoff_sec = 5.0
        self._max_backoff_sec = 120.0

    def initialize(self):
        """Initializes InfluxDB connection."""
        try:
            self._client = InfluxDBClient(
                url=self._config.url,
                token=self._config.token,
                org=self._config.org,
                timeout=15_000,
            )

            self._write_api = self._client.write_api(
                write_options=WriteOptions(
                    batch_size=500,
                    flush_interval=5_000,
                    retry_interval=5_000,
                    max_retries=5,
                    max_retry_delay=30_000,
                    exponential_base=2,
                )
            )

            # Connection test
            health = self._client.health()
            if health.status == "pass":
                self._connected = True
                logger.info(
                    f"InfluxDB Writer ready "
                    f"(URL: {self._config.url}, Bucket: {self._config.bucket})"
                )
            else:
                logger.warning(f"InfluxDB health check: {health.status} - {health.message}")
                self._connected = False

        except Exception as e:
            logger.error(f"Failed to connect to InfluxDB: {e}")
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def test_write(self) -> bool:
        """Test write to verify connection."""
        if not self._write_api:
            return False

        try:
            test_point = Point("bytetech_agent_test").tag("test", "true").field("value", 1)
            self._write_api.write(bucket=self._config.bucket, record=test_point)
            logger.info("InfluxDB test write: OK")
            return True
        except Exception as e:
            logger.error(f"InfluxDB test write: FAIL - {e}")
            return False

    def write_metrics(self, metrics: List[MetricData], extra_tags: Optional[dict] = None):
        """
        Writes metrics to InfluxDB.
        Buffers to memory and disk on failure.
        """
        if not metrics:
            return

        # Check backoff
        if self._consecutive_failures > 0:
            backoff = min(
                self._retry_backoff_sec * (2 ** self._consecutive_failures),
                self._max_backoff_sec,
            )
            if time.time() - self._last_failure_time < backoff:
                self._buffer_metrics(metrics, extra_tags)
                return

        # Write attempt
        points = self._metrics_to_points(metrics, extra_tags)

        try:
            if self._write_api:
                self._write_api.write(bucket=self._config.bucket, record=points)
                logger.debug(f"Saved {len(points)} points to InfluxDB.")
                self._consecutive_failures = 0
                self._connected = True

                # Try flushing spooled data
                self._flush_spool()
            else:
                self._buffer_metrics(metrics, extra_tags)

        except (InfluxDBError, Exception) as e:
            self._consecutive_failures += 1
            self._last_failure_time = time.time()
            self._connected = False

            backoff = min(
                self._retry_backoff_sec * (2 ** self._consecutive_failures),
                self._max_backoff_sec,
            )
            logger.warning(
                f"InfluxDB write failed ({self._consecutive_failures}x): {e}. "
                f"Retry in {backoff:.0f}s. Buffering {len(metrics)} metrics."
            )
            self._buffer_metrics(metrics, extra_tags)

    def _metrics_to_points(self, metrics: List[MetricData], extra_tags: Optional[dict]) -> List[Point]:
        """Converts MetricData to InfluxDB Points."""
        points = []
        for m in metrics:
            p = Point(m.measurement_name)

            for tk, tv in m.tags.items():
                p = p.tag(tk, str(tv))

            if extra_tags:
                for tk, tv in extra_tags.items():
                    p = p.tag(tk, str(tv))

            for fk, fv in m.fields.items():
                p = p.field(fk, fv)

            points.append(p)
        return points

    def _buffer_metrics(self, metrics: List[MetricData], extra_tags: Optional[dict]):
        """Buffers metrics to memory and spool."""
        # Memory
        with self._buffer_lock:
            for m in metrics:
                self._memory_buffer.append((m, extra_tags))

        # Disk spool
        if self._spool and self._buffer_config.enabled:
            self._spool.store(metrics, extra_tags)

    def _flush_spool(self):
        """Attempts to write spool data to InfluxDB upon reconnect."""
        if not self._spool or not self._write_api:
            return

        spool_data = self._spool.load_and_clear()
        if not spool_data:
            return

        total_replayed = 0
        for entry in spool_data:
            try:
                extra_tags = entry.get("extra_tags", {})
                raw_metrics = entry.get("metrics", [])

                metrics = [
                    MetricData(
                        measurement_name=m["measurement"],
                        tags=m["tags"],
                        fields=m["fields"],
                    )
                    for m in raw_metrics
                ]

                points = self._metrics_to_points(metrics, extra_tags)
                self._write_api.write(bucket=self._config.bucket, record=points)
                total_replayed += len(points)

            except Exception as e:
                logger.warning(f"Spool replay failed: {e}")

        if total_replayed:
            logger.info(f"Spool: replayed {total_replayed} points from disk buffer.")

        # Flush memory buffer
        with self._buffer_lock:
            if self._memory_buffer:
                points = []
                while self._memory_buffer:
                    m, etags = self._memory_buffer.popleft()
                    pts = self._metrics_to_points([m], etags)
                    points.extend(pts)

                try:
                    self._write_api.write(bucket=self._config.bucket, record=points)
                    logger.info(f"Memory buffer: replayed {len(points)} points.")
                except Exception as e:
                    logger.warning(f"Memory buffer replay failed: {e}")

    def shutdown(self):
        """Closes writer and flushes buffer."""
        try:
            if self._write_api:
                self._write_api.close()
        except Exception:
            pass

        try:
            if self._client:
                self._client.close()
        except Exception:
            pass

        logger.info("InfluxDB Writer closed.")
