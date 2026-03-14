"""Testy normalizera – transformacja pc_hw_raw → pc_hw_curated."""
import pytest

from bytetech_agent.models.metrics import MetricData
from bytetech_agent.normalizers.influx_formatter import InfluxFormatter


def _make_raw_metric(device_class: str, sensor_type: str, sensor_name: str,
                     value: float, device_name: str = "TestDevice") -> MetricData:
    """Helper: tworzy surową metrykę LHM (pc_hw_raw)."""
    return MetricData(
        measurement_name="pc_hw_raw",
        tags={
            "host": "TestPC",
            "device_class": device_class,
            "device_name": device_name,
            "sensor_type": sensor_type,
            "sensor_name": sensor_name,
            "identifier": f"/{device_class}/0/{sensor_type}/0",
        },
        fields={"value": value},
    )


class TestNormalizeToCurated:
    def test_empty_input(self):
        result = InfluxFormatter.normalize_to_curated([])
        assert result == []

    def test_non_raw_metrics_ignored(self):
        metric = MetricData(
            measurement_name="pc_fps",
            tags={"host": "PC"},
            fields={"fps_now": 60.0},
        )
        result = InfluxFormatter.normalize_to_curated([metric])
        assert result == []

    def test_cpu_package_temp(self):
        raw = [_make_raw_metric("cpu", "temperature", "CPU Package", 65.0)]
        curated = InfluxFormatter.normalize_to_curated(raw)
        assert len(curated) == 1
        assert curated[0].measurement_name == "pc_cpu"
        assert curated[0].tags["device_class"] == "cpu"
        assert "cpu_package_temp_c" in curated[0].fields
        assert curated[0].fields["cpu_package_temp_c"] == 65.0

    def test_cpu_total_load(self):
        raw = [_make_raw_metric("cpu", "load", "CPU Total", 42.5)]
        curated = InfluxFormatter.normalize_to_curated(raw)
        assert len(curated) == 1
        assert curated[0].fields.get("cpu_total_load_percent") == 42.5

    def test_gpu_temperature(self):
        raw = [_make_raw_metric("dgpu", "temperature", "GPU Core", 72.0, "RTX 4070")]
        curated = InfluxFormatter.normalize_to_curated(raw)
        assert len(curated) == 1
        assert curated[0].fields.get("gpu_temp_c") == 72.0
        assert curated[0].tags["device_name"] == "RTX 4070"

    def test_gpu_load_and_clock(self):
        raw = [
            _make_raw_metric("dgpu", "load", "GPU Core", 95.0),
            _make_raw_metric("dgpu", "clock", "GPU Core", 2100.0),
            _make_raw_metric("dgpu", "clock", "GPU Memory", 7500.0),
        ]
        curated = InfluxFormatter.normalize_to_curated(raw)
        assert len(curated) == 1
        assert curated[0].fields.get("gpu_load_percent") == 95.0
        assert curated[0].fields.get("gpu_core_clock_mhz") == 2100.0
        assert curated[0].fields.get("gpu_memory_clock_mhz") == 7500.0

    def test_ram_metrics(self):
        raw = [
            _make_raw_metric("ram", "data", "Memory Used", 12.5),
            _make_raw_metric("ram", "data", "Memory Available", 19.5),
            _make_raw_metric("ram", "load", "Memory", 39.0),
        ]
        curated = InfluxFormatter.normalize_to_curated(raw)
        assert len(curated) == 1
        assert curated[0].fields.get("ram_used_gb") == 12.5
        assert curated[0].fields.get("ram_available_gb") == 19.5
        assert curated[0].fields.get("ram_used_percent") == 39.0

    def test_storage_temperature(self):
        raw = [_make_raw_metric("storage", "temperature", "Temperature", 38.0, "Samsung 990 Pro")]
        curated = InfluxFormatter.normalize_to_curated(raw)
        assert len(curated) == 1
        assert curated[0].fields.get("storage_temp_c") == 38.0

    def test_per_core_aggregation(self):
        raw = [
            _make_raw_metric("cpu", "temperature", "CPU Package", 65.0),  # direct map
            _make_raw_metric("cpu", "temperature", "Core #0", 63.0),
            _make_raw_metric("cpu", "temperature", "Core #1", 67.0),
            _make_raw_metric("cpu", "temperature", "Core #2", 61.0),
            _make_raw_metric("cpu", "temperature", "Core #3", 69.0),
        ]
        curated = InfluxFormatter.normalize_to_curated(raw)
        assert len(curated) == 1
        fields = curated[0].fields
        assert fields["cpu_package_temp_c"] == 65.0
        assert fields["cpu_core_temp_avg_c"] == 65.0  # (63+67+61+69)/4
        assert fields["cpu_core_temp_max_c"] == 69.0
        assert fields["cpu_core_count_temp"] == 4

    def test_multiple_devices_grouped(self):
        raw = [
            _make_raw_metric("cpu", "temperature", "CPU Package", 65.0, "Intel i7"),
            _make_raw_metric("dgpu", "temperature", "GPU Core", 72.0, "RTX 4070"),
        ]
        curated = InfluxFormatter.normalize_to_curated(raw)
        assert len(curated) == 2

        cpu = [c for c in curated if c.tags["device_class"] == "cpu"]
        gpu = [c for c in curated if c.tags["device_class"] == "dgpu"]
        assert len(cpu) == 1
        assert len(gpu) == 1


class TestEnrichWithCustomFields:
    def test_no_custom_fields(self):
        metrics = [MetricData("test", {}, {"value": 1})]
        result = InfluxFormatter.enrich_with_custom_fields(metrics, {})
        assert "custom_" not in str(result[0].fields)

    def test_adds_custom_fields(self):
        metrics = [MetricData("test", {}, {"value": 1})]
        result = InfluxFormatter.enrich_with_custom_fields(metrics, {"env": "prod"})
        assert result[0].fields.get("custom_env") == "prod"
