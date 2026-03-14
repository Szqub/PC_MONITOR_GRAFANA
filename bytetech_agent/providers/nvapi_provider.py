"""
ByteTech Agent - NVAPI Provider (NVIDIA NVML).
Collects extended NVIDIA card metrics invisible to LHM:
temperature, power, fan speed, memory info, encoder/decoder, throttle reasons.
Can act as a complement to LHM or a standalone fallback.
"""
import logging
from typing import List, Optional

from bytetech_agent.providers.base import BaseProvider
from bytetech_agent.models.metrics import MetricData, ProviderContext, ProviderStatus

logger = logging.getLogger(__name__)

# Throttle reason flag mapping (NVML)
_THROTTLE_REASONS = {
    0x0000000000000001: "gpu_idle",
    0x0000000000000002: "app_clock_setting",
    0x0000000000000004: "sw_power_cap",
    0x0000000000000008: "hw_slowdown",
    0x0000000000000010: "sync_boost",
    0x0000000000000020: "sw_thermal_slowdown",
    0x0000000000000040: "hw_thermal_slowdown",
    0x0000000000000080: "hw_power_brake_slowdown",
    0x0000000000000100: "display_clock_setting",
}


class NvapiProvider(BaseProvider):
    """
    Provider of NVIDIA-specific information via NVML (pynvml).
    Collects extended metrics: temperature, power, fans, memory, encoder/decoder,
    throttle reasons, power limits. Metrics go to 'pc_hw_curated'.
    """

    def __init__(self):
        super().__init__(name="NVAPI")
        self._pynvml = None

    def initialize(self) -> bool:
        try:
            import pynvml

            self._pynvml = pynvml
            pynvml.nvmlInit()

            device_count = pynvml.nvmlDeviceGetCount()
            if device_count == 0:
                logger.warning("NVML: no NVIDIA devices. Provider disabled.")
                self._health.mark_unavailable("No NVIDIA devices.")
                return False

            # Check capabilities on the first device
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            caps = {
                "temperature": True,
                "power": self._try_query(lambda: pynvml.nvmlDeviceGetPowerUsage(handle)),
                "fan_speed": self._try_query(lambda: pynvml.nvmlDeviceGetFanSpeed(handle)),
                "memory_info": True,
                "encoder_util": self._try_query(lambda: pynvml.nvmlDeviceGetEncoderUtilization(handle)),
                "decoder_util": self._try_query(lambda: pynvml.nvmlDeviceGetDecoderUtilization(handle)),
                "clock_info": self._try_query(lambda: pynvml.nvmlDeviceGetClockInfo(handle, 0)),
                "throttle_reasons": self._try_query(
                    lambda: pynvml.nvmlDeviceGetCurrentClocksThrottleReasons(handle)
                ),
                "power_limit": self._try_query(lambda: pynvml.nvmlDeviceGetPowerManagementLimit(handle)),
                "pcie_throughput": self._try_query(lambda: pynvml.nvmlDeviceGetPcieThroughput(handle, 0)),
            }

            self._health.capabilities = caps
            self._health.status = ProviderStatus.AVAILABLE

            caps_str = ", ".join(k for k, v in caps.items() if v)
            logger.info(
                f"NVIDIA NVML Provider: {device_count} GPU, "
                f"capabilities: [{caps_str}]"
            )
            return True

        except ImportError:
            logger.warning("Missing pynvml module. NVIDIA Provider disabled.")
            self._health.mark_unavailable("Missing pynvml module.")
            return False
        except Exception as e:
            logger.warning(f"NVML initialization failed: {e}")
            self._health.mark_unavailable(str(e))
            return False

    @staticmethod
    def _try_query(func) -> bool:
        """Checks if NVML query is available."""
        try:
            func()
            return True
        except Exception:
            return False

    def _collect(self, context: ProviderContext) -> List[MetricData]:
        if not self._pynvml:
            return []

        pynvml = self._pynvml
        metrics: List[MetricData] = []

        try:
            count = pynvml.nvmlDeviceGetCount()
            for i in range(count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                name_raw = pynvml.nvmlDeviceGetName(handle)
                name = name_raw if isinstance(name_raw, str) else name_raw.decode("utf-8")

                tags = {
                    "host": context.host_alias,
                    "device_class": "dgpu",
                    "device_name": name,
                    "gpu_index": str(i),
                }

                fields: dict = {}

                # Temperature
                try:
                    temp = pynvml.nvmlDeviceGetTemperature(handle, 0)  # 0 = GPU
                    fields["temperature_c"] = float(temp)
                except Exception:
                    pass

                # Hotspot temperature (if available)
                try:
                    temp_hs = pynvml.nvmlDeviceGetTemperature(handle, 15)  # 15 = Hotspot
                    fields["hotspot_temperature_c"] = float(temp_hs)
                except Exception:
                    pass

                # Power
                if self._health.capabilities.get("power"):
                    try:
                        power_mw = pynvml.nvmlDeviceGetPowerUsage(handle)
                        fields["power_w"] = round(power_mw / 1000.0, 2)
                    except Exception:
                        pass

                # Power limit
                if self._health.capabilities.get("power_limit"):
                    try:
                        limit_mw = pynvml.nvmlDeviceGetPowerManagementLimit(handle)
                        fields["power_limit_w"] = round(limit_mw / 1000.0, 2)
                    except Exception:
                        pass

                # Fan speed
                if self._health.capabilities.get("fan_speed"):
                    try:
                        fan = pynvml.nvmlDeviceGetFanSpeed(handle)
                        fields["fan_speed_percent"] = float(fan)
                    except Exception:
                        pass

                    # Attempt to read individual fans
                    try:
                        for fan_idx in range(3):
                            try:
                                fan_speed = pynvml.nvmlDeviceGetFanSpeed_v2(handle, fan_idx)
                                fields[f"fan_{fan_idx}_speed_percent"] = float(fan_speed)
                            except Exception:
                                break
                    except Exception:
                        pass

                # Memory info
                try:
                    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    fields["vram_used_mb"] = round(mem.used / (1024 * 1024), 1)
                    fields["vram_total_mb"] = round(mem.total / (1024 * 1024), 1)
                    fields["vram_free_mb"] = round(mem.free / (1024 * 1024), 1)
                    if mem.total > 0:
                        fields["vram_used_percent"] = round(mem.used / mem.total * 100, 1)
                except Exception:
                    pass

                # Utilization
                try:
                    util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                    fields["gpu_util_percent"] = float(util.gpu)
                    fields["memory_util_percent"] = float(util.memory)
                except Exception:
                    pass

                # Clocks
                if self._health.capabilities.get("clock_info"):
                    try:
                        # 0 = Graphics, 1 = SM, 2 = Memory, 3 = Video
                        fields["clock_graphics_mhz"] = float(
                            pynvml.nvmlDeviceGetClockInfo(handle, 0)
                        )
                        fields["clock_memory_mhz"] = float(
                            pynvml.nvmlDeviceGetClockInfo(handle, 2)
                        )
                    except Exception:
                        pass

                # Encoder / decoder utilization
                if self._health.capabilities.get("encoder_util"):
                    try:
                        enc_util, _ = pynvml.nvmlDeviceGetEncoderUtilization(handle)
                        fields["encoder_util_percent"] = float(enc_util)
                    except Exception:
                        pass

                if self._health.capabilities.get("decoder_util"):
                    try:
                        dec_util, _ = pynvml.nvmlDeviceGetDecoderUtilization(handle)
                        fields["decoder_util_percent"] = float(dec_util)
                    except Exception:
                        pass

                # Throttle reasons
                if self._health.capabilities.get("throttle_reasons"):
                    try:
                        reasons = pynvml.nvmlDeviceGetCurrentClocksThrottleReasons(handle)
                        active_reasons = [
                            name for flag, name in _THROTTLE_REASONS.items()
                            if reasons & flag
                        ]
                        fields["throttle_active"] = 1 if active_reasons else 0
                        if active_reasons:
                            fields["throttle_reasons"] = ",".join(active_reasons)
                    except Exception:
                        pass

                # PCIe throughput
                if self._health.capabilities.get("pcie_throughput"):
                    try:
                        # 0 = TX, 1 = RX (KB/s)
                        tx = pynvml.nvmlDeviceGetPcieThroughput(handle, 0)
                        rx = pynvml.nvmlDeviceGetPcieThroughput(handle, 1)
                        fields["pcie_tx_kbps"] = float(tx)
                        fields["pcie_rx_kbps"] = float(rx)
                    except Exception:
                        pass

                if fields:
                    metrics.append(MetricData(
                        measurement_name="pc_hw_curated",
                        tags=tags,
                        fields=fields,
                    ))

        except Exception as e:
            logger.error(f"Error reading from NVML: {e}")
            raise

        return metrics

    def shutdown(self):
        if self._pynvml:
            try:
                self._pynvml.nvmlShutdown()
            except Exception:
                pass
            self._pynvml = None
