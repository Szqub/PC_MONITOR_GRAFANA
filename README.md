# ByteTech Agent v1.0

Windows monitoring agent for hardware, system state, and FPS telemetry.

Architecture:

`Windows Agent -> InfluxDB 2.x -> Grafana`

The project now uses RTSS shared memory as the primary production FPS backend.
Standalone PresentMon console remains available only as an optional fallback,
diagnostic, or benchmark-oriented backend.

---

## English

### Overview

ByteTech Agent collects:

- hardware telemetry from LibreHardwareMonitor and NVML
- system and display state from Windows and `psutil`
- FPS telemetry from RTSS shared memory
- optional fallback FPS telemetry from standalone PresentMon console
- provider and agent health metrics

### FPS Backend Architecture

Primary production backend:

- `rtss_shared_memory`

Optional fallback / diagnostics:

- `presentmon_console`

Not used as production path:

- PresentMon Shared Service / API V2
- CSV files on disk
- screen scraping / OCR / overlay capture

### Why RTSS Is Now Primary

PresentMon-based live collection was demoted because on the tested host it did not provide reliable production-grade live data. RTSS shared memory is now preferred because it exposes live framerate statistics with low operational complexity.

Trade-off:

- RTSS provides good live telemetry for `fps_now` and `frametime_ms_now`
- rolling 10s / 30s averages are calculated locally in the agent
- `fps_1pct_30s` and `fps_0_1pct_30s` are sampled approximations from RTSS polling, not raw frame-event percentiles from a full trace

That limitation is intentional and documented. It is not hidden.

### Main Measurements

| Measurement | Purpose |
|---|---|
| `pc_hw_raw` | Raw LibreHardwareMonitor sensor values |
| `pc_hw_curated` | Normalized hardware metrics |
| `pc_fps` | FPS, frametime, sampled lows, backend tags |
| `pc_state` | System, display, provider health, agent health |

### `pc_fps` Schema

Required tags:

- `host`
- `process_name`
- `pid`
- `app_mode`
- `backend`

Required fields:

- `fps_now`
- `frametime_ms_now`
- `fps_avg_10s`
- `fps_avg_30s`
- `fps_1pct_30s`
- `fps_0_1pct_30s`

Optional fields:

- `present_mode_name`
- `source_quality`
- `sample_count_10s`
- `sample_count_30s`

Current backend tag values:

- `rtss_shared_memory`
- `presentmon_console_stdout`

### Configuration

```yaml
fps:
  backend: "rtss_shared_memory"
  fallback_backend: ""

rtss:
  shared_memory_name: "RTSSSharedMemoryV2"
  stale_timeout_ms: 2000

presentmon:
  target_mode: "active_foreground"
  process_name: ""
  process_id: 0
  executable_path: "C:\\ByteTechAgent\\bin\\PresentMon.exe"
```

Notes:

- default backend is `rtss_shared_memory`
- `fallback_backend` is optional and can be `presentmon_console`
- `active_foreground` remains the default production target mode
- `explicit_process_name` and `explicit_process_id` remain diagnostic modes

### RTSS Requirements

RTSS must:

- be installed or bundled on the host
- be running
- expose shared memory

If RTSS shared memory is not available:

- the agent does not crash
- the RTSS provider logs a precise technical message
- if `fallback_backend` is configured, the router may try PresentMon console
- otherwise no FPS metric is emitted for that cycle

RTSS V2 note:

- the provider accepts newer RTSS V2 layouts with large `app_entry_size` and large offsets
- parsing is guarded by bounds checking against `mapping_size`
- only a safe prefix of each app entry is required for FPS parsing

### PresentMon Fallback Rules

PresentMon console is no longer the recommended primary backend.

Use it only for:

- fallback
- diagnostics
- controlled benchmark runs

Do not use this GUI path as the default or recommended executable:

- `C:\Program Files\Intel\PresentMon\PresentMonApplication\PresentMon.exe`

The project expects a standalone console executable, preferably:

- `C:\ByteTechAgent\bin\PresentMon.exe`

If a GUI `PresentMonApplication` path is configured, the provider logs a clear error and rejects it.

### Installer Behavior

The installer now assumes:

- RTSS is the default FPS backend
- standalone PresentMon is optional
- standalone PresentMon should live at `C:\ByteTechAgent\bin\PresentMon.exe`

If PresentMon fallback is enabled, the installer:

- checks whether `C:\ByteTechAgent\bin\PresentMon.exe` exists
- asks the user for a standalone executable path or allows file browse
- can copy the selected executable into `C:\ByteTechAgent\bin\PresentMon.exe`
- stores the final path in config

### Diagnostics

PresentMon stdout probe:

```powershell
python -m bytetech_agent.tools.presentmon_stdout_probe --process-name dwm.exe --duration 5
python -m bytetech_agent.tools.presentmon_stdout_probe --process-id 1234 --duration 5
```

RTSS raw shared-memory probe:

```powershell
python -m bytetech_agent.tools.rtss_probe
python -m bytetech_agent.tools.rtss_probe --shared-memory-name RTSSSharedMemoryV2 --stale-timeout-ms 2000
```

