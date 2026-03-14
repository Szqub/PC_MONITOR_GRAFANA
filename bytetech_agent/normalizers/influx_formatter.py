"""
ByteTech Agent – Normalizer / InfluxFormatter.
Przekształca surowe metryki z LHM (pc_hw_raw) na znormalizowane metryki (pc_hw_curated).
Curated = stabilne, gotowe pod dashboardy pola.
"""
import logging
from typing import List, Dict, Any, Optional

from bytetech_agent.models.metrics import MetricData

logger = logging.getLogger(__name__)

# Mapowanie sensor_name z LHM na znormalizowane pola curated
# Klucz: (device_class, sensor_type, nazwa_zawiera) → pole curated
_CURATED_MAP = {
    # CPU
    ("cpu", "temperature", "package"): "cpu_package_temp_c",
    ("cpu", "temperature", "core"): None,  # per-core → osobne pole dynamiczne
    ("cpu", "load", "cpu total"): "cpu_total_load_percent",
    ("cpu", "load", "core"): None,  # per-core
    ("cpu", "clock", "core"): None,  # per-core
    ("cpu", "power", "package"): "cpu_package_power_w",
    ("cpu", "power", "cores"): "cpu_cores_power_w",
    ("cpu", "voltage", "core"): "cpu_core_voltage_v",

    # GPU (dGPU/iGPU)
    ("dgpu", "temperature", "gpu core"): "gpu_temp_c",
    ("dgpu", "temperature", "gpu hot spot"): "gpu_hotspot_temp_c",
    ("dgpu", "temperature", "hotspot"): "gpu_hotspot_temp_c",
    ("dgpu", "load", "gpu core"): "gpu_load_percent",
    ("dgpu", "load", "gpu memory"): "gpu_memory_load_percent",
    ("dgpu", "clock", "gpu core"): "gpu_core_clock_mhz",
    ("dgpu", "clock", "gpu memory"): "gpu_memory_clock_mhz",
    ("dgpu", "power", "gpu"): "gpu_power_w",
    ("dgpu", "power", "package"): "gpu_power_w",
    ("dgpu", "smalldata", "gpu memory used"): "gpu_vram_used_mb",
    ("dgpu", "smalldata", "gpu memory total"): "gpu_vram_total_mb",
    ("dgpu", "smalldata", "gpu memory free"): "gpu_vram_free_mb",
    ("dgpu", "smalldata", "d3d dedicated memory used"): "gpu_vram_used_mb",
    ("dgpu", "data", "gpu memory used"): "gpu_vram_used_gb",
    ("dgpu", "data", "gpu memory total"): "gpu_vram_total_gb",
    ("dgpu", "fan", "gpu"): "gpu_fan_rpm",
    ("dgpu", "control", "gpu fan"): "gpu_fan_percent",

    ("igpu", "temperature", "gpu core"): "igpu_temp_c",
    ("igpu", "load", "gpu core"): "igpu_load_percent",
    ("igpu", "clock", "gpu core"): "igpu_core_clock_mhz",

    # RAM (z LHM)
    ("ram", "data", "memory used"): "ram_used_gb",
    ("ram", "data", "memory available"): "ram_available_gb",
    ("ram", "load", "memory"): "ram_used_percent",

    # Storage
    ("storage", "temperature", "temperature"): "storage_temp_c",
    ("storage", "load", "used space"): "storage_used_percent",
    ("storage", "data", "data read"): "storage_data_read_gb",
    ("storage", "data", "data written"): "storage_data_written_gb",
    ("storage", "throughput", "read rate"): "storage_read_rate_bps",
    ("storage", "throughput", "write rate"): "storage_write_rate_bps",

    # Motherboard
    ("motherboard", "temperature", "temperature"): "mb_temp_c",
    ("motherboard", "fan", "fan"): None,  # dynamiczne
    ("motherboard", "voltage", "vcore"): "mb_vcore_v",
}


def _match_curated_field(device_class: str, sensor_type: str, sensor_name: str) -> Optional[str]:
    """Dopasowuje sensor do pola curated."""
    sensor_name_lower = sensor_name.lower()
    sensor_type_lower = sensor_type.lower()
    device_class_lower = device_class.lower()

    # Block static threshold/limit sensors for storage devices
    # (e.g. "Temperature Limit", "Critical Temperature", "Temperature Trip")
    if device_class_lower == "storage" and sensor_type_lower == "temperature":
        if any(bad in sensor_name_lower for bad in ["limit", "critical", "trip", "warning"]):
            return None

    # Dokładne dopasowanie
    for (dc, st, name_part), field_name in _CURATED_MAP.items():
        if dc == device_class_lower and st == sensor_type_lower and name_part in sensor_name_lower:
            return field_name

    return None


