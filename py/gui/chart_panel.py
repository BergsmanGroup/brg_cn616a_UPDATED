"""
chart_panel.py

Live telemetry chart panel with 1-hour rolling window.
- Plots PV and setpoint per zone over time
- Auto-scales Y-axis; fixed 1-hour X-axis (max)
- Handles rotated log files (YYYY-MM-DD_NNN pattern)
- Auto-refreshes as new telemetry arrives
- Clear button to reset display
"""

import tkinter as tk
from tkinter import ttk
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any, Callable
from datetime import datetime, timedelta
import json
import traceback
from zoneinfo import ZoneInfo
import logging

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import ScalarFormatter
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure


LOGGER = logging.getLogger("cn616a.gui")


class ZoneNavigationToolbar(NavigationToolbar2Tk):
    """Navigation toolbar that notifies panel when Home is pressed."""

    def __init__(self, canvas, window, on_home_callback=None):
        self._on_home_callback = on_home_callback
        super().__init__(canvas, window)

    def home(self, *args):
        super().home(*args)
        if callable(self._on_home_callback):
            self._on_home_callback()


_TELEMETRY_CACHE: Dict[Tuple[str, float], Dict[str, Any]] = {}
_ANALYSIS_CACHE: Dict[Tuple[str, float], Dict[str, Any]] = {}


def _normalize_zone_names(raw: Any) -> Dict[int, str]:
    """Normalize zone_names from config into {zone_id: display_name}."""
    defaults = {z: f"Zone {z}" for z in range(1, 7)}

    if isinstance(raw, dict):
        for z in range(1, 7):
            text = raw.get(str(z), raw.get(z, defaults[z]))
            text = str(text).strip() if text is not None else ""
            defaults[z] = text or f"Zone {z}"
        return defaults

    if isinstance(raw, (list, tuple)):
        for idx, text in enumerate(raw[:6], start=1):
            name = str(text).strip() if text is not None else ""
            defaults[idx] = name or f"Zone {idx}"
        return defaults

    return defaults


def _load_zone_names(logs_dir: Path) -> Dict[int, str]:
    from .state_reader import get_service_config_state
    svc = get_service_config_state(logs_dir)
    cfg = svc.get("config", {}) if isinstance(svc, dict) else {}
    return _normalize_zone_names(cfg.get("zone_names", {}))


def _iter_lines_reverse(file_path: Path, chunk_size: int = 65536):
    """Yield non-empty file lines in reverse order without loading full file into memory."""
    with open(file_path, "rb") as f:
        f.seek(0, 2)
        position = f.tell()
        buffer = b""

        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            f.seek(position)
            chunk = f.read(read_size)
            buffer = chunk + buffer
            parts = buffer.split(b"\n")
            buffer = parts[0]
            for part in reversed(parts[1:]):
                line = part.decode("utf-8", errors="replace").strip()
                if line:
                    yield line

        final_line = buffer.decode("utf-8", errors="replace").strip()
        if final_line:
            yield final_line


def _extract_zone_values(obj: Dict[str, Any], ts: datetime, zones_data: Dict[int, Dict[str, List[Any]]]) -> int:
    """Extract zone PV/SP values for one telemetry object. Returns points added count."""
    points_added = 0
    zones = obj.get("data", {}).get("zones", {})
    if not zones:
        zones = obj.get("telemetry", {}).get("zones", {})

    for zone_id_str in ["1", "2", "3", "4", "5", "6"]:
        zone_id = int(zone_id_str)
        zone_data = zones.get(zone_id_str, {})

        pv = zone_data.get("pv_c")
        sp = zone_data.get("sp_abs_c") or zone_data.get("sp_abs")
        sp_autotune = zone_data.get("autotune_sp_c") or zone_data.get("autotune_sp")

        if pv is not None or sp is not None or sp_autotune is not None:
            zones_data[zone_id]["times"].append(ts)
            zones_data[zone_id]["pv"].append(pv)
            zones_data[zone_id]["sp"].append(sp)
            zones_data[zone_id]["sp_autotune"].append(sp_autotune)
            points_added += 1

    return points_added


def _get_latest_timestamp(log_files: List[Path], debug: bool = False) -> Optional[datetime]:
    """Find latest telemetry timestamp by reverse-scanning newest files first."""
    for log_file in reversed(log_files):
        try:
            for line in _iter_lines_reverse(log_file):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts_str = obj.get("timestamp_pacific") or obj.get("ts")
                ts = parse_iso_timestamp(ts_str) if ts_str else None
                if ts is not None:
                    return ts
        except Exception:
            if debug:
                print(f"[_get_latest_timestamp] failed reading {log_file.name}: {traceback.format_exc()}")
            LOGGER.exception("Failed reading latest timestamp from %s", log_file)
            continue

    return None


def _clone_zones_data(zones_data: Dict[int, Dict[str, List[Any]]]) -> Dict[int, Dict[str, List[Any]]]:
    """Return a deep-ish clone of zones_data where all value lists are copied."""
    return {
        zone_id: {
            "times": list(values["times"]),
            "pv": list(values["pv"]),
            "sp": list(values["sp"]),
            "sp_autotune": list(values["sp_autotune"]),
        }
        for zone_id, values in zones_data.items()
    }