RTSS diagnosis checklist:

1. Start RTSS.
2. Start a game or target process.
3. Verify RTSS OSD/shared memory support is available.
4. Run the agent.
5. Run `rtss_probe.py` to inspect raw mappings, header values, entry sizes, and per-entry reject reasons.
6. Check logs for backend tag and RTSS shared memory availability.
7. If `pc_fps` is still missing, inspect `rtss_provider` DEBUG logs for:
   - attempted mapping names such as `RTSSSharedMemoryV2`, `RTSSSharedMemory`, `Global\\...`, `Local\\...`
   - RTSS header details: signature, version, `dwAppEntrySize`, `dwAppArrOffset`, `dwAppArrSize`
   - parser counters: `kept`, `skipped_zero_pid`, `skipped_no_name`, `skipped_no_fps`, `skipped_stale`
   - process selection and target matching decisions

### Testing

Run all tests:

```powershell
pytest -q
```

RTSS/FPS specific tests:

```powershell
pytest -q tests\test_rtss_provider.py tests\test_presentmon_provider.py tests\test_config.py
```

### Troubleshooting

No FPS in Grafana:

1. Check `backend` tag in `pc_fps`.
2. Confirm RTSS is running.
3. Confirm RTSS shared memory is available.
4. Run `python -m bytetech_agent.tools.rtss_probe` and inspect the raw `kept` / `rejected` decisions per app entry.
5. Confirm the game is the active foreground target when using `active_foreground`.
6. If using PresentMon fallback, confirm the path is a standalone console executable and not the GUI `PresentMonApplication` path.
7. If RTSS initializes as healthy but still returns zero metrics, inspect `rtss_provider` DEBUG output before suspecting the scheduler, Influx writer, or Grafana.

Example Flux check:

```flux
from(bucket: "metrics")
  |> range(start: -10m)
  |> filter(fn: (r) => r["_measurement"] == "pc_fps")
  |> filter(fn: (r) => r["_field"] == "fps_now")
  |> filter(fn: (r) => r["backend"] == "rtss_shared_memory" or r["backend"] == "presentmon_console_stdout")
```

---

## Polski

### Opis

ByteTech Agent zbiera:

- telemetrię sprzętową z LibreHardwareMonitor i NVML
- stan systemu i ekranów z Windows oraz `psutil`
- telemetrię FPS z RTSS shared memory
- opcjonalne FPS z fallbacku standalone PresentMon console
- metryki zdrowia providera i całego agenta

### Architektura backendu FPS

Główny backend produkcyjny:

- `rtss_shared_memory`

Opcjonalny fallback / diagnostyka:

- `presentmon_console`

Nie używamy jako ścieżki produkcyjnej:

- PresentMon Shared Service / API V2
- CSV na dysku
- screen scrapingu / OCR / overlay capture

### Dlaczego RTSS jest teraz primary

Backendy oparte o PresentMon zostały zdegradowane, bo na hoście testowym nie dawały wiarygodnych danych live do produkcyjnego monitoringu. RTSS shared memory jest teraz preferowane, bo daje prostsze i stabilniejsze live telemetry.

Świadomy kompromis:

- RTSS dobrze nadaje się do `fps_now` i `frametime_ms_now`
- rolling 10s / 30s liczymy lokalnie w agencie
- `fps_1pct_30s` i `fps_0_1pct_30s` są sampled approximations z próbek RTSS, a nie surowymi percentylami z pełnego frame trace

To ograniczenie jest jawne i udokumentowane.

### Schema `pc_fps`

Wymagane tagi:

- `host`
- `process_name`
- `pid`
- `app_mode`
- `backend`

Wymagane pola:

- `fps_now`
- `frametime_ms_now`
- `fps_avg_10s`
- `fps_avg_30s`
- `fps_1pct_30s`
- `fps_0_1pct_30s`

Pola opcjonalne:

- `present_mode_name`
- `source_quality`
- `sample_count_10s`
- `sample_count_30s`

Aktualne wartości taga `backend`:

- `rtss_shared_memory`
- `presentmon_console_stdout`

### Konfiguracja

```yaml
fps:
  backend: "rtss_shared_memory"
  fallback_backend: ""

rtss:
  shared_memory_name: "RTSSSharedMemoryV2"
  stale_timeout_ms: 2000

presentmon:
  target_mode: "active_foreground"
  process_name: ""
  process_id: 0
  executable_path: "C:\\ByteTechAgent\\bin\\PresentMon.exe"
```

Uwagi:

- domyślny backend to `rtss_shared_memory`
- `fallback_backend` jest opcjonalny i może mieć wartość `presentmon_console`
- `active_foreground` pozostaje domyślnym trybem produkcyjnym
- `explicit_process_name` i `explicit_process_id` pozostają trybami diagnostycznymi

### Wymagania dla RTSS

RTSS musi:

- być zainstalowany albo dostarczony na hoście
- być uruchomiony
- udostępniać shared memory

