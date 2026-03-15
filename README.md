# ByteTech Agent v1.0

Windows monitoring agent for hardware, system state, and game FPS metrics.

Architecture:

`Windows Agent -> InfluxDB 2.x -> Grafana`

This repository now uses a production-oriented PresentMon integration based on the standalone `PresentMon.exe` console application launched as a subprocess. The agent reads frame-level records from `stdout`, computes rolling statistics in memory, and writes them directly to InfluxDB measurement `pc_fps`.

CSV files are not used as the production data path and the agent does not depend on `PresentMonSharedService`, `ctypes`, or the Intel PresentMon API V2 bindings.

---

## English

### Overview

ByteTech Agent collects:

- hardware telemetry from LibreHardwareMonitor and NVML
- system and display state from Windows and `psutil`
- frame timing and FPS metrics from PresentMon console stdout
- provider and agent health metrics

The default InfluxDB bucket is `metrics`.

### Main Measurements

| Measurement | Purpose |
|---|---|
| `pc_hw_raw` | Raw LibreHardwareMonitor sensor values |
| `pc_hw_curated` | Normalized hardware metrics |
| `pc_fps` | FPS, frametime, lows, latency, PresentMon backend tags |
| `pc_state` | System, display, provider health, agent health |

### PresentMon FPS Provider

The FPS provider is implemented in [bytetech_agent/providers/presentmon_provider.py](C:\Users\Firell\.gemini\antigravity\scratch\PC_MONITOR_GRAFANA\bytetech_agent\providers\presentmon_provider.py).

Production path:

1. Resolve the current target process.
2. Launch `PresentMon.exe` as a subprocess.
3. Stream CSV rows from `stdout`.
4. Parse frame-level records in Python.
5. Maintain rolling 1s, 10s, and 30s windows in memory per process.
6. Emit one `pc_fps` metric per collection cycle.

Supported target modes:

- `active_foreground`
- `explicit_process_name`
- `explicit_process_id`

Tags written to `pc_fps`:

- `host`
- `process_name`
- `pid`
- `app_mode`
- `backend`

Current backend tag value:

- `presentmon_console_stdout`

Fields written to `pc_fps`:

- `fps_now`
- `frametime_ms_now`
- `fps_avg_10s`
- `fps_avg_30s`
- `fps_1pct_30s`
- `fps_0_1pct_30s`
- `cpu_busy_ms` when available
- `gpu_busy_ms` when available
- `display_latency_ms` when available
- `present_mode_name` when available

### Important PresentMon Notes

- The agent no longer uses `PresentMonSharedService` or API V2 through `ctypes`.
- The agent uses `--output_stdout` and reads PresentMon output directly from the child process stream.
- In PresentMon itself, `--output_stdout` and `--no_csv` are mutually exclusive. Because of that, the agent uses `stdout` streaming and does not write CSV files to disk.
- On some Windows hosts, `PresentMon.exe` requires elevation. If so, run the agent from an elevated scheduled task, service, or administrator console.

### PresentMon Configuration

Configuration model:

```yaml
presentmon:
  target_mode: "active_foreground"
  process_name: ""
  process_id: 0
  executable_path: "C:\\Program Files\\Intel\\PresentMon\\PresentMonApplication\\PresentMon.exe"
```

Notes:

- `executable_path` is optional.
- If `executable_path` is omitted, the agent tries known locations such as `C:\Program Files\Intel\PresentMon\PresentMonApplication\PresentMon.exe`.
- `process_name` is used only in `explicit_process_name`.
- `process_id` is used in `explicit_process_id` or `explicit_pid`.

### Installation

Requirements:

- Windows 10/11 x64
- Python 3.10+
- InfluxDB 2.x
- LibreHardwareMonitor for hardware telemetry
- PresentMon for FPS telemetry
- NVIDIA GPU is optional for NVML metrics

Install dependencies:

```powershell
pip install -r requirements.txt
```

Run the agent:

```powershell
python -m bytetech_agent
```

Run the installer:

```powershell
.\install\install.ps1
```

### Diagnostics

