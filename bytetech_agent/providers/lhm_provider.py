"""
ByteTech Agent - LHM Provider.
Collects hardware metrics from LibreHardwareMonitor.
Supports three backends (fallback chain):
  1. WMI root\\LibreHardwareMonitor
  2. WMI root\\OpenHardwareMonitor
  3. HTTP JSON API (http://host:8085/data.json)
Generates pc_hw_raw with device_class / device_name tags.
"""
import json
import logging
import urllib.request
import urllib.error
from typing import List, Dict, Optional, Tuple

from bytetech_agent.providers.base import BaseProvider
from bytetech_agent.models.metrics import MetricData, ProviderContext, ProviderStatus

logger = logging.getLogger(__name__)

# Hardware type mapping (LHM WMI / JSON)
_HW_TYPE_MAP: Dict[str, str] = {
    "CPU": "cpu",
    "GpuNvidia": "dgpu",
    "GpuAmd": "dgpu",
    "GpuIntel": "igpu",
    "RAM": "ram",
    "Motherboard": "motherboard",
    "SuperIO": "motherboard",
    "Storage": "storage",
    "Network": "network",
    "Battery": "battery",
    "PSU": "psu",
    "Cooler": "cooler",
    "EmbeddedController": "ec",
}

# Sensor type mapping (LHM WMI / JSON)
_SENSOR_TYPE_MAP: Dict[str, str] = {
    "Voltage": "voltage",
    "Current": "current",
    "Power": "power",
    "Clock": "clock",
    "Temperature": "temperature",
    "Load": "load",
    "Frequency": "frequency",
    "Fan": "fan",
    "Flow": "flow",
    "Control": "control",
    "Level": "level",
    "Factor": "factor",
    "Data": "data",
    "SmallData": "smalldata",
    "Throughput": "throughput",
    "TimeSpan": "timespan",
    "Energy": "energy",
    "Noise": "noise",
    "Humidity": "humidity",
    "RawValue": "raw",
}

# JSON API: ImageURL-based type detection
_JSON_IMAGE_MAP: Dict[str, str] = {
    "cpu.png": "CPU",
    "nvidia.png": "GpuNvidia",
    "amd.png": "GpuAmd",
    "intel.png": "GpuIntel",
    "ram.png": "RAM",
    "mainboard.png": "Motherboard",
    "hdd.png": "Storage",
    "nic.png": "Network",
    "battery.png": "Battery",
    "psu.png": "PSU",
}


def _classify_hardware(identifier: str, hw_type_raw: str) -> Tuple[str, str]:
    """Returns (device_class, device_name) from identifier and type."""
    device_class = _HW_TYPE_MAP.get(hw_type_raw, "other")
    parts = identifier.strip("/").split("/")
    device_name = parts[0] if parts else hw_type_raw
    return device_class, device_name


