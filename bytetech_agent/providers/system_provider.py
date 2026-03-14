"""
ByteTech Agent - System Provider.
Collects generic system metrics: hostname, uptime, OS version, logged user, RAM, network.
Metrics are written to the 'pc_state' measurement.
"""
import logging
import os
import platform
import socket
import time
from typing import List

from bytetech_agent.providers.base import BaseProvider
from bytetech_agent.models.metrics import MetricData, ProviderContext, ProviderStatus

logger = logging.getLogger(__name__)


class SystemProvider(BaseProvider):
    """
    Provider for system metrics: hostname, uptime, OS, RAM, network.
    Metrics go to 'pc_state'.
    """

    def __init__(self):
        super().__init__(name="System")
        self._boot_time: float = 0.0
        self._os_version: str = ""
        self._hostname: str = ""

    def initialize(self) -> bool:
        try:
            import psutil

            self._boot_time = psutil.boot_time()
            self._hostname = socket.gethostname()
            self._os_version = f"{platform.system()} {platform.version()} ({platform.machine()})"

            self._health.capabilities = {
                "hostname": True,
                "uptime": True,
                "os_version": True,
                "logged_user": True,
                "ram_info": True,
                "network_info": True,
                "disk_info": True,
            }
            self._health.status = ProviderStatus.AVAILABLE
            logger.info(f"System Provider initialized ({self._hostname}, {self._os_version}).")
            return True

        except ImportError:
            logger.warning("Missing psutil module. System Provider disabled.")
            self._health.mark_unavailable("Missing psutil module.")
            return False
        except Exception as e:
            logger.warning(f"System Provider initialization failed: {e}")
            self._health.mark_unavailable(str(e))
            return False

    def _collect(self, context: ProviderContext) -> List[MetricData]:
        import psutil

        metrics: List[MetricData] = []

        # -- System info --
        sys_tags = {
            "host": context.host_alias,
            "info_type": "system",
        }
        sys_fields: dict = {
            "hostname": self._hostname,
            "os_version": self._os_version,
            "uptime_sec": round(time.time() - self._boot_time, 0),
        }

        # Logged user
        try:
            users = psutil.users()
            if users:
                sys_fields["logged_user"] = users[0].name
                sys_fields["logged_users_count"] = len(users)
            else:
                sys_fields["logged_user"] = os.environ.get("USERNAME", "unknown")
        except Exception:
            sys_fields["logged_user"] = os.environ.get("USERNAME", "unknown")

        metrics.append(MetricData(
            measurement_name="pc_state",
            tags=sys_tags,
            fields=sys_fields,
        ))

        # -- RAM --
        try:
            ram = psutil.virtual_memory()
            ram_tags = {
                "host": context.host_alias,
                "device_class": "ram",
                "info_type": "memory",
            }
            ram_fields = {
                "ram_total_mb": round(ram.total / (1024 * 1024), 1),
                "ram_used_mb": round(ram.used / (1024 * 1024), 1),
                "ram_available_mb": round(ram.available / (1024 * 1024), 1),
                "ram_used_percent": round(ram.percent, 1),
            }
            metrics.append(MetricData(
                measurement_name="pc_state",
                tags=ram_tags,
                fields=ram_fields,
            ))
        except Exception as e:
            logger.debug(f"RAM info error: {e}")

        # -- Storage --
        try:
            for partition in psutil.disk_partitions(all=False):
                try:
                    usage = psutil.disk_usage(partition.mountpoint)
                    disk_tags = {
                        "host": context.host_alias,
                        "device_class": "storage",
                        "device_name": partition.device,
                        "mountpoint": partition.mountpoint,
                        "fstype": partition.fstype,
                        "info_type": "disk",
                    }
                    disk_fields = {
                        "disk_total_gb": round(usage.total / (1024 ** 3), 2),
                        "disk_used_gb": round(usage.used / (1024 ** 3), 2),
                        "disk_free_gb": round(usage.free / (1024 ** 3), 2),
                        "disk_used_percent": round(usage.percent, 1),
                    }
                    metrics.append(MetricData(
                        measurement_name="pc_state",
                        tags=disk_tags,
                        fields=disk_fields,
                    ))
                except PermissionError:
                    continue
        except Exception as e:
            logger.debug(f"Disk info error: {e}")

        # -- Disk I/O --
        try:
            disk_io = psutil.disk_io_counters(perdisk=False)
            if disk_io:
                io_tags = {
                    "host": context.host_alias,
                    "device_class": "storage",
                    "info_type": "disk_io",
                }
                io_fields = {
                    "disk_read_bytes": disk_io.read_bytes,
                    "disk_write_bytes": disk_io.write_bytes,
                    "disk_read_count": disk_io.read_count,
                    "disk_write_count": disk_io.write_count,
                }
                metrics.append(MetricData(
                    measurement_name="pc_state",
                    tags=io_tags,
                    fields=io_fields,
                ))
        except Exception as e:
            logger.debug(f"Disk I/O error: {e}")

        # -- Network (basic info) --
        try:
            net_io = psutil.net_io_counters(pernic=False)
            if net_io:
                net_tags = {
                    "host": context.host_alias,
                    "info_type": "network",
                }
                net_fields = {
                    "net_bytes_sent": net_io.bytes_sent,
                    "net_bytes_recv": net_io.bytes_recv,
                    "net_packets_sent": net_io.packets_sent,
                    "net_packets_recv": net_io.packets_recv,
                    "net_errors_in": net_io.errin,
                    "net_errors_out": net_io.errout,
                }
                metrics.append(MetricData(
                    measurement_name="pc_state",
                    tags=net_tags,
                    fields=net_fields,
                ))
        except Exception as e:
            logger.debug(f"Network info error: {e}")

        # -- CPU usage via psutil (supplemental to LHM) --
        try:
            cpu_percent = psutil.cpu_percent(interval=None)
            cpu_count = psutil.cpu_count(logical=True)
            cpu_freq = psutil.cpu_freq()

            cpu_tags = {
                "host": context.host_alias,
                "device_class": "cpu",
                "info_type": "cpu_summary",
            }
            cpu_fields = {
                "cpu_total_load_percent": float(cpu_percent),
                "cpu_logical_cores": cpu_count,
            }
            if cpu_freq:
                cpu_fields["cpu_freq_current_mhz"] = round(cpu_freq.current, 0)
                if cpu_freq.max > 0:
                    cpu_fields["cpu_freq_max_mhz"] = round(cpu_freq.max, 0)

            metrics.append(MetricData(
                measurement_name="pc_state",
                tags=cpu_tags,
                fields=cpu_fields,
            ))
        except Exception as e:
            logger.debug(f"CPU info error: {e}")

        return metrics

    def shutdown(self):
        pass
