"""Testy writera InfluxDB i DurableSpool."""
import os
import json
import tempfile
import pytest

from bytetech_agent.models.metrics import MetricData
from bytetech_agent.config import InfluxConfig, BufferConfig
from bytetech_agent.writers.influx_writer import DurableSpool, InfluxWriter


def _make_metrics(n: int = 3) -> list:
    return [
        MetricData(
            measurement_name="pc_hw_raw",
            tags={"host": "TestPC", "sensor_type": "temperature"},
            fields={"value": float(i * 10)},
        )
        for i in range(n)
    ]


class TestDurableSpool:
    def test_store_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            spool = DurableSpool(tmpdir, max_files=10)
            metrics = _make_metrics(5)
            spool.store(metrics, {"site": "test"})

            assert spool.pending_count == 1

            loaded = spool.load_and_clear()
            assert len(loaded) == 1
            assert len(loaded[0]["metrics"]) == 5
            assert loaded[0]["extra_tags"]["site"] == "test"

            # Po load_and_clear powinno być czysto
            assert spool.pending_count == 0

    def test_max_files_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            spool = DurableSpool(tmpdir, max_files=3)

            for i in range(5):
                spool.store([_make_metrics(1)[0]])

            # Powinno być max 3 pliki (2 najstarsze usunięte)
            assert spool.pending_count <= 3

    def test_empty_spool(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            spool = DurableSpool(tmpdir)
            loaded = spool.load_and_clear()
            assert loaded == []

    def test_spool_file_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            spool = DurableSpool(tmpdir)
            spool.store(_make_metrics(2))

            files = os.listdir(tmpdir)
            assert len(files) == 1
            assert files[0].startswith("spool_")
            assert files[0].endswith(".json")

            with open(os.path.join(tmpdir, files[0]), "r") as f:
                data = json.load(f)

            assert "timestamp" in data
            assert "metrics" in data
            assert len(data["metrics"]) == 2
            assert data["metrics"][0]["measurement"] == "pc_hw_raw"


class TestInfluxWriter:
    def test_init_without_connection(self):
        """Writer powinien się zainicjalizować nawet bez działającego InfluxDB."""
        config = InfluxConfig(
            url="http://nonexistent:8086",
            token="fake",
            org="test",
            bucket="test",
        )
        buffer_config = BufferConfig(
            enabled=True,
            spool_dir=tempfile.mkdtemp(),
        )
        writer = InfluxWriter(config, buffer_config)
        # initialize() nie powinno crashować
        writer.initialize()
        assert writer.is_connected is False

    def test_write_without_connection_buffers(self):
        """Metryki powinny być bufferowane gdy brak połączenia."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = InfluxConfig(
                url="http://nonexistent:8086",
                token="fake",
                org="test",
                bucket="test",
            )
            buffer_config = BufferConfig(
                enabled=True,
                spool_dir=os.path.join(tmpdir, "spool"),
            )
            writer = InfluxWriter(config, buffer_config)
            # Nie inicjalizuij – symuluj brak write_api
            metrics = _make_metrics(5)
            writer.write_metrics(metrics, {"site": "test"})

            # Dane powinny być w spool
            spool_files = os.listdir(os.path.join(tmpdir, "spool"))
            assert len(spool_files) >= 1

    def test_metrics_to_points(self):
        """Test konwersji MetricData na InfluxDB Points."""
        config = InfluxConfig(url="http://x:8086", token="t", org="o", bucket="b")
        writer = InfluxWriter(config)

        metrics = [
            MetricData(
                measurement_name="pc_hw_raw",
                tags={"host": "PC1", "sensor_type": "temp"},
                fields={"value": 42.0, "max": 100.0},
            )
        ]

        points = writer._metrics_to_points(metrics, {"site": "Dom"})
        assert len(points) == 1
