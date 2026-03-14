# ByteTech Agent v1.0

**Modular Windows Agent** for hardware and performance (FPS/frametime) monitoring.  
It sends metrics **directly to InfluxDB 2.x** – no Telegraf, no CSV hacks, no dummy data.

One agent → multiple computers → one Grafana dashboard with the `$host` filter.

---

## What It Collects

### 🔧 Hardware (LHM Provider)
| Metric | Source | Measurement |
|---|---|---|
| CPU temp (package + per-core) | LibreHardwareMonitor WMI/JSON | `pc_hw_raw` → `pc_hw_curated` |
| CPU load, clock, power | LibreHardwareMonitor WMI/JSON | `pc_hw_raw` → `pc_hw_curated` |
| GPU temp, load, clock, power, VRAM | LibreHardwareMonitor WMI/JSON | `pc_hw_raw` → `pc_hw_curated` |
| RAM used/available | LibreHardwareMonitor WMI/JSON | `pc_hw_raw` → `pc_hw_curated` |
| Storage temp, usage, throughput | LibreHardwareMonitor WMI/JSON | `pc_hw_raw` → `pc_hw_curated` |
| Motherboard temp, fans, voltage | LibreHardwareMonitor WMI/JSON | `pc_hw_raw` → `pc_hw_curated` |

**Features**: Automatic device classification (`device_class`: cpu/dgpu/igpu/ram/storage/motherboard), hardware cache, and Min/Max data points per sensor.  
**Fallback Order**: WMI (`root\LibreHardwareMonitor`) → WMI (`root\OpenHardwareMonitor`) → JSON API (`http://127.0.0.1:8085/data.json`).

### 🟢 NVIDIA (NVML Provider)
| Metric | Source |
|---|---|
| GPU + Hotspot Temperature | `pynvml` (NVML) |
| Power draw + Power limit | `pynvml` |
| Fan speed (multi-fan) | `pynvml` |
| VRAM used/free/total/percent | `pynvml` |
| GPU/Memory utilization | `pynvml` |
| Clock Graphics/Memory (MHz) | `pynvml` |
| Encoder/Decoder utilization | `pynvml` |
| Throttle reasons (9 flag types) | `pynvml` |
| PCIe TX/RX throughput | `pynvml` |

**Measurement**: `pc_hw_curated`. Capability-guarded – if the GPU doesn't support a specific metric, it won't report it.

### 🎮 FPS / Frame Timing (PresentMon Provider)
| Metric | Source |
|---|---|
| FPS (now, avg 10s, avg 30s) | PresentMon API (`PresentMonAPI2.dll` via ctypes) |
| FPS 1% low (30s), 0.1% low (30s) | Rolling FrameTimingBuffer |
| Frametime (ms) | PresentMon API |
| CPU busy (ms), GPU busy (ms) | PresentMon API |
| Display latency (ms) | PresentMon API |
| Present mode | PresentMon API |

**Measurement**: `pc_fps`. Targeting modes: `active_foreground` (auto-detects games), `explicit_process_name`, `explicit_pid`.  
**Fallback**: ETW (Event Tracing for Windows) when PresentMon API is unavailable.  
If no backend is available, the provider returns `UNAVAILABLE`.

### 🖥️ Display (Display Provider)
| Metric | Source |
|---|---|
| Resolution (X×Y) | Windows user32.dll (EnumDisplaySettingsW) |
| Refresh rate (Hz) | Windows user32.dll |
| Color depth (bits) | Windows user32.dll |
| HDR supported/enabled | Registry + DXGI detection |
| Multi-monitor enumeration | EnumDisplayDevicesW |
| Primary display flag | DISPLAY_DEVICE flags |

**Measurement**: `pc_state`.

