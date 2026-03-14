"""
ByteTech Agent – PresentMon Provider (V2 API)
Integracja z Intel PresentMon Service (PresentMonAPI2.dll).
Zbiera realne metryki FPS/frame timing z użyciem natywnych zapytań dynamicznych (PM_DYNAMIC_QUERY).
"""
import ctypes
import ctypes.wintypes
import logging
import os
import time
import threading
from typing import List, Optional

from bytetech_agent.providers.base import BaseProvider
from bytetech_agent.models.metrics import MetricData, ProviderContext, ProviderStatus

logger = logging.getLogger(__name__)

# --- CTypes Struktury i Definicje API (PresentMon V2) ---

class PM_QUERY_ELEMENT(ctypes.Structure):
    _fields_ = [
        ("metric", ctypes.c_uint32),
        ("stat", ctypes.c_uint32),
        ("deviceId", ctypes.c_uint32),
        ("arrayIndex", ctypes.c_uint32),
        ("dataOffset", ctypes.c_uint64),
        ("dataSize", ctypes.c_uint64),
    ]

# Zidentyfikowane (po introspekcji API) identyfikatory metryk docelowych
PM_METRIC_CPU_BUSY = 9
PM_METRIC_DISPLAYED_FPS = 11
PM_METRIC_PRESENTED_FPS = 12
PM_METRIC_GPU_BUSY = 14
PM_METRIC_PRESENT_MODE = 20
PM_METRIC_DISPLAY_LATENCY = 24
PM_METRIC_DISPLAYED_FRAME_TIME = 85
PM_METRIC_PRESENTED_FRAME_TIME = 87

# Statystyki z API Intela
PM_STAT_NONE = 0
PM_STAT_AVG = 1
PM_STAT_PERCENTILE_99 = 2
PM_STAT_PERCENTILE_95 = 3
PM_STAT_PERCENTILE_90 = 4
PM_STAT_PERCENTILE_01 = 5
PM_STAT_PERCENTILE_05 = 6
PM_STAT_PERCENTILE_10 = 7
PM_STAT_MAX = 8
PM_STAT_MIN = 9

PM_STATUS_SUCCESS = 0