Jeżeli RTSS shared memory nie jest dostępne:

- agent nie crashuje
- provider RTSS loguje precyzyjny techniczny komunikat
- jeśli skonfigurowano `fallback_backend`, router może spróbować PresentMon console
- w przeciwnym razie w tej iteracji nie powstaje `pc_fps`

Uwaga dla RTSS V2:

- provider akceptuje nowsze layouty RTSS V2 z dużym `app_entry_size` i dużymi offsetami
- parsowanie jest zabezpieczone bounds checkingiem względem `mapping_size`
- do odczytu FPS wymagany jest tylko bezpieczny prefix wpisu aplikacji

### Zasady dla fallbacku PresentMon

PresentMon console nie jest już zalecanym primary backendem.

Używaj go tylko do:

- fallbacku
- diagnostyki
- kontrolowanych benchmarków

Nie używaj tej ścieżki GUI jako domyślnej ani zalecanej:

- `C:\Program Files\Intel\PresentMon\PresentMonApplication\PresentMon.exe`

Projekt oczekuje standalone console executable, najlepiej:

- `C:\ByteTechAgent\bin\PresentMon.exe`

Jeśli w configu pojawi się ścieżka do GUI `PresentMonApplication`, provider zgłosi czytelny błąd i ją odrzuci.

### Zachowanie instalatora

Instalator zakłada teraz, że:

- RTSS jest domyślnym backendem FPS
- standalone PresentMon jest opcjonalny
- standalone PresentMon powinien trafić do `C:\ByteTechAgent\bin\PresentMon.exe`

Jeżeli włączysz fallback PresentMon, instalator:

- sprawdzi istnienie `C:\ByteTechAgent\bin\PresentMon.exe`
- poprosi o ścieżkę do standalone executable albo pozwoli wskazać plik przez browse dialog
- może skopiować wskazany plik do `C:\ByteTechAgent\bin\PresentMon.exe`
- zapisze finalną ścieżkę do configa

### Diagnostyka

Probe dla PresentMon stdout:

```powershell
python -m bytetech_agent.tools.presentmon_stdout_probe --process-name dwm.exe --duration 5
python -m bytetech_agent.tools.presentmon_stdout_probe --process-id 1234 --duration 5
```

Surowy probe RTSS shared memory:

```powershell
python -m bytetech_agent.tools.rtss_probe
python -m bytetech_agent.tools.rtss_probe --shared-memory-name RTSSSharedMemoryV2 --stale-timeout-ms 2000
```

Checklist dla RTSS:

1. Uruchom RTSS.
2. Uruchom grę albo proces docelowy.
3. Upewnij się, że RTSS udostępnia shared memory.
4. Uruchom agenta.
5. Uruchom `rtss_probe.py`, żeby zobaczyć surowe mappingi, header, entry size oraz powody `kept` / `rejected` dla każdego wpisu.
6. Sprawdź logi pod kątem dostępności RTSS i taga `backend`.
7. Jeżeli dalej nie ma `pc_fps`, sprawdź logi DEBUG z `rtss_provider`, w szczególności:
   - próbowane nazwy mappingu: `RTSSSharedMemoryV2`, `RTSSSharedMemory`, `Global\\...`, `Local\\...`
   - szczegóły headera RTSS: sygnatura, wersja, `dwAppEntrySize`, `dwAppArrOffset`, `dwAppArrSize`
   - liczniki parsera: `kept`, `skipped_zero_pid`, `skipped_no_name`, `skipped_no_fps`, `skipped_stale`
   - decyzje o wyborze targetu i dopasowaniu procesu

### Testy

Wszystkie testy:

```powershell
pytest -q
```

Testy RTSS/FPS:

```powershell
pytest -q tests\test_rtss_provider.py tests\test_presentmon_provider.py tests\test_config.py
```

### Diagnoza braku FPS

1. Sprawdź tag `backend` w `pc_fps`.
2. Potwierdź, że RTSS działa.
3. Potwierdź, że RTSS shared memory jest dostępne.
4. Uruchom `python -m bytetech_agent.tools.rtss_probe` i sprawdź surowe decyzje `kept` / `rejected` dla wpisów aplikacji.
5. Przy `active_foreground` upewnij się, że gra jest faktycznie aktywnym oknem.
6. Przy fallbacku PresentMon upewnij się, że ścieżka wskazuje standalone console executable, a nie GUI `PresentMonApplication`.
7. Jeżeli provider RTSS zgłasza się jako healthy, ale nadal zwraca 0 rekordów, szukaj problemu najpierw w DEBUG logach `rtss_provider`, a nie w schedulerze czy writerze InfluxDB.

Przykładowe zapytanie Flux:

```flux
from(bucket: "metrics")
  |> range(start: -10m)
  |> filter(fn: (r) => r["_measurement"] == "pc_fps")
  |> filter(fn: (r) => r["_field"] == "fps_now")
  |> filter(fn: (r) => r["backend"] == "rtss_shared_memory" or r["backend"] == "presentmon_console_stdout")
```