class InfluxFormatter:
    """
    Moduł normalizacji metryk:
    - normalize_to_curated: LHM raw → pc_hw_curated (znormalizowane pola dashboardowe)
    - enrich_with_custom_fields: dodaje custom pola z konfiguracji
    """

    @staticmethod
    def normalize_to_curated(raw_metrics: List[MetricData]) -> List[MetricData]:
        """
        Przekształca surowe metryki LHM (pc_hw_raw) na dedykowane metryki rozdzielone per klasa urządzenia (pc_cpu, pc_gpu, itp).
        Grupuje metryki po (host, device_class, device_name) → jedno MetricData per urządzenie.
        """
        # Grupowanie: (host, device_class, device_name) → {field: value}
        curated_groups: Dict[tuple, Dict[str, Any]] = {}
        curated_tags: Dict[tuple, Dict[str, str]] = {}

        # Liczniki per-core
        per_core_temps: Dict[tuple, List[float]] = {}
        per_core_loads: Dict[tuple, List[float]] = {}
        per_core_clocks: Dict[tuple, List[float]] = {}
        fan_data: Dict[tuple, Dict[str, float]] = {}

        for metric in raw_metrics:
            if metric.measurement_name != "pc_hw_raw":
                continue

            host = metric.tags.get("host", "unknown")
            device_class = metric.tags.get("device_class", "other")
            device_name = metric.tags.get("device_name", "unknown")
            sensor_type = metric.tags.get("sensor_type", "")
            sensor_name = metric.tags.get("sensor_name", "")
            value = metric.fields.get("value")

            if value is None:
                continue

            key = (host, device_class, device_name)

            if key not in curated_groups:
                curated_groups[key] = {}
                curated_tags[key] = {
                    "host": host,
                    "device_class": device_class,
                    "device_name": device_name,
                }

            # Dopasuj do curated field
            curated_field = _match_curated_field(device_class, sensor_type, sensor_name)

            if curated_field:
                curated_groups[key][curated_field] = float(value)
            elif curated_field is None:
                # Per-core i per-fan dane
                sn_lower = sensor_name.lower()
                st_lower = sensor_type.lower()

                if device_class == "cpu" and st_lower == "temperature" and "core" in sn_lower:
                    if key not in per_core_temps:
                        per_core_temps[key] = []
                    per_core_temps[key].append(float(value))

                elif device_class == "cpu" and st_lower == "load" and "core" in sn_lower:
                    if key not in per_core_loads:
                        per_core_loads[key] = []
                    per_core_loads[key].append(float(value))

                elif device_class == "cpu" and st_lower == "clock" and "core" in sn_lower:
                    if key not in per_core_clocks:
                        per_core_clocks[key] = []
                    per_core_clocks[key].append(float(value))

                elif device_class == "motherboard" and st_lower == "fan":
                    if key not in fan_data:
                        fan_data[key] = {}
                    # Czyść nazwę fana
                    fan_key = sensor_name.lower().replace(" ", "_").replace("#", "")
                    fan_data[key][f"fan_{fan_key}_rpm"] = float(value)

        # Dodaj per-core agregaty
        for key, temps in per_core_temps.items():
            if key in curated_groups and temps:
                curated_groups[key]["cpu_core_temp_avg_c"] = round(sum(temps) / len(temps), 1)
                curated_groups[key]["cpu_core_temp_max_c"] = round(max(temps), 1)
                curated_groups[key]["cpu_core_count_temp"] = len(temps)

        for key, loads in per_core_loads.items():
            if key in curated_groups and loads:
                curated_groups[key]["cpu_core_load_avg_percent"] = round(sum(loads) / len(loads), 1)
                curated_groups[key]["cpu_core_load_max_percent"] = round(max(loads), 1)

        for key, clocks in per_core_clocks.items():
            if key in curated_groups and clocks:
                curated_groups[key]["cpu_core_clock_avg_mhz"] = round(sum(clocks) / len(clocks), 0)
                curated_groups[key]["cpu_core_clock_max_mhz"] = round(max(clocks), 0)

        for key, fans in fan_data.items():
            if key in curated_groups:
                curated_groups[key].update(fans)

        # Mapping device_class to split measurement names
        measurement_routing = {
            "cpu": "pc_cpu",
            "dgpu": "pc_gpu",
            "igpu": "pc_gpu",
            "ram": "pc_memory",
            "storage": "pc_storage",
            "motherboard": "pc_motherboard",
        }

        # Buduj curated MetricData
        curated_metrics: List[MetricData] = []
        for key, fields in curated_groups.items():
            if fields:  # Nie emituj pustych
                device_class = curated_tags[key]["device_class"]
                measurement_name = measurement_routing.get(device_class, "pc_hw_curated")
                curated_metrics.append(MetricData(
                    measurement_name=measurement_name,
                    tags=curated_tags[key],
                    fields=fields,
                ))

        return curated_metrics

    @staticmethod
    def enrich_with_custom_fields(
        metrics: List[MetricData],
        custom_fields: Dict[str, Any],
    ) -> List[MetricData]:
        """Wzbogaca metryki o dodatkowe pola z konfiguracji."""
        if not custom_fields:
            return metrics

        for metric in metrics:
            for k, v in custom_fields.items():
                metric.fields[f"custom_{k}"] = v

        return metrics