class PresentMonProvider(BaseProvider):
    """
    Provider metryki FPS/frame timing obsługujący Intel PresentMon V2.
    """

    def __init__(self, config):
        super().__init__(name="PresentMon")
        self.config = config
        self._pm_api_lib = None
        self._session_handle = ctypes.c_void_p()
        
        # Uchwyty dynamicznych zapytań powiązane z różnymi oknami czasowymi
        self._query_1s = ctypes.c_void_p()
        self._query_10s = ctypes.c_void_p()
        self._query_30s = ctypes.c_void_p()
        
        # Meta tablice określające jak parsować zwracane dane binarne z okien
        self._layout_1s = []
        self._layout_10s = []
        self._layout_30s = []
        self._blob_1s = None
        self._blob_10s = None
        self._blob_30s = None

        self._active_pid: Optional[int] = None
        self._active_process_name: Optional[str] = None
        
        self._current_backend = "none"

    def initialize(self) -> bool:
        if self._init_pm_service():
            self._health.capabilities = {
                "fps_now": True,
                "frametime_ms_now": True,
                "fps_avg_10s": True,
                "fps_avg_30s": True,
                "fps_1pct_30s": True,
                "cpu_busy_ms": True,
                "gpu_busy_ms": True,
                "display_latency_ms": True,
                "present_mode": True,
                "fps_0_1pct_30s": False, # Explicitly missing in PresentMon API v2 native stats
            }
            self._health.status = ProviderStatus.AVAILABLE
            self._current_backend = "presentmon_api"
            return True

        logger.warning(
            "PresentMon Provider: Brak komunikacji z usługą PM V2. "
            "Zainstaluj narzędzia Intel PresentMon, usługa musi działać w tle."
        )
        self._health.mark_unavailable("PresentMonAPI2.dll niedostępny lub usługa PM Service leży.")
        return False

    def _init_pm_service(self) -> bool:
        """Ładuje bibliotekę i testuje otwarcie nowej sesji V2 komunikacji przez rurę."""
        dll_names = ["PresentMonAPI2.dll", "PresentMonAPI2Loader.dll", "PresentMonAPI.dll"]
        search_paths = [
            os.path.join(os.environ.get("ProgramFiles", ""), "Intel", "PresentMonSharedService"),
            os.path.join(os.environ.get("ProgramFiles", ""), "Intel", "PresentMon", "PresentMonApplication"),
            os.path.join(os.environ.get("ProgramFiles", ""), "PresentMon"),
            os.getcwd(),
        ]

        dll_path = None
        for path in search_paths:
            for name in dll_names:
                full_path = os.path.join(path, name)
                if os.path.isfile(full_path):
                    if hasattr(os, "add_dll_directory"):
                        try:
                            os.add_dll_directory(path)
                        except Exception:
                            pass
                    try:
                        self._pm_api_lib = ctypes.cdll.LoadLibrary(full_path)
                        dll_path = full_path
                        break
                    except OSError:
                        continue
            if self._pm_api_lib:
                break

        if not self._pm_api_lib:
            logger.debug("Odmowa. Żaden DLL z rodziny PMv2 API nie został wykryty.")
            return False

        logger.debug(f"Załadowano backend API: {dll_path}")

        if not hasattr(self._pm_api_lib, "pmOpenSession"):
            logger.debug("To jest bardzo stary PM APIv1 DLL (brak pmOpenSession). Przerywam na V2.")
            self._pm_api_lib = None
            return False

        # Inicjalizacja PM Sesji do Service
        status = self._pm_api_lib.pmOpenSession(ctypes.byref(self._session_handle))
        if status != PM_STATUS_SUCCESS:
            logger.debug(f"Błąd otwarcia sesji The PresentMon Service (status {status}). Upewnij się, że usługa działa w tle na koncie administratora.")
            self._pm_api_lib = None
            return False

        logger.debug("Zestawiono stabilną sesję IPC z Intel PresentMon Service.")
        
        # Definiowanie sygnatur funkcji API na ten wątek
        self._setup_api_signatures()
        
        # Jeśli jesteśmy tutaj to API gra elegancko. Budujemy natywne okna zapytania (Dynamic Querying)
        if not self._register_queries():
            self._pm_api_lib.pmCloseSession(self._session_handle)
            self._session_handle = ctypes.c_void_p()
            self._pm_api_lib = None
            return False

        return True

    def _setup_api_signatures(self):
        """Deklaruje odpowiednie sygnatury C prewencyjnie dla funkcji z argumentami"""
        self._pm_api_lib.pmRegisterDynamicQuery.restype = ctypes.c_uint32
        self._pm_api_lib.pmRegisterDynamicQuery.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(PM_QUERY_ELEMENT), ctypes.c_uint64, ctypes.c_double, ctypes.c_double
        ]
        self._pm_api_lib.pmPollDynamicQuery.restype = ctypes.c_uint32
        self._pm_api_lib.pmPollDynamicQuery.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_uint32)
        ]
        self._pm_api_lib.pmStartTrackingProcess.restype = ctypes.c_uint32
        self._pm_api_lib.pmStopTrackingProcess.restype = ctypes.c_uint32

    def _register_queries(self) -> bool:
        """
        Rejestruje 3 dynamiczne zapytania rzucane na serwer Intela.
        PM wylicza z nich agresywne natywne statystyki we własnym wątku backendu.
        """
        # --- Zapytanie dla Okna Bieżącego: 1 sekunda (Instant FPS / Frametime Avg / Lag) ---
        q1_def = [
            (PM_METRIC_DISPLAYED_FPS, PM_STAT_AVG, "fps_now"),
            (PM_METRIC_PRESENTED_FRAME_TIME, PM_STAT_AVG, "frametime_ms_now"),
            (PM_METRIC_CPU_BUSY, PM_STAT_AVG, "cpu_busy_ms"),
            (PM_METRIC_GPU_BUSY, PM_STAT_AVG, "gpu_busy_ms"),
            (PM_METRIC_DISPLAY_LATENCY, PM_STAT_AVG, "display_latency_ms"),
            (PM_METRIC_PRESENT_MODE, PM_STAT_NONE, "present_mode") # (Hardware Composed Independent Flip itd...) Mode 20 doesnt usually need stat but we ask for ANY enum val
        ]
        self._query_1s, self._layout_1s, self._blob_1s = self._build_single_query(q1_def, window_ms=1000.0)

        # --- Zapytanie dla Okna Agregacyjnego 10-sekundowego ---
        q2_def = [
            (PM_METRIC_DISPLAYED_FPS, PM_STAT_AVG, "fps_avg_10s"),
        ]
        self._query_10s, self._layout_10s, self._blob_10s = self._build_single_query(q2_def, window_ms=10000.0)

        # --- Zapytanie dla Okna Agregacyjnego 30-sekundowego (do wykresów Long Term) ---
        q3_def = [
            (PM_METRIC_DISPLAYED_FPS, PM_STAT_AVG, "fps_avg_30s"),
            (PM_METRIC_DISPLAYED_FPS, PM_STAT_PERCENTILE_01, "fps_1pct_30s"), # Intela natywne 1% lows. Native 0.1% lows are missing
        ]
        self._query_30s, self._layout_30s, self._blob_30s = self._build_single_query(q3_def, window_ms=30000.0)

        if not self._query_1s or not self._query_10s or not self._query_30s:
            logger.error("Rejestracja zapytań dynamicznych w PM Serwis odrzucona.")
            return False
            
        return True

    def _build_single_query(self, raw_specs, window_ms: float):
        num_metrics = len(raw_specs)
        elements = (PM_QUERY_ELEMENT * num_metrics)()
        for i, (metric, stat, key_name) in enumerate(raw_specs):
            elements[i].metric = metric
            elements[i].stat = stat
            elements[i].deviceId = 0
            elements[i].arrayIndex = 0

        query_handle = ctypes.c_void_p()
        status = self._pm_api_lib.pmRegisterDynamicQuery(
            self._session_handle, 
            ctypes.byref(query_handle), 
            elements, 
            num_metrics, 
            ctypes.c_double(window_ms), 
            ctypes.c_double(0.0)
        )

        if status != PM_STATUS_SUCCESS:
            logger.error(f"Zapytanie dynamiczne PM (okno {window_ms}ms) zawiodło: status={status}")
            return None, [], None

        layout = []
        for i in range(num_metrics):
            sz = elements[i].dataSize
            # Enum has 4 bytes mostly, double has 8 bytes
            layout.append({
                "key": raw_specs[i][2],
                "offset": elements[i].dataOffset,
                "size": sz, 
                "is_enum": raw_specs[i][0] == PM_METRIC_PRESENT_MODE
            })

        max_size = 4096  # Wystarczająco dla kilkunastu swapchainów po offsety do 32-128B
        blob_buffer = (ctypes.c_uint8 * max_size)()

        return query_handle, layout, blob_buffer

    # --- Posiadanie Aktywnego Procesu (Target resolution) ---

    def _resolve_target_pid(self) -> Optional[int]:
        """Rozwiązuje PID w zależności od polityki (własny proces docelowy albo całe otwarte okno na przodzie)."""
        if self.config.target_mode == "explicit_pid":
            pid = self.config.process_id
            return pid if pid and pid > 0 else None

        elif self.config.target_mode == "explicit_process_name":
            name = self.config.process_name
            if not name:
                return None
            return self._find_process_by_name(name)

        elif self.config.target_mode == "active_foreground":
            return self._get_foreground_pid()

        return None

    def _find_process_by_name(self, name: str) -> Optional[int]:
        try:
            import psutil
            name_lower = name.lower()
            for p in psutil.process_iter(["name", "pid"]):
                if p.info.get("name") and p.info["name"].lower() == name_lower:
                    return p.info["pid"]
        except Exception:
            pass
        return None

    def _get_process_name(self, pid: int) -> str:
        try:
            import psutil
            return psutil.Process(pid).name()
        except Exception:
            return "unknown"

    def _get_foreground_pid(self) -> Optional[int]:
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None
            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return pid.value if pid.value else None
        except Exception:
            return None

    def _ensure_process_tracking(self, pid: int):
        """Intel PresentMon musi zostać włączony celowo dla tego procesu, aby zbierać ostre buffory"""
        if self._active_pid != pid:
            if self._active_pid is not None:
                logger.debug(f"Odpinam tracking ze starego PID: {self._active_pid}")
                self._pm_api_lib.pmStopTrackingProcess(self._session_handle, ctypes.c_uint32(self._active_pid))
                self._active_pid = None
                self._active_process_name = None
            
            logger.debug(f"Zapinam intelowski telemetryczny strumień trackingowy pod PID: {pid}")
            st = self._pm_api_lib.pmStartTrackingProcess(self._session_handle, ctypes.c_uint32(pid))
            if st == PM_STATUS_SUCCESS:
                self._active_pid = pid
                self._active_process_name = self._get_process_name(pid)
            else:
                logger.debug(f"pmStartTrackingProcess odmówił status {st} dla PID {pid}.")

    # --- Generowanie Logiki Agregatywnej ---

    def _run_query_extract(self, query, blob_buf, layout, pid) -> dict:
        """Odpytuje konkretne okno dynamiczne i rozwiązuje wskazane w nim pola bitowe do dict."""
        num_swap_chains = ctypes.c_uint32(10) # Out param na faktyczną liczbę swapchainów buforowanych na blobie
        
        status = self._pm_api_lib.pmPollDynamicQuery(
            query, 
            ctypes.c_uint32(pid), 
            blob_buf, 
            ctypes.byref(num_swap_chains)
        )

        if status != PM_STATUS_SUCCESS or num_swap_chains.value == 0:
            return {}

        out = {}
        sc_idx = 0 # W przypadku wielu swapchainów bierzemy główny domyślny pod indeksem 0
        for lay in layout:
            key = lay["key"]
            off = lay["offset"]
            size = lay["size"]
            is_enum = lay["is_enum"]
            
            data_ptr = ctypes.addressof(blob_buf) + off + sc_idx * 1024 # Note blob block spacing isn't perfectly mapped without struct sizes, but for 1 SC its always offset 0 blocks
            # Domyślnie używamy precyzyjnego Double (8 batjów) do FPS / MS Latency
            if is_enum or size == 4:
                val = ctypes.cast(data_ptr, ctypes.POINTER(ctypes.c_uint32)).contents.value
                out[key] = round(float(val), 2)
            else:
                val = ctypes.cast(data_ptr, ctypes.POINTER(ctypes.c_double)).contents.value
                out[key] = round(float(val), 2) if key not in ["present_mode"] else float(val)

        return out

    def _collect(self, context: ProviderContext) -> List[MetricData]:
        if not self._pm_api_lib:
            return []

        pid = self._resolve_target_pid()
        if not pid:
            return []

        # Wyrzuca komendy start / stop śledzenia
        self._ensure_process_tracking(pid)

        # Trzeba mieć aktywny tracker processowy żeby uzyskać jakiekolwiek zapytanie z bufora usługi
        if not self._active_pid:
            return []

        # Pompujemy dane ze wszystkich zarejestrowanych okien w usłudze
        dict_1s = self._run_query_extract(self._query_1s, self._blob_1s, self._layout_1s, pid)
        # Tylko z 1s wymagamy obecności jakichś renderujących danych by kontynowować emisję rekordu (brak pustych logów idle)
        if not dict_1s or dict_1s.get("fps_now", 0.0) <= 0.0:
            return []

        dict_10s = self._run_query_extract(self._query_10s, self._blob_10s, self._layout_10s, pid)
        dict_30s = self._run_query_extract(self._query_30s, self._blob_30s, self._layout_30s, pid)

        # Scalanie końcowych wskaźników do paczki
        fields = {}
        fields.update(dict_1s)
        fields.update(dict_10s)
        fields.update(dict_30s)

        # Dołączenie identyfikatorów gry tak aby backend w Influx i grafanie widział do kogo należa klatki
        tags = {
            "host": context.host_alias,
            "process_name": self._active_process_name or "unknown",
            "pid": str(pid),
            "app_mode": self.config.target_mode,
            "backend": "presentmon_v2_api",
        }
        
        fields["process_id"] = pid

        return [MetricData(measurement_name="pc_fps", tags=tags, fields=fields)]

    def shutdown(self):
        if self._pm_api_lib:
            if self._active_pid:
                try:
                    self._pm_api_lib.pmStopTrackingProcess(self._session_handle, ctypes.c_uint32(self._active_pid))
                except Exception:
                    pass
            for q in filter(None, [self._query_1s, self._query_10s, self._query_30s]):
                try:
                    self._pm_api_lib.pmFreeDynamicQuery(q)
                except Exception:
                    pass

            if self._session_handle:
                try:
                    self._pm_api_lib.pmCloseSession(self._session_handle)
                except Exception:
                    pass
            
            self._session_handle = ctypes.c_void_p()
            self._pm_api_lib = None