The stdout probe tool is implemented in [bytetech_agent/tools/presentmon_stdout_probe.py](C:\Users\Firell\.gemini\antigravity\scratch\PC_MONITOR_GRAFANA\bytetech_agent\tools\presentmon_stdout_probe.py).

Examples:

```powershell
python -m bytetech_agent.tools.presentmon_stdout_probe --process-name dwm.exe --duration 5
python -m bytetech_agent.tools.presentmon_stdout_probe --process-id 1234 --duration 5
```

The probe:

- resolves `PresentMon.exe`
- prints the exact launch command
- shows the first stdout lines
- runs the same CSV parser used by the agent
- reports whether non-zero frame-level records were parsed

### Logging

The PresentMon provider logs:

- exact launch command
- subprocess PID
- process target switches
- first stdout and stderr lines after startup
- frame-level processed record counts
- computed fields before `MetricData` creation
- reasons for zero-valued output
- parser errors without crashing the agent

### Testing

Run all tests:

```powershell
pytest -q
```

Run only PresentMon-related tests:

```powershell
pytest -q tests\test_presentmon_provider.py
```

### Operational Behavior

- If no foreground target exists, the agent emits legal zero values for `pc_fps`.
- If the target process is not rendering, the agent emits zeros instead of crashing or dropping the whole write batch.
- If the foreground game changes, the provider restarts PresentMon only when necessary.
- The provider performs clean shutdown and avoids zombie child processes.

### Known Limitations

- Live validation still depends on the local host allowing `PresentMon.exe` to run.
- Some hosts require administrator privileges for PresentMon capture.
- If multiple processes share the same executable name in `explicit_process_name`, the provider uses the freshest active sample set.

---

## Polski

### Opis

ByteTech Agent zbiera:

- telemetrię sprzętową z LibreHardwareMonitor i NVML
- stan systemu i ekranów z Windows oraz `psutil`
- metryki FPS i frametime z PresentMon uruchamianego jako osobny proces
- metryki zdrowia providera i całego agenta

Domyślny bucket InfluxDB to `metrics`.

### Główne measurementy

| Measurement | Przeznaczenie |
|---|---|
| `pc_hw_raw` | Surowe sensory LibreHardwareMonitor |
| `pc_hw_curated` | Znormalizowane metryki sprzętowe |
| `pc_fps` | FPS, frametime, lows, latency i tagi backendu PresentMon |
| `pc_state` | Stan systemu, ekranów, providerów i zdrowia agenta |

### Provider FPS oparty o PresentMon

Provider FPS jest zaimplementowany w [bytetech_agent/providers/presentmon_provider.py](C:\Users\Firell\.gemini\antigravity\scratch\PC_MONITOR_GRAFANA\bytetech_agent\providers\presentmon_provider.py).

Ścieżka produkcyjna działa tak:

1. Agent wyznacza aktualny proces docelowy.
2. Uruchamia `PresentMon.exe` jako subprocess.
3. Czyta strumieniowo rekordy CSV ze `stdout`.
4. Parsuje rekordy frame-level w Pythonie.
5. Utrzymuje rolling windows 1s, 10s i 30s w pamięci, osobno dla procesu.
6. W każdej iteracji emituje pojedynczy rekord `pc_fps`.

Obsługiwane tryby targetowania:

- `active_foreground`
- `explicit_process_name`
- `explicit_process_id`

Tagi zapisywane do `pc_fps`:

- `host`
- `process_name`
- `pid`
- `app_mode`
- `backend`

Aktualna wartość taga `backend`:

- `presentmon_console_stdout`

Pola zapisywane do `pc_fps`:

- `fps_now`
- `frametime_ms_now`
- `fps_avg_10s`
- `fps_avg_30s`
- `fps_1pct_30s`
- `fps_0_1pct_30s`
- `cpu_busy_ms` jeśli dostępne
- `gpu_busy_ms` jeśli dostępne
- `display_latency_ms` jeśli dostępne
- `present_mode_name` jeśli dostępne

### Ważne uwagi o PresentMon