def _build_log_signature(log_files: List[Path]) -> Tuple[Tuple[str, int, int], ...]:
    """Build a lightweight signature from file name, mtime_ns, and size."""
    sig: List[Tuple[str, int, int]] = []
    for log_file in log_files:
        try:
            st = log_file.stat()
            sig.append((log_file.name, st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((log_file.name, -1, -1))
    return tuple(sig)


def get_display_timezone():
    """Return preferred display timezone; fall back to local timezone if tz database is unavailable."""
    try:
        return ZoneInfo("America/Los_Angeles")
    except Exception:
        return datetime.now().astimezone().tzinfo


def parse_iso_timestamp(ts_str: str) -> Optional[datetime]:
    """Parse ISO 8601 timestamp string to datetime."""
    if not ts_str:
        return None
    try:
        # Handle both with and without timezone
        return datetime.fromisoformat(ts_str)
    except Exception:
        return None


def find_jsonl_log_files(logs_dir: Path, main_name: str, rotated_pattern: str) -> List[Path]:
    """Find JSONL log files with optional rotation, sorted by mtime."""
    files: List[Path] = []

    main_log = logs_dir / main_name
    if main_log.exists():
        files.append(main_log)

    files.extend(logs_dir.glob(rotated_pattern))

    unique_by_name: Dict[str, Path] = {f.name: f for f in files}
    unique_files = list(unique_by_name.values())

    def sort_key(file_path: Path):
        try:
            st = file_path.stat()
            return (st.st_mtime_ns, file_path.name)
        except OSError:
            return (0, file_path.name)

    unique_files.sort(key=sort_key)
    return unique_files


def find_log_files(logs_dir: Path) -> List[Path]:
    """Find telemetry log files, ordered by mtime."""
    return find_jsonl_log_files(
        logs_dir,
        main_name="cn616a_telemetry_log.jsonl",
        rotated_pattern="cn616a_telemetry_log_*.jsonl",
    )


def find_analysis_log_files(logs_dir: Path) -> List[Path]:
    """Find analysis log files, ordered by mtime."""
    return find_jsonl_log_files(
        logs_dir,
        main_name="cn616a_analysis_log.jsonl",
        rotated_pattern="cn616a_analysis_log_*.jsonl",
    )


def _scan_windowed_log_points(
    log_files: List[Path],
    time_window_hours: float,
    out_data: Dict[int, Dict[str, List[Any]]],
    extract_fn: Callable[[Dict[str, Any], datetime, Dict[int, Dict[str, List[Any]]]], int],
    *,
    debug: bool = False,
    label: str = "history",
    error_log_message: Optional[str] = None,
    print_error_prefix: Optional[str] = None,
) -> Tuple[int, int]:
    """Shared reverse-scan loader used for telemetry and analysis history."""
    latest_ts = _get_latest_timestamp(log_files, debug=debug)
    if latest_ts is None:
        return (0, 0)

    window_start = latest_ts - timedelta(hours=time_window_hours)
    if debug:
        print(f"[{label}] latest_ts={latest_ts}, window_start={window_start}")

    total_lines_read = 0
    total_points = 0
    stop_all = False
    for log_file in reversed(log_files):
        if stop_all:
            break
        try:
            file_had_in_window_data = False
            for line in _iter_lines_reverse(log_file):
                total_lines_read += 1
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts_str = obj.get("timestamp_pacific") or obj.get("ts")
                ts = parse_iso_timestamp(ts_str) if ts_str else None
                if ts is None:
                    continue

                if ts < window_start:
                    if not file_had_in_window_data:
                        stop_all = True
                    break

                file_had_in_window_data = True
                total_points += extract_fn(obj, ts, out_data)
        except Exception:
            if print_error_prefix:
                try:
                    print(f"[{print_error_prefix}] Error reading {log_file}")
                except Exception:
                    pass
            if error_log_message:
                LOGGER.exception(error_log_message, log_file)
            else:
                LOGGER.exception("%s failed reading %s", label, log_file)
            if debug:
                print(f"[{label}] {traceback.format_exc()}")
            continue

    return (total_lines_read, total_points)


def _clone_analysis_data(analysis_data: Dict[int, Dict[str, List[Any]]]) -> Dict[int, Dict[str, List[Any]]]:
    return {
        zone_id: {
            "times": list(values["times"]),
            "mae": list(values["mae"]),
        }
        for zone_id, values in analysis_data.items()
    }


def _extract_analysis_zone_values(obj: Dict[str, Any], ts: datetime, analysis_data: Dict[int, Dict[str, List[Any]]]) -> int:
    """Extract per-zone MAE values for one analysis object. Returns points added count."""
    points_added = 0
    zones = obj.get("analysis", {})
    if not isinstance(zones, dict):
        return 0

    for zone_id in range(1, 7):
        zone_obj = zones.get(str(zone_id), zones.get(zone_id, {}))
        if not isinstance(zone_obj, dict):
            continue
        mae_val = zone_obj.get("avg_abs_error_c")
        if isinstance(mae_val, (int, float)):
            analysis_data[zone_id]["times"].append(ts)
            analysis_data[zone_id]["mae"].append(float(mae_val))
            points_added += 1

    return points_added


def load_analysis_points(logs_dir: Path, time_window_hours: float = 1.0, debug: bool = False) -> Dict[int, Dict[str, List[Any]]]:
    """
    Load MAE analysis points from JSONL logs within the time window.

    Returns dict: {zone_id: {'times': [dt, ...], 'mae': [float, ...]}}
    """
    if debug:
        print(f"[load_analysis_points] loading with history window={time_window_hours}h")

    log_files = find_analysis_log_files(logs_dir)
    if not log_files:
        return {z: {"times": [], "mae": []} for z in range(1, 7)}

    cache_key = (str(logs_dir.resolve()), float(time_window_hours))
    file_signature = _build_log_signature(log_files)
    cached = _ANALYSIS_CACHE.get(cache_key)
    if cached and cached.get("signature") == file_signature:
        if debug:
            print("[load_analysis_points] cache hit")
        return _clone_analysis_data(cached["analysis_data"])

    analysis_data = {z: {"times": [], "mae": []} for z in range(1, 7)}
    lines_read, points_added = _scan_windowed_log_points(
        log_files,
        time_window_hours,
        analysis_data,
        _extract_analysis_zone_values,
        debug=debug,
        label="load_analysis_points",
        error_log_message="load_analysis_points failed reading %s",
    )
    if lines_read == 0 and points_added == 0:
        _ANALYSIS_CACHE[cache_key] = {
            "signature": file_signature,
            "analysis_data": _clone_analysis_data(analysis_data),
        }
        return analysis_data

    if debug:
        print(f"[load_analysis_points] total reverse lines={lines_read}, points added={points_added}")

    for zone_id in range(1, 7):
        analysis_data[zone_id]["times"].reverse()
        analysis_data[zone_id]["mae"].reverse()

    _ANALYSIS_CACHE[cache_key] = {
        "signature": file_signature,
        "analysis_data": _clone_analysis_data(analysis_data),
    }
    return analysis_data


def load_telemetry_points(logs_dir: Path, time_window_hours: float = 1.0, debug: bool = False) -> Dict[int, Dict[str, List[Tuple]]]:
    """
    Load telemetry points from JSONL logs within the time window.
    
    Returns dict: {zone_id: {'times': [dt, ...], 'pv': [float, ...], 'sp': [float, ...], 'sp_autotune': [float, ...]}}
    """
    if debug:
        print(f"[load_telemetry_points] loading with history window={time_window_hours}h")
    
    log_files = find_log_files(logs_dir)
    if debug:
        print(f"[load_telemetry_points] found {len(log_files)} log files: {[f.name for f in log_files]}")
    
    if not log_files:
        # Initialize zones 1-6
        zones_data = {z: {"times": [], "pv": [], "sp": [], "sp_autotune": []} for z in range(1, 7)}
        if debug:
            print(f"[load_telemetry_points] no log files found in {logs_dir}")
        return zones_data

    cache_key = (str(logs_dir.resolve()), float(time_window_hours))
    file_signature = _build_log_signature(log_files)
    cached = _TELEMETRY_CACHE.get(cache_key)
    if cached and cached.get("signature") == file_signature:
        if debug:
            print("[load_telemetry_points] cache hit")
        return _clone_zones_data(cached["zones_data"])

    # Initialize zones 1-6
    zones_data = {z: {"times": [], "pv": [], "sp": [], "sp_autotune": []} for z in range(1, 7)}
    
    # Read only the required history window by scanning newest records backward.
    total_lines_read, total_points = _scan_windowed_log_points(
        log_files,
        time_window_hours,
        zones_data,
        _extract_zone_values,
        debug=debug,
        label="load_telemetry_points",
        error_log_message="load_telemetry_points failed reading %s",
        print_error_prefix="load_telemetry_points",
    )
    if total_lines_read == 0 and total_points == 0:
        _TELEMETRY_CACHE[cache_key] = {
            "signature": file_signature,
            "zones_data": _clone_zones_data(zones_data),
        }
        return zones_data

    # Reverse per-zone lists back to chronological order after reverse scan.
    for zone_id in range(1, 7):
        zones_data[zone_id]["times"].reverse()
        zones_data[zone_id]["pv"].reverse()
        zones_data[zone_id]["sp"].reverse()
        zones_data[zone_id]["sp_autotune"].reverse()

    if debug:
        print(f"[load_telemetry_points] TOTAL: {total_lines_read} lines read, {total_points} points loaded across all zones")
        for z in range(1, 7):
            n_points = len(zones_data[z]["times"])
            if n_points > 0:
                print(f"[load_telemetry_points]   Zone {z}: {n_points} points")

    _TELEMETRY_CACHE[cache_key] = {
        "signature": file_signature,
        "zones_data": _clone_zones_data(zones_data),
    }

    return zones_data


class ZoneChartPanel(tk.Frame):
    """Individual zone chart with PV and setpoints."""
    
    def __init__(self, parent, zone_id: int, logs_dir: Path,
                 viewer_cfg: Dict[str, Any],
                 zone_name: Optional[str] = None,
                 refresh_interval: float = 2.0, debug: bool = False):
        super().__init__(parent)
        self.zone_id = zone_id
        self.zone_name = str(zone_name).strip() if zone_name else f"Zone {zone_id}"
        self.logs_dir = Path(logs_dir)
        self.refresh_interval = refresh_interval
        self.debug = debug
        
        # viewer configuration defaults
        self.history_hours = viewer_cfg.get("history_hours", 1.0)
        self.pv_color = viewer_cfg.get("pv_color", "blue")
        self.sp_color = viewer_cfg.get("sp_color", "red")
        self.sp_autotune_color = viewer_cfg.get("sp_autotune_color", "purple")
        self.line_width = viewer_cfg.get("line_width", 2.5)
        self.show_sp_abs = bool(viewer_cfg.get("show_sp_abs", True))
        self.show_sp_autotune = bool(viewer_cfg.get("show_sp_autotune", True))
        self.show_mae = bool(viewer_cfg.get("show_mae", True))
        
        # Data for this zone only
        self.zone_data = {"times": [], "pv": [], "sp": [], "sp_autotune": []}
        # Timestamp after which new points should be accepted (used by clear)
        self.clear_cutoff: Optional[datetime] = None
        
        self.running = False
        self._last_signature: Optional[Tuple[Any, ...]] = None
        
        # UI
        self.fig: Optional[Figure] = None
        self.canvas: Optional[FigureCanvasTkAgg] = None
        self.toolbar: Optional[NavigationToolbar2Tk] = None
        self.status_label: Optional[ttk.Label] = None
        self.header_label: Optional[ttk.Label] = None
        self.metrics_label: Optional[ttk.Label] = None
        self.ax_pv = None
        self.ax_sp = None
        self.current_mae: Optional[float] = None
        self.mae_series: Dict[str, List[Any]] = {"times": [], "values": []}
        self._mae_ylim: Optional[Tuple[float, float]] = None

        # View state (for preserving user zoom/pan view)
        self._updating_plot = False
        self._view_locked = False
        self._home_view: Optional[Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float]]] = None
        self._locked_view: Optional[Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float]]] = None
        self._interaction_active = False
        self._pending_zone_data: Optional[Dict[str, List[Any]]] = None
        self._draw_scheduled = False
        
        self.create_widgets()
        
        # Initial render
        self.after(100, self._deferred_init)
    
    def _deferred_init(self):
        """Deferred initialization to ensure widget is properly rendered."""
        self._update_plot()
    
    def create_widgets(self):
        # Header
        header = ttk.Frame(self)
        header.pack(fill=tk.X, padx=10, pady=10)
        
        self.header_label = ttk.Label(header, text="", font=("Arial", 12, "bold"))
        self.header_label.pack(side=tk.LEFT)
        self._update_zone_header()
        
        # Clear button (only for this zone)
        clear_btn = ttk.Button(header, text="Clear This Zone", command=self.clear_chart)
        clear_btn.pack(side=tk.RIGHT, padx=5)
        
        # Status label
        self.status_label = ttk.Label(self, text="", font=("Arial", 9))
        self.status_label.pack(fill=tk.X, padx=10)

        # Live metrics summary (centered, one line)
        self.metrics_label = ttk.Label(self, text="", font=("Arial", 10), anchor="center", justify=tk.CENTER)
        self.metrics_label.pack(fill=tk.X, padx=10, pady=(0, 4))
        
        # Canvas frame for matplotlib
        self.canvas_frame = ttk.Frame(self)
        self.canvas_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Create matplotlib figure (single subplot)
        self.fig = Figure(figsize=(12, 5), dpi=100)
        self.ax_pv = self.fig.add_subplot(111)
        self.ax_sp = self.ax_pv.twinx()
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.canvas_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Matplotlib interactive toolbar (zoom/home/save)
        toolbar_frame = ttk.Frame(self)
        toolbar_frame.pack(fill=tk.X, padx=10, pady=(0, 8))
        self.toolbar = ZoneNavigationToolbar(self.canvas, toolbar_frame, on_home_callback=self._on_home_pressed)
        self.toolbar.update()

        # Track user interactions that may change view limits.
        self.canvas.mpl_connect("button_press_event", self._on_user_press_event)
        self.canvas.mpl_connect("button_release_event", self._on_user_release_event)
        self.canvas.mpl_connect("scroll_event", self._on_user_view_event)
    
    def initial_load(self):
        """Load telemetry data from logs for this zone. Applies clear cutoff if one exists."""
        try:
            all_zones_data = load_telemetry_points(self.logs_dir, time_window_hours=self.history_hours, debug=self.debug)
            zone_data = all_zones_data[self.zone_id]
            if self.clear_cutoff:
                # filter out older points
                filtered = {"times": [], "pv": [], "sp": [], "sp_autotune": []}
                for t,p,s,sa in zip(zone_data["times"], zone_data["pv"], zone_data["sp"], zone_data.get("sp_autotune", [])):
                    if t > self.clear_cutoff:
                        filtered["times"].append(t)
                        filtered["pv"].append(p)
                        filtered["sp"].append(s)
                        filtered["sp_autotune"].append(sa)
                zone_data = filtered
            self.zone_data = zone_data
            self._update_plot()
            total_points = len(self.zone_data["times"])
            self.status_label.config(text=f"Loaded {total_points} points")
            if self.debug:
                print(f"[ZoneChartPanel Zone {self.zone_id}] Initial load: {total_points} points")
        except Exception as e:
            self.status_label.config(text=f"Error: {str(e)}")
            if self.debug:
                print(f"[ZoneChartPanel.initial_load Z{self.zone_id}] {traceback.format_exc()}")
    
    def start_auto_refresh(self):
        """Compatibility no-op: parent ChartPanel now drives refresh in main thread."""
        self.running = True
    
    def stop_auto_refresh(self):
        """Stop chart updates for this zone."""
        self.running = False
    
    def refresh(self):
        """Compatibility method retained for API consistency."""
        return

    def _history_seconds(self) -> int:
        return max(1, int(round(float(self.history_hours) * 3600.0)))

    def _update_zone_header(self):
        if self.header_label is not None:
            self.header_label.config(text=f"{self.zone_name} ({self._history_seconds()}-s window)")

    def set_zone_name(self, zone_name: Optional[str]):
        name = str(zone_name).strip() if zone_name else ""
        self.zone_name = name or f"Zone {self.zone_id}"
        self._update_zone_header()

    def set_mae_history(self, mae_history: Dict[str, List[Any]]):
        """Set MAE time-series history for this zone from analysis logs."""
        history = {
            "times": list(mae_history.get("times", [])),
            "values": list(mae_history.get("mae", [])),
        }

        if self.clear_cutoff:
            filtered_times: List[Any] = []
            filtered_vals: List[Any] = []
            for t, v in zip(history["times"], history["values"]):
                if t > self.clear_cutoff:
                    filtered_times.append(t)
                    filtered_vals.append(v)
            history["times"] = filtered_times
            history["values"] = filtered_vals

        self.mae_series = history

    def set_live_metrics(self, zone_metrics: Dict[str, Any], analysis_metrics: Optional[Dict[str, Any]] = None):
        """Update one-line live metrics summary above chart."""
        if self.metrics_label is None:
            return

        pv = zone_metrics.get("pv_c")
        sp = zone_metrics.get("sp_abs") if zone_metrics.get("sp_abs") is not None else zone_metrics.get("sp_abs_c")
        control_method = zone_metrics.get("control_method", "N/A")
        autotune_enable = str(zone_metrics.get("autotune_enable", "N/A"))
        autotune_sp = zone_metrics.get("autotune_sp")
        p_gain = zone_metrics.get("p_gain")
        i_gain = zone_metrics.get("i_gain")
        d_gain = zone_metrics.get("d_gain")

        analysis_metrics = analysis_metrics or {}
        in_equilibrium = analysis_metrics.get("in_equilibrium")
        avg_error = analysis_metrics.get("avg_abs_error_c")

        mae_value = float(avg_error) if isinstance(avg_error, (int, float)) else None
        self.current_mae = mae_value

        if mae_value is not None:
            sample_time = None
            if self.zone_data["times"]:
                sample_time = self.zone_data["times"][-1]
            if sample_time is None:
                sample_time = datetime.now().astimezone()

            if self.clear_cutoff and sample_time <= self.clear_cutoff:
                sample_time = datetime.now().astimezone()

            mae_times = self.mae_series["times"]
            mae_values = self.mae_series["values"]
            if mae_times and mae_times[-1] == sample_time:
                mae_values[-1] = mae_value
            else:
                mae_times.append(sample_time)
                mae_values.append(mae_value)

            # Keep MAE history bounded to chart window to avoid unbounded growth.
            history_cutoff = sample_time - timedelta(hours=self.history_hours)
            while mae_times and mae_times[0] < history_cutoff:
                mae_times.pop(0)
                mae_values.pop(0)

        pv_txt = f"{float(pv):.2f}°C" if isinstance(pv, (int, float)) else "N/A"
        sp_txt = f"{float(sp):.2f}°C" if isinstance(sp, (int, float)) else "N/A"
        if autotune_enable.upper() == "ENABLE":
            at_state = "On"
        elif autotune_enable.upper() == "DISABLE":
            at_state = "Off"
        else:
            at_state = autotune_enable
        at_sp_txt = f"{float(autotune_sp):.2f}°C" if isinstance(autotune_sp, (int, float)) else "N/A"
        p_txt = f"{float(p_gain):.4f}" if isinstance(p_gain, (int, float)) else "N/A"
        i_txt = f"{float(i_gain):.4f}" if isinstance(i_gain, (int, float)) else "N/A"
        d_txt = f"{float(d_gain):.4f}" if isinstance(d_gain, (int, float)) else "N/A"

        if in_equilibrium is True:
            eq_txt = "Yes"
        elif in_equilibrium is False:
            eq_txt = "No"
        else:
            eq_txt = "N/A"
        avg_err_txt = f"{float(avg_error):.3f}°C" if isinstance(avg_error, (int, float)) else "N/A"

        row1 = f"PV: {pv_txt}   SP: {sp_txt}   Equilibrium? {eq_txt}   MAE (|e|): {avg_err_txt}"
        row2 = f"Control: {control_method}   AT: {at_state} ({at_sp_txt})   P: {p_txt}   I: {i_txt}   D: {d_txt}"
        self.metrics_label.config(text=f"{row1}\n{row2}")

        # Do not force an immediate redraw from metrics updates; chart refresh loop handles plotting.

    def set_zone_data(self, new_zone_data: Dict[str, List[Any]]):
        """Set new zone data and redraw only when changed."""
        try:
            # Clone so caller-owned cache structures are not mutated by clear filtering.
            copied_zone_data = {
                "times": list(new_zone_data.get("times", [])),
                "pv": list(new_zone_data.get("pv", [])),
                "sp": list(new_zone_data.get("sp", [])),
                "sp_autotune": list(new_zone_data.get("sp_autotune", [])),
            }

            # Avoid interrupting rectangle zoom/pan while user is actively interacting.
            if self._interaction_active:
                self._pending_zone_data = copied_zone_data
                return

            if self.clear_cutoff:
                # apply cutoff filter
                filtered = {"times": [], "pv": [], "sp": [], "sp_autotune": []}
                for t,p,s,sa in zip(copied_zone_data["times"], copied_zone_data["pv"], copied_zone_data["sp"], copied_zone_data.get("sp_autotune", [])):
                    if t > self.clear_cutoff:
                        filtered["times"].append(t)
                        filtered["pv"].append(p)
                        filtered["sp"].append(s)
                        filtered["sp_autotune"].append(sa)
                copied_zone_data = filtered

            times = copied_zone_data["times"]
            signature = (
                len(times),
                times[-1] if times else None,
                copied_zone_data["pv"][-1] if copied_zone_data["pv"] else None,
                copied_zone_data["sp"][-1] if copied_zone_data["sp"] else None,
                copied_zone_data["sp_autotune"][-1] if copied_zone_data["sp_autotune"] else None,
            )

            if signature != self._last_signature:
                self._last_signature = signature
                self.zone_data = copied_zone_data
                self._update_plot()
                total_points = len(self.zone_data["times"])
                self.status_label.config(text=f"Updated: {total_points} points")
                if self.debug and total_points > 0:
                    print(f"[ZoneChartPanel.set_zone_data Z{self.zone_id}] {total_points} points")
        
        except Exception as e:
            if self.debug:
                print(f"[ZoneChartPanel.set_zone_data Z{self.zone_id}] {traceback.format_exc()}")
            LOGGER.exception("ZoneChartPanel.set_zone_data failed for zone %s", self.zone_id)
    
    def clear_chart(self):
        """Clear this zone's chart display and set cutoff to now.
        Future loads will ignore older data until new points arrive."""
        self.zone_data = {"times": [], "pv": [], "sp": [], "sp_autotune": []}
        self.mae_series = {"times": [], "values": []}
        self._mae_ylim = None
        self._last_signature = None
        self._view_locked = False
        self._locked_view = None
        self.clear_cutoff = datetime.now().astimezone()
        if self.debug:
            print(f"[ZoneChartPanel.clear_chart Z{self.zone_id}] cutoff set to {self.clear_cutoff}")
        self._update_plot()
        self.status_label.config(text="Chart cleared")

    def _capture_current_view(self):
        if self.ax_pv is None or self.ax_sp is None:
            return None
        return (
            tuple(self.ax_pv.get_xlim()),
            tuple(self.ax_pv.get_ylim()),
            tuple(self.ax_sp.get_ylim()),
        )

    def _apply_view(self, view):
        if self.ax_pv is None or self.ax_sp is None or view is None:
            return
        xlim, y_pv, y_sp = view
        self.ax_pv.set_xlim(xlim)
        self.ax_pv.set_ylim(y_pv)
        self.ax_sp.set_ylim(y_sp)

    def _on_user_view_event(self, _event=None):
        if self._updating_plot or self.ax_pv is None or self.ax_sp is None:
            return

        current_view = self._capture_current_view()
        if current_view is None:
            return

        # If user returned to home limits, unlock. Otherwise lock on user-selected view.
        if self._home_view is not None and self._views_close(current_view, self._home_view):
            self._view_locked = False
            self._locked_view = None
        else:
            self._view_locked = True
            self._locked_view = current_view

    def _toolbar_mode_active(self) -> bool:
        if self.toolbar is None:
            return False
        mode = str(getattr(self.toolbar, "mode", "") or "").strip().lower()
        return mode != ""

    def _on_user_press_event(self, event=None):
        if self._updating_plot:
            return
        if event is not None and event.inaxes is None:
            return
        if self._toolbar_mode_active():
            self._interaction_active = True

    def _on_user_release_event(self, event=None):
        self._interaction_active = False
        self._on_user_view_event(event)

        # Apply latest deferred data once interaction is complete.
        if self._pending_zone_data is not None:
            pending = self._pending_zone_data
            self._pending_zone_data = None
            self.set_zone_data(pending)

    def _on_home_pressed(self):
        """Unlock user view when toolbar Home is pressed."""
        self._view_locked = False
        self._locked_view = None

    def _safe_request_draw(self):
        """Request a deferred canvas draw while avoiding re-entrant render calls."""
        if self.canvas is None:
            return
        try:
            widget = self.canvas.get_tk_widget()
            if widget is None or not widget.winfo_exists():
                return
        except Exception:
            LOGGER.exception("Failed checking chart widget state before draw")
            return

        if self._draw_scheduled:
            return

        self._draw_scheduled = True

        def _draw_once():
            self._draw_scheduled = False
            try:
                if self.canvas is not None:
                    self.canvas.draw_idle()
            except Exception:
                LOGGER.exception("Deferred chart draw failed for zone %s", self.zone_id)

        try:
            self.after_idle(_draw_once)
        except Exception:
            self._draw_scheduled = False
            LOGGER.exception("Failed scheduling deferred chart draw for zone %s", self.zone_id)

    @staticmethod
    def _views_close(a, b, tol=1e-9):
        for pair_a, pair_b in zip(a, b):
            for va, vb in zip(pair_a, pair_b):
                if abs(float(va) - float(vb)) > tol:
                    return False
        return True
    
    def _update_plot(self):
        """Redraw the matplotlib chart for this zone."""
        try:
            if self.debug:
                print(f"[ZoneChartPanel._update_plot Z{self.zone_id}] Starting plot update...")

            previous_locked_view = self._locked_view if self._view_locked else None
            self._updating_plot = True

            if self.ax_pv is None or self.ax_sp is None:
                self.ax_pv = self.fig.add_subplot(111)
                self.ax_sp = self.ax_pv.twinx()

            ax_pv = self.ax_pv
            ax_sp = self.ax_sp
            ax_pv.cla()
            ax_sp.cla()
            
            times = self.zone_data["times"]
            pvs = self.zone_data["pv"]
            sps = self.zone_data["sp"]
            sp_autotunes = self.zone_data["sp_autotune"]
            
            # Convert all times to display timezone for consistent display
            display_tz = get_display_timezone()
            times_display = [
                t.astimezone(display_tz) if getattr(t, "tzinfo", None) is not None else t
                for t in times
            ]
            
            if not times:
                ax_pv.text(0.5, 0.5, "No data", ha="center", va="center", 
                          transform=ax_pv.transAxes, fontsize=14)
                self._safe_request_draw()
                return
            
            # Determine x-range from data itself and cap to configured history window
            max_time = max(times_display)
            min_time = max_time - timedelta(hours=self.history_hours)
            if times_display:
                data_min = min(times_display)
                if data_min > min_time:
                    min_time = data_min

            if min_time >= max_time:
                min_time = max_time - timedelta(minutes=1)
            
            # Plot PV on left axis (solid blue line)
            pv_times = [t for t, p in zip(times_display, pvs) if p is not None]
            pv_vals = [p for p in pvs if p is not None]
            if pv_vals:
                ax_pv.plot(pv_times, pv_vals,
                           color=self.pv_color,
                           linewidth=self.line_width,
                           label="PV", linestyle="-")
                if self.debug:
                    print(f"[ZoneChartPanel._update_plot Z{self.zone_id}] plotted {len(pv_vals)} PV points")
            
            # Plot absolute setpoint on left axis
            sp_times = [t for t, s in zip(times_display, sps) if s is not None]
            sp_vals = [s for s in sps if s is not None]
            if self.show_sp_abs and sp_vals:
                ax_pv.plot(sp_times, sp_vals,
                           color=self.sp_color,
                           linewidth=self.line_width,
                           label="SP Abs", linestyle="-")
                if self.debug:
                    print(f"[ZoneChartPanel._update_plot Z{self.zone_id}] plotted {len(sp_vals)} SP Abs points")
            
            # Plot autotune setpoint on left axis
            sp_auto_times = [t for t, s in zip(times_display, sp_autotunes) if s is not None]
            sp_auto_vals = [s for s in sp_autotunes if s is not None]
            if self.show_sp_autotune and sp_auto_vals:
                ax_pv.plot(sp_auto_times, sp_auto_vals,
                           color=self.sp_autotune_color,
                           linewidth=self.line_width,
                           label="SP Autotune", linestyle="--")
                if self.debug:
                    print(f"[ZoneChartPanel._update_plot Z{self.zone_id}] plotted {len(sp_auto_vals)} SP Autotune points")
            
            # Configure axes
            ax_pv.set_xlabel("Time", fontsize=10)
            ax_pv.set_ylabel("Temperature (°C)", fontsize=10, fontweight="bold")
            
            ax_pv.tick_params(axis="y", labelsize=9)
            ax_pv.tick_params(axis="x", labelsize=9)

            # Keep left Y-axis in plain decimal format (no scientific notation / offset).
            y_formatter = ScalarFormatter(useOffset=False)
            y_formatter.set_scientific(False)
            ax_pv.yaxis.set_major_formatter(y_formatter)

            # Plot MAE trend on secondary Y-axis and smooth axis updates to reduce jitter.
            mae_times = [
                t.astimezone(display_tz) if getattr(t, "tzinfo", None) is not None else t
                for t in self.mae_series["times"]
            ]
            mae_vals = list(self.mae_series["values"])
            mae_pairs = [
                (t, v)
                for t, v in zip(mae_times, mae_vals)
                if v is not None and t >= min_time and t <= max_time
            ]

            if self.show_mae and mae_pairs:
                mae_plot_times = [t for t, _ in mae_pairs]
                mae_plot_vals = [float(v) for _, v in mae_pairs]
                ax_sp.plot(
                    mae_plot_times,
                    mae_plot_vals,
                    color="darkgreen",
                    linewidth=max(1.5, float(self.line_width) * 0.8),
                    linestyle="-",
                    label="MAE",
                )

                mae_min = min(mae_plot_vals)
                mae_max = max(mae_plot_vals)
                if mae_max - mae_min < 1e-9:
                    pad = max(0.05, abs(mae_max) * 0.2)
                else:
                    pad = max(0.02, (mae_max - mae_min) * 0.15)

                target_lower = max(0.0, mae_min - pad)
                target_upper = max(target_lower + 0.05, mae_max + pad)
                if self._mae_ylim is None:
                    self._mae_ylim = (target_lower, target_upper)
                else:
                    prev_lower, prev_upper = self._mae_ylim
                    smooth_lower = prev_lower * 0.8 + target_lower * 0.2
                    smooth_upper = prev_upper * 0.8 + target_upper * 0.2
                    # Never clip current data while smoothing.
                    smooth_lower = min(smooth_lower, target_lower)
                    smooth_upper = max(smooth_upper, target_upper)
                    if smooth_upper - smooth_lower < 0.05:
                        smooth_upper = smooth_lower + 0.05
                    self._mae_ylim = (max(0.0, smooth_lower), smooth_upper)

                ax_sp.set_ylim(*self._mae_ylim)
                ax_sp.set_ylabel("MAE (°C)", color="darkgreen", fontsize=10, fontweight="bold")
                ax_sp.yaxis.set_label_position("right")
                ax_sp.yaxis.tick_right()
                ax_sp.tick_params(axis="y", right=True, labelright=True, labelcolor="darkgreen", labelsize=9)
                ax_sp.tick_params(axis="x", bottom=False, labelbottom=False)
                mae_formatter = ScalarFormatter(useOffset=False)
                mae_formatter.set_scientific(False)
                ax_sp.yaxis.set_major_formatter(mae_formatter)
            else:
                self._mae_ylim = None
                ax_sp.set_ylabel("")
                ax_sp.set_yticks([])
                ax_sp.tick_params(axis="y", right=True, labelright=False)
                ax_sp.tick_params(axis="x", bottom=False, labelbottom=False)
            
            # Set X-axis range using computed bounds
            ax_pv.set_xlim(min_time, max_time)
            ax_pv.xaxis.set_major_locator(mdates.AutoDateLocator())
            ax_pv.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S", tz=display_tz))
            
            # Rotate x-axis labels using axis-level API to avoid per-label tick object churn.
            ax_pv.tick_params(axis="x", labelrotation=45)
            
            ax_pv.grid(True, alpha=0.3, linestyle="-", linewidth=0.5)
            
            # Add combined legend for temperature and MAE traces.
            all_lines = ax_pv.get_lines() + ax_sp.get_lines()
            if all_lines:
                labels = [l.get_label() for l in all_lines]
                legend = ax_pv.legend(all_lines, labels, loc="upper left", fontsize=9)
                legend.set_zorder(1000)
                legend.get_frame().set_alpha(0.9)

            # Home view is the auto-scaled view for this data.
            self._home_view = self._capture_current_view()

            # Preserve user-selected rectangle zoom/pan while locked.
            if previous_locked_view is not None:
                self._apply_view(previous_locked_view)
                self._locked_view = previous_locked_view
            
            # Redraw canvas (deferred/coalesced for stability on TkAgg).
            self._safe_request_draw()
            
            if self.debug:
                print(f"[ZoneChartPanel._update_plot Z{self.zone_id}] Canvas drawn successfully")
        
        except Exception as e:
            error_msg = f"Plot error: {str(e)}"
            self.status_label.config(text=error_msg)
            LOGGER.exception("ZoneChartPanel._update_plot failed for zone %s", self.zone_id)
            if self.debug:
                print(f"[ZoneChartPanel._update_plot Z{self.zone_id}] {error_msg}\n{traceback.format_exc()}")
        finally:
            self._updating_plot = False
    
    def destroy(self):
        """Clean up when panel is destroyed."""
        self.stop_auto_refresh()
        super().destroy()


