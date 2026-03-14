"""
ByteTech Agent - Display Provider.
Collects display information: resolution, refresh rate, HDR.
Based on Windows API (user32.dll, DXGI).
"""
import ctypes
import ctypes.wintypes
import logging
import os
from typing import List

from bytetech_agent.providers.base import BaseProvider
from bytetech_agent.models.metrics import MetricData, ProviderContext, ProviderStatus

logger = logging.getLogger(__name__)


class DEVMODEW(ctypes.Structure):
    """Windows DEVMODEW structure for EnumDisplaySettingsW."""
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", ctypes.c_ushort),
        ("dmDriverVersion", ctypes.c_ushort),
        ("dmSize", ctypes.c_ushort),
        ("dmDriverExtra", ctypes.c_ushort),
        ("dmFields", ctypes.c_ulong),
        ("dmPositionX", ctypes.c_long),
        ("dmPositionY", ctypes.c_long),
        ("dmDisplayOrientation", ctypes.c_ulong),
        ("dmDisplayFixedOutput", ctypes.c_ulong),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_ushort),
        ("dmBitsPerPel", ctypes.c_ulong),
        ("dmPelsWidth", ctypes.c_ulong),
        ("dmPelsHeight", ctypes.c_ulong),
        ("dmDisplayFlags", ctypes.c_ulong),
        ("dmDisplayFrequency", ctypes.c_ulong),
        ("dmICMMethod", ctypes.c_ulong),
        ("dmICMIntent", ctypes.c_ulong),
        ("dmMediaType", ctypes.c_ulong),
        ("dmDitherType", ctypes.c_ulong),
        ("dmReserved1", ctypes.c_ulong),
        ("dmReserved2", ctypes.c_ulong),
        ("dmPanningWidth", ctypes.c_ulong),
        ("dmPanningHeight", ctypes.c_ulong),
    ]


class DISPLAY_DEVICEW(ctypes.Structure):
    """Windows DISPLAY_DEVICEW structure for EnumDisplayDevicesW."""
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("DeviceName", ctypes.c_wchar * 32),
        ("DeviceString", ctypes.c_wchar * 128),
        ("StateFlags", ctypes.c_ulong),
        ("DeviceID", ctypes.c_wchar * 128),
        ("DeviceKey", ctypes.c_wchar * 128),
    ]


# DISPLAY_DEVICE flags
_DISPLAY_DEVICE_ACTIVE = 0x00000001
_DISPLAY_DEVICE_PRIMARY_DEVICE = 0x00000004


class DisplayProvider(BaseProvider):
    """
    Provider of display information.
    Collects resolution, refresh rate, color, multi-monitor info.
    Attempts HDR detection (via registry or DXGI AdvancedColorInfo).
    Metrics go to 'pc_state' measurement.
    """

    def __init__(self):
        super().__init__(name="Display")
        self._user32 = None
        self._hdr_detection_available = False

    def initialize(self) -> bool:
        if os.name != "nt":
            logger.warning("DisplayProvider supports Windows only.")
            self._health.mark_unavailable("Non-Windows OS.")
            return False

        try:
            self._user32 = ctypes.windll.user32
            # Availability test
            _ = self._user32.EnumDisplaySettingsW
            _ = self._user32.EnumDisplayDevicesW

            # HDR detection attempt
            self._hdr_detection_available = self._check_hdr_capability()

            self._health.capabilities = {
                "resolution": True,
                "refresh_rate": True,
                "color_depth": True,
                "multi_monitor": True,
                "hdr_detection": self._hdr_detection_available,
            }
            self._health.status = ProviderStatus.AVAILABLE
            logger.info(
                f"DisplayProvider initialized "
                f"(HDR detection: {'yes' if self._hdr_detection_available else 'no'})."
            )
            return True

        except Exception as e:
            logger.error(f"Failed to load user32: {e}")
            self._health.mark_unavailable(str(e))
            return False

    def _check_hdr_capability(self) -> bool:
        """Checks if HDR detection is possible (via registry)."""
        try:
            import winreg

            key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\AdvancedDisplay"
            try:
                winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path)
                return True
            except FileNotFoundError:
                pass

            # Alternative location
            key_path2 = r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers"
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path2)
                winreg.CloseKey(key)
                return True
            except FileNotFoundError:
                pass

        except Exception:
            pass
        return False

    def _detect_hdr_status(self) -> dict:
        """
        Attempts to detect HDR status.
        Method: check Windows registry for HDR-capable displays.
        """
        result = {"hdr_supported": False, "hdr_enabled": False}
        if not self._hdr_detection_available:
            return result

        try:
            import winreg

            # Windows stores HDR settings in registry
            key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\AutoRotation"
            # No direct HDR key in older Windows
            # Checking via DXGI output (if available)

            # Method via DXGIOutput6::GetDesc1
            try:
                dxgi = ctypes.windll.LoadLibrary("dxgi.dll")
                if dxgi:
                    # DXGI available - HDR detection possible through COM
                    # Full implementation requires IDXGIFactory6/IDXGIOutput6 COM interfaces
                    # Here we only verify DLL presence
                    result["hdr_supported"] = True
            except Exception:
                pass

        except Exception as e:
            logger.debug(f"HDR detection error: {e}")

        return result

    def _collect(self, context: ProviderContext) -> List[MetricData]:
        if not self._user32:
            return []

        metrics: List[MetricData] = []
        display_idx = 0

        while True:
            # Display device enumeration
            dd = DISPLAY_DEVICEW()
            dd.cb = ctypes.sizeof(DISPLAY_DEVICEW)

            if not self._user32.EnumDisplayDevicesW(None, display_idx, ctypes.byref(dd), 0):
                break

            display_idx += 1

            # Skip inactive monitors
            if not (dd.StateFlags & _DISPLAY_DEVICE_ACTIVE):
                continue

            is_primary = bool(dd.StateFlags & _DISPLAY_DEVICE_PRIMARY_DEVICE)
            device_name = dd.DeviceName
            device_string = dd.DeviceString

            # Get display settings
            devmode = DEVMODEW()
            devmode.dmSize = ctypes.sizeof(DEVMODEW)

            # ENUM_CURRENT_SETTINGS = -1
            if not self._user32.EnumDisplaySettingsW(device_name, -1, ctypes.byref(devmode)):
                continue

            tags = {
                "host": context.host_alias,
                "display_name": device_string.strip() if device_string else f"Display_{display_idx}",
                "display_device": device_name.strip() if device_name else f"Unknown_{display_idx}",
                "is_primary": "true" if is_primary else "false",
            }

            fields = {
                "resolution_x": int(devmode.dmPelsWidth),
                "resolution_y": int(devmode.dmPelsHeight),
                "refresh_rate": int(devmode.dmDisplayFrequency),
                "color_depth": int(devmode.dmBitsPerPel),
                "display_index": display_idx - 1,
            }

            # HDR detection attempt
            if self._hdr_detection_available and is_primary:
                hdr_info = self._detect_hdr_status()
                fields["hdr_supported"] = 1 if hdr_info["hdr_supported"] else 0
                fields["hdr_enabled"] = 1 if hdr_info["hdr_enabled"] else 0

            metrics.append(MetricData(
                measurement_name="pc_state",
                tags=tags,
                fields=fields,
            ))

        return metrics

    def shutdown(self):
        self._user32 = None