### 📊 System (System Provider)
| Metric | Source |
|---|---|
| Hostname, OS version, uptime | `psutil`, `platform`, `socket` |
| Logged-in user | `psutil.users()` |
| RAM total/used/available/percent | `psutil.virtual_memory()` |
| Disks: total/used/free/percent per partition | `psutil.disk_partitions()` + `disk_usage()` |
| Disk I/O: bytes read/written, count | `psutil.disk_io_counters()` |
| Network: bytes sent/recv, packets, errors | `psutil.net_io_counters()` |
| CPU: total load %, freq, logical cores | `psutil.cpu_percent()` + `cpu_freq()` |

**Measurement**: `pc_state`.

### 🛡️ Health Service
| Metric | Measurement |
|---|---|
| Agent status (healthy/degraded/critical) | `pc_state` (info_type=agent_health) |
| Agent uptime, InfluxDB connected | `pc_state` |
| Per-provider: status, last error, metrics count | `pc_state` (info_type=provider_health) |
| Capability flags per provider (cap_*) | `pc_state` |

---

## InfluxDB Schema (Default bucket: `metrics`)

| Measurement | Description | Tags |
|---|---|---|
| `pc_hw_raw` | Raw LHM sensors | host, device_class, device_name, sensor_type, sensor_name, identifier |
| `pc_hw_curated` | Normalized HW metrics | host, device_class, device_name, gpu_index |
| `pc_fps` | FPS, frametime, latency | host, process_name, pid, app_mode, backend |
| `pc_state` | System, display, health | host, info_type, provider_name, display_name |

**Global tags**: `host`, `site`, `owner` – independent hardware filtering in Grafana.

---

## Architecture

```
PC_MONITOR_GRAFANA/
├── bytetech_agent/
│   ├── app.py                      # Entry point + signal handling (SIGINT/SIGTERM/SIGBREAK)
│   ├── __main__.py                 # python -m bytetech_agent
│   ├── config.py                   # Pydantic models (Influx, Metadata, Timing, Providers, Buffer, Options)
│   ├── logging_setup.py            # RotatingFileHandler
│   ├── models/
│   │   └── metrics.py              # MetricData, ProviderStatus, ProviderHealthInfo, ProviderContext
│   ├── providers/
│   │   ├── base.py                 # BaseProvider ABC: safe get_metrics(), auto health tracking
│   │   ├── lhm_provider.py         # LibreHardwareMonitor (WMI LHM -> WMI OHM -> JSON API)
│   │   ├── presentmon_provider.py  # PresentMon C API (ctypes) + ETW fallback + FrameTimingBuffer
│   │   ├── display_provider.py     # Display info (user32.dll, DXGI, registry HDR)
│   │   ├── nvapi_provider.py       # NVIDIA NVML (pynvml) – temp/power/fan/VRAM/clocks/throttle
│   │   └── system_provider.py      # System info (psutil) – hostname/uptime/RAM/disk/net/CPU
│   ├── normalizers/
│   │   └── influx_formatter.py     # pc_hw_raw → pc_hw_curated (40 sensor mappings, per-core aggregation)
│   ├── writers/
│   │   └── influx_writer.py        # InfluxDB 2.x writer + DurableSpool (JSON disk buffer) + backoff
│   └── services/
│       ├── scheduler.py            # Agent lifecycle: threads, providers, normalizer pipeline, shutdown
│       └── health.py               # Health monitoring: provider status, capability flags, agent health
├── install/
│   ├── install.ps1                 # Interactive installer (config, testing, LHM/PM check, Scheduled Task)
│   └── uninstall.ps1              # Uninstaller (task removal, process kill, cleanup)
├── tests/                          # 57 tests (config, normalizer, writer, providers, health)
├── examples/
│   └── config.example.yaml         # Config template
├── pyproject.toml                  # pip install -e . support
├── requirements.txt
└── README.md
```

---

## Requirements

| Component | Version | Required? |
|---|---|---|
| Windows | 10/11 x64 | ✅ Yes |
| Python | 3.10+ | ✅ Yes (Installer downloads automatically via winget if missing) |
| LibreHardwareMonitor | Latest | ✅ Yes (using WMI or Web Server JSON API) |
| PresentMon | 2.x | Optional (for FPS tracking) |
| InfluxDB | 2.x (server instance) | ✅ Yes |
| NVIDIA GPU | - | Optional (for NVML provider) |

