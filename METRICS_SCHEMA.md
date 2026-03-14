# ByteTech Agent – Metrics Schema

This document defines the exact measurements, tags, and fields emitted by the ByteTech Agent runtime. 

The agent strictly separates **live runtime performance metrics** from **agent capabilities / health flags**. Performance metrics are split by hardware component (`pc_cpu`, `pc_gpu`, etc.) to make querying in Grafana predictable and clean.

---

## 1. Live Runtime Performance Metrics

These measurements contain actual telemetry values collected by the underlying providers (LHM, PresentMon, System API, NVAPI). 

### `pc_cpu`
**Source:** LibreHardwareMonitor  
**Cadence:** `hw_interval` (default 2s)  
**Tags:** `host`, `device_class="cpu"`, `device_name`  
**Fields:**
- `cpu_core_clock_avg_mhz` (float)
- `cpu_core_clock_max_mhz` (float)
- `cpu_core_load_avg_percent` (float)
- `cpu_core_load_max_percent` (float)
- `cpu_core_temp_avg_c` (float)
- `cpu_core_temp_max_c` (float)
- `cpu_core_voltage_v` (float)
- `cpu_package_power_w` (float)
- `cpu_total_load_percent` (float)
- `cpu_core_count_temp` (float) - Number of cores detected with thermal sensors

### `pc_gpu`
**Source:** LibreHardwareMonitor (or Native NVAPI)  
**Cadence:** `hw_interval` (default 2s)  
**Tags:** `host`, `device_class` (`dgpu` or `igpu`), `device_name`  
**Fields:**
- `gpu_core_clock_mhz` (float)
- `gpu_fan_percent` (float)
- `gpu_fan_rpm` (float)
- `gpu_load_percent` (float)
- `gpu_memory_clock_mhz` (float)
- `gpu_memory_load_percent` (float)
- `gpu_power_w` (float)
- `gpu_temp_c` (float)
- `gpu_vram_free_mb` (float)
- `gpu_vram_total_mb` (float)
- `gpu_vram_used_mb` (float)
*(Note: NVAPI adds deeper fields like `decoder_util_percent`, `pcie_tx_kbps`, etc. to this measurement where applicable)*

### `pc_memory`
**Source:** LibreHardwareMonitor  
**Cadence:** `hw_interval` (default 2s)  
**Tags:** `host`, `device_class="ram"`, `device_name`  
**Fields:**
- `ram_available_gb` (float)
- `ram_used_gb` (float)
- `ram_used_percent` (float)

### `pc_storage`
**Source:** LibreHardwareMonitor  
**Cadence:** `hw_interval` (default 2s)  
**Tags:** `host`, `device_class="storage"`, `device_name`  
**Fields:**
- `storage_data_read_gb` (float)
- `storage_data_written_gb` (float)
- `storage_read_rate_bps` (float)
- `storage_temp_c` (float) - *Live composite temperature only. Static hardware/firmware threshold limits (like Critical, Limit, or Trip sensors) are intentionally excluded.*
- `storage_used_percent` (float)
- `storage_write_rate_bps` (float)

### `pc_motherboard`
**Source:** LibreHardwareMonitor  
**Cadence:** `hw_interval` (default 2s)  
**Tags:** `host`, `device_class="motherboard"`, `device_name`  
**Fields:**
- `mb_temp_c` (float)
- `mb_vcore_v` (float)
- *(Dynamically detected fan RPMs)*

### `pc_fps`
**Source:** PresentMon (API or ETW Fallback)  
**Cadence:** `fps_interval` (default 1s)  
**Note:** Only emitted when an active game/process is detected dynamically.  
**Tags:** `host`, `process_name`, `pid`, `app_mode`, `backend`  
**Fields:**
- `fps_now` (float)
- `frametime_ms_now` (float)
- `fps_avg_10s` (float)
- `fps_avg_30s` (float)
- `fps_1pct_30s` (float)
- `fps_0_1pct_30s` (float)
*(If using the native PresentMon API extension instead of ETW fallback, the following fields are also emitted: `cpu_busy_ms`, `gpu_busy_ms`, `display_latency_ms`)*

---

## 2. System State & Capabilities

Capabilities and total system aggregates are collected in a separate bucket to prevent pollution of the high-frequency metrics.

### `pc_state`
**Source:** System API & Health Service  
**Cadence:** `hw_interval` (default 2s)  
**Note:** `pc_state` uses the `info_type` tag to separate its records cleanly. Do not average `pc_state` values without filtering by `info_type` first.

**When `info_type="system"`:**
- `hostname` (string)
- `os_version` (string)
- `uptime_sec` (float)
- `logged_user` (string)
- `logged_users_count` (int)

**When `info_type="cpu_summary"`:**
- `cpu_total_load_percent` (float)
- `cpu_freq_current_mhz` (float)
- `cpu_logical_cores` (int)

**When `info_type="network"`:**
- `net_bytes_recv` (int)
- `net_bytes_sent` (int)
- `net_errors_in` (int)

**When `info_type="disk"`:**
**(Tags include `mountpoint`, `fstype`)**
- `disk_used_percent` (float)
- `disk_free_gb` (float)

**When `info_type="provider_health"`:**
This represents the internal capabilities of the agent on the host machine.
- `status` (string: AVAILABLE, DEGRADED)
- `cap_fps_now` (bool)
- `cap_cpu_busy_ms` (bool)
- `cap_gpu_temp` (bool)

---

## 3. Example Flux Queries

**Query overall machine CPU Load:**
```flux
from(bucket: "metrics")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "pc_cpu")
  |> filter(fn: (r) => r["_field"] == "cpu_total_load_percent")
  |> filter(fn: (r) => r["host"] == "${host}")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> yield(name: "mean")
```

**Query Active Game FPS:**
```flux
from(bucket: "metrics")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "pc_fps")
  |> filter(fn: (r) => r["_field"] == "fps_now" or r["_field"] == "fps_1pct_30s")
  |> filter(fn: (r) => r["host"] == "${host}")
```

**Query PresentMon Backend Capability (Is ETW Fallback in use?):**
```flux
from(bucket: "metrics")
  |> range(start: -5m) // Only need the most recent status
  |> filter(fn: (r) => r["_measurement"] == "pc_state")
  |> filter(fn: (r) => r["info_type"] == "provider_health")
  |> filter(fn: (r) => r["provider"] == "PresentMon")
  |> filter(fn: (r) => r["_field"] == "cap_cpu_busy_ms") // If false, ETW fallback is active
  |> last()
```