class ChartPanel(tk.Frame):
    """Container for per-zone chart tabs with 1-hour rolling window."""
    
    def __init__(self, parent, logs_dir: Path, refresh_interval: float = 2.0, debug: bool = False):
        super().__init__(parent)
        self.logs_dir = Path(logs_dir)
        self.refresh_interval = refresh_interval
        self.debug = debug
        
        # Zone panels
        self.zone_panels: List[ZoneChartPanel] = []
        self._refresh_after_id: Optional[str] = None
        self.title_label: Optional[ttk.Label] = None
        self.notebook: Optional[ttk.Notebook] = None
        self._zone_names: Dict[int, str] = {z: f"Zone {z}" for z in range(1, 7)}
        
        self.create_widgets()
        
        # Schedule initial load after widget is properly displayed
        self.after(100, self._deferred_init)
    
    def _deferred_init(self):
        """Deferred initialization to ensure widgets are properly rendered."""
        self.refresh()
        self.start_auto_refresh()
    
    def create_widgets(self):
        # Header
        header = ttk.Frame(self)
        header.pack(fill=tk.X, padx=10, pady=10)

        self.title_label = ttk.Label(header, text="", font=("Arial", 14, "bold"))
        self.title_label.pack(side=tk.LEFT)
        
        # Sub-notebook for zones
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # pull viewer settings from service config
        from .state_reader import get_service_config_state
        svc = get_service_config_state(self.logs_dir)
        svc_cfg = svc.get("config", {})
        self._zone_names = _normalize_zone_names(svc_cfg.get("zone_names", {}))
        viewer_cfg = svc_cfg.get("viewer", {})
        if not viewer_cfg:
            viewer_cfg = {
                "history_hours": svc_cfg.get("viewer_history_hours", 1.0),
                "line_width": svc_cfg.get("viewer_line_width", 2.5),
                "pv_color": svc_cfg.get("viewer_pv_color", "blue"),
                "sp_color": svc_cfg.get("viewer_sp_color", "red"),
                "sp_autotune_color": svc_cfg.get("viewer_sp_autotune_color", "purple"),
                "show_sp_abs": svc_cfg.get("viewer_show_sp_abs", True),
                "show_sp_autotune": svc_cfg.get("viewer_show_sp_autotune", True),
                "show_mae": svc_cfg.get("viewer_show_mae", True),
            }
        self._update_title(float(viewer_cfg.get("history_hours", 1.0) or 1.0))

        # Create a panel for each zone using the same viewer config
        for zone_id in range(1, 7):
            zone_panel = ZoneChartPanel(
                self.notebook, zone_id, self.logs_dir,
                viewer_cfg,
                zone_name=self._zone_names.get(zone_id, f"Zone {zone_id}"),
                refresh_interval=self.refresh_interval,
                debug=self.debug
            )
            self.notebook.add(zone_panel, text=self._zone_names.get(zone_id, f"Zone {zone_id}"))
            self.zone_panels.append(zone_panel)

    def apply_zone_names(self, zone_names: Dict[int, str]):
        normalized = _normalize_zone_names(zone_names)
        if normalized == self._zone_names:
            return
        self._zone_names = normalized

        if self.notebook is None:
            return

        for idx, panel in enumerate(self.zone_panels):
            display_name = self._zone_names.get(panel.zone_id, f"Zone {panel.zone_id}")
            panel.set_zone_name(display_name)
            try:
                self.notebook.tab(idx, text=display_name)
            except Exception:
                pass

    def _update_title(self, history_hours: float):
        history_seconds = max(1, int(round(float(history_hours) * 3600.0)))
        if self.title_label is not None:
            self.title_label.config(text=f"Live Telemetry - Per Zone Charts ({history_seconds}-s window)")
    
    def start_auto_refresh(self):
        """Start one main-thread refresh loop for all zone panels."""
        if self._refresh_after_id is not None:
            return
        for panel in self.zone_panels:
            panel.start_auto_refresh()
        self._schedule_next_refresh()

    def _schedule_next_refresh(self):
        interval_ms = max(100, int(self.refresh_interval * 1000))
        self._refresh_after_id = self.after(interval_ms, self._refresh_tick)

    def _refresh_tick(self):
        self._refresh_after_id = None
        self.refresh()
        if any(panel.running for panel in self.zone_panels):
            self._schedule_next_refresh()
    
    def refresh(self):
        """Refresh all zone panels."""
        try:
            self.apply_zone_names(_load_zone_names(self.logs_dir))

            # Load all zones once; distribute to zone tabs.
            all_zones_data = load_telemetry_points(
                self.logs_dir,
                time_window_hours=max(panel.history_hours for panel in self.zone_panels) if self.zone_panels else 1.0,
                debug=False,
            )
            all_analysis_data = load_analysis_points(
                self.logs_dir,
                time_window_hours=max(panel.history_hours for panel in self.zone_panels) if self.zone_panels else 1.0,
                debug=False,
            )

            from .state_reader import get_telemetry_state, get_analysis_state
            telem_state = get_telemetry_state(self.logs_dir)
            telem_zones = (((telem_state or {}).get("telemetry", {}) or {}).get("zones", {}) or {})
            analysis_state = get_analysis_state(self.logs_dir)
            analysis_zones = ((analysis_state or {}).get("analysis", {}) or {})

            for panel in self.zone_panels:
                zone_data = all_zones_data.get(panel.zone_id, {"times": [], "pv": [], "sp": [], "sp_autotune": []})
                zone_mae = all_analysis_data.get(panel.zone_id, {"times": [], "mae": []})
                panel.set_zone_data(zone_data)
                panel.set_mae_history(zone_mae)
                zone_metrics = telem_zones.get(str(panel.zone_id), {}) if isinstance(telem_zones, dict) else {}
                zone_analysis = analysis_zones.get(str(panel.zone_id), {}) if isinstance(analysis_zones, dict) else {}
                panel.set_live_metrics(
                    zone_metrics if isinstance(zone_metrics, dict) else {},
                    zone_analysis if isinstance(zone_analysis, dict) else {},
                )
        except Exception:
            if self.debug:
                print(f"[ChartPanel.refresh] {traceback.format_exc()}")
            LOGGER.exception("ChartPanel.refresh failed")
    
    def stop_auto_refresh(self):
        """Stop auto-refresh on all zone panels."""
        if self._refresh_after_id is not None:
            self.after_cancel(self._refresh_after_id)
            self._refresh_after_id = None
        for panel in self.zone_panels:
            panel.stop_auto_refresh()
    
    def apply_viewer_config(self, viewer_cfg: Dict[str, Any]):
        """Update existing zone panels with new viewer configuration."""
        for panel in self.zone_panels:
            # adjust attributes
            panel.history_hours = viewer_cfg.get("history_hours", panel.history_hours)
            panel.line_width = viewer_cfg.get("line_width", panel.line_width)
            panel.pv_color = viewer_cfg.get("pv_color", panel.pv_color)
            panel.sp_color = viewer_cfg.get("sp_color", panel.sp_color)
            panel.sp_autotune_color = viewer_cfg.get("sp_autotune_color", panel.sp_autotune_color)
            panel.show_sp_abs = bool(viewer_cfg.get("show_sp_abs", panel.show_sp_abs))
            panel.show_sp_autotune = bool(viewer_cfg.get("show_sp_autotune", panel.show_sp_autotune))
            panel.show_mae = bool(viewer_cfg.get("show_mae", panel.show_mae))
            panel._update_zone_header()
            panel._update_plot()

        if self.zone_panels:
            self._update_title(max(panel.history_hours for panel in self.zone_panels))

    def apply_service_config(self, cfg: Dict[str, Any]):
        if not isinstance(cfg, dict):
            return
        self.apply_zone_names(_normalize_zone_names(cfg.get("zone_names", {})))
    
    def destroy(self):
        """Clean up all zone panels when container is destroyed."""
        self.stop_auto_refresh()
        super().destroy()