class LhmProvider(BaseProvider):
    """
    Hardware metrics provider via LibreHardwareMonitor.
    Supports WMI (LHM/OHM) and JSON API backends.
    Generates metrics for measurement pc_hw_raw.
    """

    def __init__(self, json_url: str = "http://127.0.0.1:8085"):
        super().__init__(name="LHM")
        self._wmi_client = None
        self._json_api_url: str = json_url.rstrip("/")
        self._active_backend: Optional[str] = None
        self._hardware_cache: Dict[str, Tuple[str, str, str]] = {}

    def initialize(self) -> bool:
        # Try WMI backends first, then JSON API
        if self._try_wmi_backend():
            return True
        if self._try_json_api_backend():
            return True

        logger.warning("LHM Provider: no backend available (WMI and JSON API both failed)")
        self._health.mark_unavailable("No LHM backend available (WMI namespaces missing, JSON API unreachable)")
        return False

    def _try_wmi_backend(self) -> bool:
        """Try WMI namespaces: LibreHardwareMonitor, then OpenHardwareMonitor."""
        try:
            import pythoncom
            import wmi

            pythoncom.CoInitialize()

            for namespace, backend_name in [
                (r"root\LibreHardwareMonitor", "wmi_lhm"),
                (r"root\OpenHardwareMonitor", "wmi_ohm"),
            ]:
                try:
                    client = wmi.WMI(namespace=namespace)
                    client.Sensor()  # test query
                    self._wmi_client = client
                    self._active_backend = backend_name
                    logger.info(f"LHM Provider initialized (backend: {backend_name}, namespace: {namespace})")
                    self._build_hardware_cache()
                    self._mark_available()
                    return True
                except Exception:
                    continue

            return False

        except ImportError:
            logger.debug("WMI/pythoncom modules not available, skipping WMI backends")
            return False
        except Exception as e:
            logger.debug(f"WMI backend init failed: {e}")
            return False

    def _try_json_api_backend(self) -> bool:
        """Try LHM JSON API at configured URL."""
        try:
            url = f"{self._json_api_url}/data.json"
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    if isinstance(data, dict) and "Children" in data:
                        self._active_backend = "json_api"
                        logger.info(f"LHM Provider initialized (backend: json_api, url: {self._json_api_url})")
                        self._mark_available()
                        return True
            return False
        except Exception as e:
            logger.debug(f"JSON API backend not available: {e}")
            return False

    def _mark_available(self):
        """Set provider as available with capabilities."""
        self._health.capabilities = {
            "cpu_temp": True,
            "cpu_load": True,
            "gpu_temp": True,
            "gpu_load": True,
            "ram": True,
            "storage": True,
            "fans": True,
            "motherboard": True,
            "backend": True,
        }
        self._health.status = ProviderStatus.AVAILABLE

    @property
    def active_backend(self) -> Optional[str]:
        return self._active_backend

    def _build_hardware_cache(self):
        """Build hardware cache from WMI (identifier -> device info)."""
        if not self._wmi_client:
            return
        try:
            import pythoncom
            pythoncom.CoInitialize()
            hardware_list = self._wmi_client.Hardware()
            for hw in hardware_list:
                hw_id = getattr(hw, "Identifier", "")
                hw_type_raw = getattr(hw, "HardwareType", "Unknown")
                hw_name = getattr(hw, "Name", "Unknown")
                device_class, _ = _classify_hardware(hw_id, hw_type_raw)
                self._hardware_cache[hw_id] = (device_class, hw_name, hw_type_raw)
        except Exception as e:
            logger.debug(f"Failed to build hardware cache: {e}")

    def _find_device_info(self, sensor_identifier: str) -> Tuple[str, str]:
        """Find device_class and device_name for a sensor from cache."""
        for hw_id, (device_class, device_name, _) in self._hardware_cache.items():
            if sensor_identifier.startswith(hw_id):
                return device_class, device_name

        # Fallback: extract from identifier
        parts = sensor_identifier.strip("/").split("/")
        if len(parts) >= 2:
            class_hint = parts[0].lower()
            if "cpu" in class_hint:
                return "cpu", parts[0]
            elif "gpu" in class_hint:
                return "dgpu", parts[0]
            elif "ram" in class_hint or "memory" in class_hint:
                return "ram", parts[0]
            elif "nvme" in class_hint or "storage" in class_hint or "hdd" in class_hint or "ssd" in class_hint:
                return "storage", parts[0]
        return "other", "unknown"

    def _collect(self, context: ProviderContext) -> List[MetricData]:
        if self._active_backend in ("wmi_lhm", "wmi_ohm"):
            return self._collect_wmi(context)
        elif self._active_backend == "json_api":
            return self._collect_json_api(context)
        return []

    def _collect_wmi(self, context: ProviderContext) -> List[MetricData]:
        """Collect metrics via WMI."""
        import pythoncom
        pythoncom.CoInitialize()

        if not self._wmi_client:
            return []

        metrics: List[MetricData] = []
        try:
            sensors = self._wmi_client.Sensor()
            for sensor in sensors:
                stype = getattr(sensor, "SensorType", "Unknown")
                name = getattr(sensor, "Name", "Unknown")
                value = getattr(sensor, "Value", None)
                identifier = getattr(sensor, "Identifier", "")
                parent = getattr(sensor, "Parent", "")

                if value is None:
                    continue

                device_class, device_name = self._find_device_info(
                    parent if parent else identifier
                )
                sensor_type_clean = _SENSOR_TYPE_MAP.get(stype, stype.lower() if stype else "unknown")

                tags = {
                    "host": context.host_alias,
                    "device_class": device_class,
                    "device_name": device_name,
                    "sensor_type": sensor_type_clean,
                    "sensor_name": name,
                    "identifier": identifier,
                }
                fields: Dict[str, float] = {"value": float(value)}

                min_val = getattr(sensor, "Min", None)
                max_val = getattr(sensor, "Max", None)
                if min_val is not None:
                    fields["min"] = float(min_val)
                if max_val is not None:
                    fields["max"] = float(max_val)

                metrics.append(MetricData(
                    measurement_name="pc_hw_raw",
                    tags=tags,
                    fields=fields,
                ))
        except Exception as e:
            logger.error(f"Error reading LHM WMI sensors: {e}")
            raise
        finally:
            pythoncom.CoUninitialize()

        return metrics

    def _collect_json_api(self, context: ProviderContext) -> List[MetricData]:
        """Collect metrics via LHM JSON API (http://host:8085/data.json)."""
        url = f"{self._json_api_url}/data.json"
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.error(f"Error fetching LHM JSON API ({url}): {e}")
            raise

        metrics: List[MetricData] = []
        # data.json has a recursive tree structure:
        # root -> Children (hardware nodes) -> Children (sensor categories) -> Children (sensors)
        self._walk_json_tree(data, context, metrics, hw_type="", hw_name="", hw_identifier="")
        return metrics

    def _walk_json_tree(
        self,
        node: dict,
        context: ProviderContext,
        metrics: List[MetricData],
        depth: int = 0,
        **kwargs
    ):
        """
        Recursively walk the LHM data.json tree dynamically.
        """
        # Inherit context from parents
        hw_type = kwargs.get("hw_type", "")
        hw_name = kwargs.get("hw_name", "")
        hw_identifier = kwargs.get("hw_identifier", "")
        sensor_category = kwargs.get("sensor_category", "")

        children = node.get("Children", [])
        text = node.get("Text", "")
        image_url = node.get("ImageURL", "")
        value_str = node.get("Value", "")
        min_str = node.get("Min", "")
        max_str = node.get("Max", "")
        node_type = node.get("Type", "")

        # Detect Hardware Nodes (Level where HardwareId exists or it looks like hardware image)
        if "HardwareId" in node or (image_url and any(x in image_url for x in ["cpu", "nvidia", "amd", "ram", "hdd", "mainboard"])):
            hw_name = text
            hw_identifier = node.get("HardwareId", text)
            
            img_name = image_url.split("/")[-1] if image_url else ""
            hw_type = _JSON_IMAGE_MAP.get(img_name, "")
            if not hw_type:
                text_lower = text.lower()
                if "cpu" in text_lower or "processor" in text_lower or "ryzen" in text_lower or "intel" in text_lower:
                    hw_type = "CPU"
                elif "nvidia" in text_lower or "geforce" in text_lower or "rtx" in text_lower:
                    hw_type = "GpuNvidia"
                elif "radeon" in text_lower or "rx" in text_lower:
                    hw_type = "GpuAmd"
                elif "memory" in text_lower or "ram" in text_lower:
                    hw_type = "RAM"
                elif "motherboard" in text_lower or "aorus" in text_lower or "msi" in text_lower:
                    hw_type = "Motherboard"
                elif any(s in text_lower for s in ["ssd", "nvme", "disk"]):
                    hw_type = "Storage"

        # Detect Sensor Categories (e.g. "Temperatures", "Loads")
        if not value_str and len(children) > 0:
            guessed_cat = self._guess_sensor_type(text)
            if guessed_cat != text: # Match found
                sensor_category = guessed_cat

        # Actual sensor with value
        if value_str and "SensorId" in node:
            parsed_value = self._parse_sensor_value(value_str)
            if parsed_value is not None and hw_type:
                device_class = _HW_TYPE_MAP.get(hw_type, "other")
                
                cat = node_type if node_type else sensor_category
                if not cat: cat = "unknown"
                sensor_type_clean = _SENSOR_TYPE_MAP.get(cat, cat.lower())

                tags = {
                    "host": context.host_alias,
                    "device_class": device_class,
                    "device_name": hw_name,
                    "sensor_type": sensor_type_clean,
                    "sensor_name": text,
                    "identifier": node.get("SensorId", f"/{hw_identifier}/{sensor_type_clean}/{text}"),
                }
                fields: Dict[str, float] = {"value": parsed_value}

                min_v = self._parse_sensor_value(min_str)
                max_v = self._parse_sensor_value(max_str)
                if min_v is not None:
                    fields["min"] = min_v
                if max_v is not None:
                    fields["max"] = max_v

                metrics.append(MetricData(
                    measurement_name="pc_hw_raw",
                    tags=tags,
                    fields=fields,
                ))

        # Recurse into children
        for child in children:
            self._walk_json_tree(
                child, context, metrics,
                hw_type=hw_type,
                hw_name=hw_name,
                hw_identifier=hw_identifier,
                sensor_category=sensor_category,
                depth=depth + 1,
            )

    @staticmethod
    def _guess_sensor_type(text: str) -> str:
        """Guess LHM sensor type from category text."""
        text_lower = text.lower()
        mapping = {
            "temperature": "Temperature",
            "clock": "Clock",
            "load": "Load",
            "fan": "Fan",
            "voltage": "Voltage",
            "power": "Power",
            "data": "Data",
            "small data": "SmallData",
            "throughput": "Throughput",
            "level": "Level",
            "control": "Control",
            "current": "Current",
            "frequency": "Frequency",
            "factor": "Factor",
            "energy": "Energy",
            "noise": "Noise",
        }
        for key, val in mapping.items():
            if key in text_lower:
                return val
        return text

    @staticmethod
    def _parse_sensor_value(value_str: str) -> Optional[float]:
        """Parse a sensor value string like '45.2 C' or '1200 MHz' to float."""
        if not value_str or value_str == "-":
            return None
        # Strip units: '45.2 °C' -> '45.2', '1200 MHz' -> '1200'
        cleaned = ""
        for ch in value_str:
            if ch.isdigit() or ch in ".-+":
                cleaned += ch
            elif ch == "," :
                cleaned += "."  # Handle comma decimal
            elif cleaned:
                break  # Stop at first non-numeric after digits
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None

    def shutdown(self):
        self._wmi_client = None
        self._hardware_cache.clear()
        self._active_backend = None