- Agent nie używa już `PresentMonSharedService` ani API V2 przez `ctypes`.
- Agent używa `--output_stdout` i czyta dane bezpośrednio ze strumienia procesu potomnego.
- W samym PresentMon flagi `--output_stdout` i `--no_csv` są wzajemnie wykluczające. Dlatego agent używa streamingu po `stdout` i nie zapisuje CSV na dysk.
- Na części hostów Windows `PresentMon.exe` wymaga podniesionych uprawnień. W takiej sytuacji uruchamiaj agenta jako zadanie z uprawnieniami administratora, usługę lub z podniesionej konsoli.

### Konfiguracja PresentMon

Model konfiguracji:

```yaml
presentmon:
  target_mode: "active_foreground"
  process_name: ""
  process_id: 0
  executable_path: "C:\\Program Files\\Intel\\PresentMon\\PresentMonApplication\\PresentMon.exe"
```

Uwagi:

- `executable_path` jest opcjonalne.
- Jeśli `executable_path` nie jest ustawione, agent szuka `PresentMon.exe` w znanych lokalizacjach, między innymi w `C:\Program Files\Intel\PresentMon\PresentMonApplication\PresentMon.exe`.
- `process_name` jest używane tylko w trybie `explicit_process_name`.
- `process_id` jest używane w trybie `explicit_process_id` lub `explicit_pid`.

### Instalacja

Wymagania:

- Windows 10/11 x64
- Python 3.10+
- InfluxDB 2.x
- LibreHardwareMonitor dla metryk sprzętowych
- PresentMon dla metryk FPS
- karta NVIDIA jest opcjonalna dla metryk NVML

Instalacja zależności:

```powershell
pip install -r requirements.txt
```

Uruchomienie agenta:

```powershell
python -m bytetech_agent
```

Uruchomienie instalatora:

```powershell
.\install\install.ps1
```

### Diagnostyka

Narzędzie diagnostyczne stdout probe znajduje się w [bytetech_agent/tools/presentmon_stdout_probe.py](C:\Users\Firell\.gemini\antigravity\scratch\PC_MONITOR_GRAFANA\bytetech_agent\tools\presentmon_stdout_probe.py).

Przykłady:

```powershell
python -m bytetech_agent.tools.presentmon_stdout_probe --process-name dwm.exe --duration 5
python -m bytetech_agent.tools.presentmon_stdout_probe --process-id 1234 --duration 5
```

Probe:

- lokalizuje `PresentMon.exe`
- wypisuje dokładną komendę uruchomienia
- pokazuje pierwsze linie `stdout`
- używa tego samego parsera CSV co agent
- raportuje, czy udało się sparsować niezerowe rekordy frame-level

### Logowanie

Provider PresentMon loguje:

- dokładną komendę startową
- PID subprocessa
- przełączenia targetu procesu
- pierwsze linie `stdout` i `stderr` po starcie
- liczbę przetworzonych rekordów frame-level
- wyliczone pola przed utworzeniem `MetricData`
- przyczyny wysłania zer
- błędy parsera bez wywracania całego agenta

### Testy

Uruchomienie wszystkich testów:

```powershell
pytest -q
```

Uruchomienie testów PresentMon:

```powershell
pytest -q tests\test_presentmon_provider.py
```

### Zachowanie operacyjne

- Jeśli nie ma aktywnego procesu docelowego, agent emituje poprawne zera dla `pc_fps`.
- Jeśli proces docelowy nie renderuje, agent wysyła zera zamiast crashować lub gubić cały batch do InfluxDB.
- Jeśli zmienia się aktywna gra na foregroundzie, provider restartuje PresentMon tylko wtedy, gdy to konieczne.
- Provider wykonuje clean shutdown i nie zostawia zombie processów.

### Znane ograniczenia

- Walidacja live nadal zależy od tego, czy dany host pozwala uruchomić `PresentMon.exe`.
- Na części hostów do capture z PresentMon potrzebne są uprawnienia administratora.
- Jeśli kilka procesów ma tę samą nazwę w trybie `explicit_process_name`, provider wybiera najświeższy aktywny zestaw próbek.