---

## Installation

```powershell
# Run as Administrator:
.\install\install.ps1
```

The installer will ask interactively for:
1. **InfluxDB** – host, port, org, bucket, token
2. **Host** – alias (e.g. PC-Firell), site, owner
3. **Providers** – NVML, Display, PresentMon (can optionally be disabled)

Then automatically:
- Download Python 3.10+ using `winget` (if missing)
- Create `C:\ByteTechAgent` & virtual environment (`venv`)
- Install dependencies
- Generate `config.yaml`
- **Detect** LibreHardwareMonitor (WMI LHM -> WMI OHM -> JSON API)
- **Detect** PresentMon API / Service implementations
- **Test** the connection to the remote InfluxDB + test write to InfluxDB
- Register a **Scheduled Task**
- Print an installation summary to the terminal.

## Agent Management

```powershell
Restart-ScheduledTask -TaskName ByteTechAgent      # Restart agent
Stop-ScheduledTask -TaskName ByteTechAgent          # Stop agent
Get-ScheduledTask -TaskName ByteTechAgent           # View status
Get-Content C:\ByteTechAgent\logs\*.log -Tail 50    # View live logs
```

## Uninstallation

```powershell
.\install\uninstall.ps1
```

---

## Adding New Providers

```python
from bytetech_agent.providers.base import BaseProvider
from bytetech_agent.models.metrics import MetricData, ProviderContext, ProviderStatus

class MyProvider(BaseProvider):
    def __init__(self):
        super().__init__(name="MyProvider")

    def initialize(self) -> bool:
        self._health.capabilities = {"my_metric": True}
        self._health.status = ProviderStatus.AVAILABLE
        return True

    def _collect(self, context: ProviderContext) -> list[MetricData]:
        return [MetricData(
            measurement_name="pc_hw_curated",
            tags={"host": context.host_alias, "device_class": "custom"},
            fields={"my_value": real_value},
        )]

    def shutdown(self):
        pass
```

Registration: `scheduler.py` → add to the providers list + toggle flag in `config.py`.

---

## Testing

```powershell
python -m pytest tests/ -v
```

**57 tests** covering:
- Config: Pydantic validation, consistency, YAML parsing
- Normalizer: raw→curated mapping, per-core aggregation, grouping
- Writer: DurableSpool logic (store/load/limit), JSON offline buffer bounds
- Providers: FrameTimingBuffer calculations, exception handling
- Health: component states, emission testing

---

## Resilience & Fault Tolerance

| Scenario | Behavior |
|---|---|
| InfluxDB unavailable | Memory deque buffer (10k) + Disk spool json buffering (Max 50 files) |
| InfluxDB returns | Auto-replay from disk spool + memory buffer |
| Provider crashes | Caught by BaseProvider, reports `DEGRADED`/`FAILED` and execution continues. |
| Optional API is unavailable | Returns `UNAVAILABLE` flag. No metrics emitted. |
| LHM (WMI) is disabled | Fallback to HTTP JSON API parser (`http://127.0.0.1:8085/data.json`). |
| LHM is entirely absent | LHM switches to `UNAVAILABLE` flag, other providers continue. |
| PresentMon absent | Switches to `UNAVAILABLE` flag, pipeline continues. |
| NVML/GPU absent | Switches to `UNAVAILABLE` flag, pipeline continues. |
| System restarts | Starts automatically using Windows Scheduled Task at system startup. |

## Known Limitations

- **PresentMon**: requires the PresentMon 2.x `PresentMonAPI2.dll` library files.
- **ETW fallback**: requires active Administrator privileges.
- **HDR detection**: strictly hardware and OS bound. 
- **DLSS/FSR/XeSS**: Currently lacks an open, publicly available system API.
- **IDE lint errors (Pyre2)**: A Python linter runtime error in some IDEs due to `pynvml`'s missing definition boundaries. Has zero impact on actual telemetry metrics or agent functionality.
