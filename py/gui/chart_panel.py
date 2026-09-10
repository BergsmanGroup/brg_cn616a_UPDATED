"""
chart_panel.py

Live telemetry chart panel with 1-hour rolling window.
- Plots PV and setpoint per zone over time
- Auto-scales Y-axis; fixed 1-hour X-axis (max)
- Handles rotated log files (YYYY-MM-DD_NNN pattern)
- Auto-refreshes as new telemetry arrives
- Clear button to reset display
"""

import base64
import tkinter as tk
from tkinter import ttk
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any, Callable
from datetime import datetime, timedelta
import json
import math
import statistics
import traceback
from zoneinfo import ZoneInfo
import logging

from .chart_render_pool import ChartRenderPool


LOGGER = logging.getLogger("cn616a.gui")


# Minimum visible time range (seconds) allowed when zooming in on a zone chart.
MIN_ZOOM_SECONDS = 5.0
_RENDER_DPI = 100.0


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


def _insert_time_gaps(
    times: List[datetime],
    values: List[float],
    *,
    gap_factor: float = 2.5,
    min_gap_seconds: float = 5.0,
) -> Tuple[List[datetime], List[float]]:
    """Insert NaN separators where timestamp gaps exceed expected cadence.

    Matplotlib breaks line segments when Y is NaN, which visually exposes data dropouts.
    """
    if len(times) <= 1 or len(values) <= 1:
        return (times, values)

    pair_count = min(len(times), len(values))
    if pair_count <= 1:
        return (times[:pair_count], values[:pair_count])

    deltas: List[float] = []
    for i in range(1, pair_count):
        try:
            dt_seconds = float((times[i] - times[i - 1]).total_seconds())
        except Exception:
            continue
        if dt_seconds > 0:
            deltas.append(dt_seconds)

    if not deltas:
        return (times[:pair_count], values[:pair_count])

    median_delta = float(statistics.median_low(deltas))
    gap_threshold = max(float(min_gap_seconds), float(gap_factor) * median_delta)

    out_times: List[datetime] = [times[0]]
    out_values: List[float] = [float(values[0])]

    for i in range(1, pair_count):
        curr_time = times[i]
        curr_value = float(values[i])

        try:
            dt_seconds = float((curr_time - times[i - 1]).total_seconds())
        except Exception:
            dt_seconds = 0.0

        if dt_seconds > gap_threshold:
            out_times.append(curr_time)
            out_values.append(float("nan"))

        out_times.append(curr_time)
        out_values.append(curr_value)

    return (out_times, out_values)


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
    """Individual zone chart with PV and setpoints.

    Rendering happens in a separate worker process (see chart_render_pool.py); this
    panel only ever displays a finished PNG image on a plain tk.Canvas and never
    touches matplotlib directly, so a native rendering crash cannot reach the GUI.
    """

    def __init__(self, parent, zone_id: int, logs_dir: Path,
                 viewer_cfg: Dict[str, Any],
                 render_pool: ChartRenderPool,
                 zone_name: Optional[str] = None,
                 refresh_interval: float = 2.0, debug: bool = False):
        super().__init__(parent)
        self.zone_id = zone_id
        self.zone_name = str(zone_name).strip() if zone_name else f"Zone {zone_id}"
        self.logs_dir = Path(logs_dir)
        self.refresh_interval = refresh_interval
        self.debug = debug
        self.render_pool = render_pool
        
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
        self.plot_canvas: Optional[tk.Canvas] = None
        self.status_label: Optional[ttk.Label] = None
        self.header_label: Optional[ttk.Label] = None
        self.metrics_label: Optional[ttk.Label] = None
        self.current_mae: Optional[float] = None
        self.mae_series: Dict[str, List[Any]] = {"times": [], "values": []}
        self._photo_image = None  # keep a reference alive; Tk drops images with no referrer
        self._image_item = None

        # Rendered-view bookkeeping (populated once a render result arrives)
        self._last_axes_bbox_px: Optional[Tuple[float, float, float, float]] = None
        self._last_fig_size_px: Optional[Tuple[float, float]] = None
        self._last_xlim: Optional[Tuple[datetime, datetime]] = None

        # View state: None means "auto" (full history window); otherwise an explicit zoom range.
        self._view_xlim: Optional[Tuple[datetime, datetime]] = None
        self._interaction_active = False
        self._pending_zone_data: Optional[Dict[str, List[Any]]] = None

        # Left-drag rubber-band zoom state
        self._zoom_drag_start_px: Optional[int] = None
        self._zoom_rect_item: Optional[int] = None
        # Right-drag pan state
        self._pan_drag_start_px: Optional[int] = None
        self._pan_start_xlim: Optional[Tuple[datetime, datetime]] = None
        
        self.create_widgets()
        
        # Initial render
        self.after(100, self._deferred_init)
    
    def _deferred_init(self):
        """Deferred initialization to ensure widget is properly rendered."""
        self._request_render()
    
    def create_widgets(self):
        # Header
        header = ttk.Frame(self)
        header.pack(fill=tk.X, padx=10, pady=10)
        
        self.header_label = ttk.Label(header, text="", font=("Arial", 12, "bold"))
        self.header_label.pack(side=tk.LEFT)
        self._update_zone_header()
        
        ttk.Button(header, text="Home", command=self._on_home_pressed).pack(side=tk.RIGHT, padx=5)
        ttk.Button(header, text="Clear This Zone", command=self.clear_chart).pack(side=tk.RIGHT, padx=5)
        
        # Status label
        self.status_label = ttk.Label(self, text="", font=("Arial", 9))
        self.status_label.pack(fill=tk.X, padx=10)

        # Live metrics summary (centered, one line)
        self.metrics_label = ttk.Label(self, text="", font=("Arial", 10), anchor="center", justify=tk.CENTER)
        self.metrics_label.pack(fill=tk.X, padx=10, pady=(0, 4))

        ttk.Label(
            self, foreground="gray", font=("Arial", 8),
            text="Left-drag: zoom to range | Right-drag: pan | Scroll: zoom | Home: reset view",
        ).pack(fill=tk.X, padx=10)
        
        # Canvas frame showing the process-rendered chart image
        self.canvas_frame = ttk.Frame(self)
        self.canvas_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        self.plot_canvas = tk.Canvas(self.canvas_frame, bg="white", highlightthickness=0)
        self.plot_canvas.pack(fill=tk.BOTH, expand=True)
        self._image_item = self.plot_canvas.create_image(0, 0, anchor="nw")

        self.plot_canvas.bind("<Configure>", self._on_canvas_configure)
        self.plot_canvas.bind("<ButtonPress-1>", self._on_left_press)
        self.plot_canvas.bind("<B1-Motion>", self._on_left_drag)
        self.plot_canvas.bind("<ButtonRelease-1>", self._on_left_release)
        self.plot_canvas.bind("<ButtonPress-3>", self._on_right_press)
        self.plot_canvas.bind("<B3-Motion>", self._on_right_drag)
        self.plot_canvas.bind("<ButtonRelease-3>", self._on_right_release)
        self.plot_canvas.bind("<MouseWheel>", self._on_mouse_wheel)
    
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
            self._request_render()
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
                self._request_render()
                if self.debug and len(times) > 0:
                    print(f"[ZoneChartPanel.set_zone_data Z{self.zone_id}] {len(times)} points")
        
        except Exception as e:
            if self.debug:
                print(f"[ZoneChartPanel.set_zone_data Z{self.zone_id}] {traceback.format_exc()}")
            LOGGER.exception("ZoneChartPanel.set_zone_data failed for zone %s", self.zone_id)
    
    def clear_chart(self):
        """Clear this zone's chart display and set cutoff to now.
        Future loads will ignore older data until new points arrive."""
        self.zone_data = {"times": [], "pv": [], "sp": [], "sp_autotune": []}
        self.mae_series = {"times": [], "values": []}
        self._last_signature = None
        self._view_xlim = None
        self.clear_cutoff = datetime.now().astimezone()
        if self.debug:
            print(f"[ZoneChartPanel.clear_chart Z{self.zone_id}] cutoff set to {self.clear_cutoff}")
        self._request_render()
        self.status_label.config(text="Chart cleared")

    # -----------------------------
    # Pixel <-> data-time mapping (uses the axes bbox returned by the last render)
    # -----------------------------
    def _pixel_x_to_time(self, px_x: float) -> Optional[datetime]:
        bbox = self._last_axes_bbox_px
        xlim = self._last_xlim
        if not bbox or not xlim:
            return None
        x0, _, x1, _ = bbox
        if x1 <= x0:
            return None
        t0, t1 = xlim
        frac = (px_x - x0) / (x1 - x0)
        frac = min(1.5, max(-0.5, frac))  # allow slight overshoot at the edges for easier dragging
        return t0 + (t1 - t0) * frac

    def _apply_pending_zone_data(self, *, force_render: bool = False):
        if self._pending_zone_data is not None:
            self.zone_data = self._pending_zone_data
            self._pending_zone_data = None
            self._request_render()
        elif force_render:
            self._request_render()

    # -----------------------------
    # Mouse interaction: left-drag = rubber-band zoom to a time range
    # -----------------------------
    def _on_left_press(self, event):
        if self._last_axes_bbox_px is None:
            return
        self._interaction_active = True
        self._zoom_drag_start_px = event.x
        self._zoom_rect_item = self.plot_canvas.create_rectangle(
            event.x, 0, event.x, self.plot_canvas.winfo_height(),
            outline="#3366cc", width=1, dash=(4, 2),
        )

    def _on_left_drag(self, event):
        if self._zoom_rect_item is None or self._zoom_drag_start_px is None:
            return
        self.plot_canvas.coords(
            self._zoom_rect_item,
            self._zoom_drag_start_px, 0, event.x, self.plot_canvas.winfo_height(),
        )

    def _on_left_release(self, event):
        if self._zoom_rect_item is not None:
            self.plot_canvas.delete(self._zoom_rect_item)
            self._zoom_rect_item = None
        self._interaction_active = False
        start_px = self._zoom_drag_start_px
        self._zoom_drag_start_px = None

        if start_px is None or abs(event.x - start_px) < 4:
            self._apply_pending_zone_data()  # treat as a click, not a drag
            return

        t_a = self._pixel_x_to_time(start_px)
        t_b = self._pixel_x_to_time(event.x)
        if t_a is None or t_b is None:
            self._apply_pending_zone_data()
            return

        lo, hi = (t_a, t_b) if t_a < t_b else (t_b, t_a)
        if (hi - lo).total_seconds() < MIN_ZOOM_SECONDS:
            self._apply_pending_zone_data()
            return

        self._view_xlim = (lo, hi)
        self._apply_pending_zone_data(force_render=True)

    # -----------------------------
    # Mouse interaction: right-drag = pan the visible time range
    # -----------------------------
    def _on_right_press(self, event):
        if self._last_axes_bbox_px is None or self._last_xlim is None:
            return
        self._interaction_active = True
        self._pan_drag_start_px = event.x
        self._pan_start_xlim = self._view_xlim or self._last_xlim

    def _on_right_drag(self, event):
        if self._pan_drag_start_px is None or self._image_item is None:
            return
        # Cheap visual feedback only; the exact recompute + re-render happens on release.
        self.plot_canvas.moveto(self._image_item, event.x - self._pan_drag_start_px, 0)

    def _on_right_release(self, event):
        self._interaction_active = False
        start_px = self._pan_drag_start_px
        base_xlim = self._pan_start_xlim
        self._pan_drag_start_px = None
        self._pan_start_xlim = None
        if self._image_item is not None:
            self.plot_canvas.moveto(self._image_item, 0, 0)

        bbox = self._last_axes_bbox_px
        if start_px is None or base_xlim is None or bbox is None:
            self._apply_pending_zone_data()
            return
        x0, _, x1, _ = bbox
        if x1 <= x0:
            self._apply_pending_zone_data()
            return

        t0, t1 = base_xlim
        width_s = (t1 - t0).total_seconds()
        shift_s = -((event.x - start_px) / (x1 - x0)) * width_s
        self._view_xlim = (t0 + timedelta(seconds=shift_s), t1 + timedelta(seconds=shift_s))
        self._apply_pending_zone_data(force_render=True)

    # -----------------------------
    # Mouse interaction: scroll wheel = zoom centered on cursor
    # -----------------------------
    def _on_mouse_wheel(self, event):
        if self._last_axes_bbox_px is None or self._last_xlim is None:
            return
        anchor_time = self._pixel_x_to_time(event.x)
        if anchor_time is None:
            return

        t0, t1 = self._view_xlim or self._last_xlim
        width_s = max(MIN_ZOOM_SECONDS, (t1 - t0).total_seconds())
        factor = 0.85 if event.delta > 0 else (1.0 / 0.85)
        max_width_s = max(width_s, float(self.history_hours) * 3600.0 * 4.0)
        new_width_s = min(max_width_s, max(MIN_ZOOM_SECONDS, width_s * factor))

        anchor_frac = (anchor_time - t0).total_seconds() / width_s if width_s > 0 else 0.5
        new_t0 = anchor_time - timedelta(seconds=anchor_frac * new_width_s)
        self._view_xlim = (new_t0, new_t0 + timedelta(seconds=new_width_s))
        self._request_render()

    def _on_home_pressed(self):
        """Reset to the auto-scaled full-history view."""
        self._view_xlim = None
        self._request_render()

    def _on_canvas_configure(self, _event=None):
        # A resize changes the figure size in inches; re-render at the new size. submit()
        # naturally coalesces rapid resize events since only one render per zone is ever in flight.
        self._request_render()

    # -----------------------------
    # Render request / result handling (actual drawing happens in a worker process)
    # -----------------------------
    def _request_render(self):
        if self.render_pool is None or self.plot_canvas is None:
            return
        width_px = max(50, int(self.plot_canvas.winfo_width()))
        height_px = max(50, int(self.plot_canvas.winfo_height()))

        request = {
            "zone_name": self.zone_name,
            "times": list(self.zone_data.get("times", [])),
            "pv": list(self.zone_data.get("pv", [])),
            "sp": list(self.zone_data.get("sp", [])),
            "sp_autotune": list(self.zone_data.get("sp_autotune", [])),
            "mae_times": list(self.mae_series.get("times", [])),
            "mae_values": list(self.mae_series.get("values", [])),
            "history_hours": float(self.history_hours),
            "view_xlim": self._view_xlim,
            "line_width": float(self.line_width),
            "pv_color": self.pv_color,
            "sp_color": self.sp_color,
            "sp_autotune_color": self.sp_autotune_color,
            "show_sp_abs": bool(self.show_sp_abs),
            "show_sp_autotune": bool(self.show_sp_autotune),
            "show_mae": bool(self.show_mae),
            "fig_width_in": width_px / _RENDER_DPI,
            "fig_height_in": height_px / _RENDER_DPI,
            "dpi": _RENDER_DPI,
            "tz_name": "America/Los_Angeles",
        }
        self.render_pool.submit(self.zone_id, request)

    def on_render_result(self, result: Dict[str, Any]):
        """Called by the owning ChartPanel when a render finishes for this zone."""
        if "error" in result:
            self.status_label.config(text=f"Chart render error: {result['error']}")
            LOGGER.error("Chart render failed for zone %s: %s", self.zone_id, result["error"])
            return

        png_bytes = result.get("png")
        if not png_bytes:
            return
        try:
            photo = tk.PhotoImage(data=base64.b64encode(png_bytes).decode("ascii"), format="png")
        except Exception:
            LOGGER.exception("Failed decoding rendered chart image for zone %s", self.zone_id)
            return

        self._photo_image = photo
        self.plot_canvas.itemconfigure(self._image_item, image=photo)
        self.plot_canvas.coords(self._image_item, 0, 0)
        self._last_axes_bbox_px = result.get("axes_bbox_px")
        self._last_fig_size_px = result.get("fig_size_px")
        self._last_xlim = result.get("xlim")

        total_points = len(self.zone_data.get("times", []))
        if result.get("has_data"):
            self.status_label.config(text=f"Updated: {total_points} points")
        else:
            self.status_label.config(text="No data")

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
        self._zone_panel_by_id: Dict[int, ZoneChartPanel] = {}
        self._refresh_after_id: Optional[str] = None
        self._render_poll_after_id: Optional[str] = None
        self.title_label: Optional[ttk.Label] = None
        self.notebook: Optional[ttk.Notebook] = None
        self._zone_names: Dict[int, str] = {z: f"Zone {z}" for z in range(1, 7)}

        # One shared render worker process for all six zones; a native rendering crash
        # kills only this worker, never the GUI (see chart_render_pool.py).
        self.render_pool = ChartRenderPool()
        
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
                self.render_pool,
                zone_name=self._zone_names.get(zone_id, f"Zone {zone_id}"),
                refresh_interval=self.refresh_interval,
                debug=self.debug
            )
            self.notebook.add(zone_panel, text=self._zone_names.get(zone_id, f"Zone {zone_id}"))
            self.zone_panels.append(zone_panel)
            self._zone_panel_by_id[zone_id] = zone_panel

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
        self._schedule_render_poll()

    def _schedule_next_refresh(self):
        interval_ms = max(100, int(self.refresh_interval * 1000))
        self._refresh_after_id = self.after(interval_ms, self._refresh_tick)

    def _refresh_tick(self):
        self._refresh_after_id = None
        self.refresh()
        if any(panel.running for panel in self.zone_panels):
            self._schedule_next_refresh()

    def _schedule_render_poll(self):
        # Polled on a short, fixed cadence independent of the (often slower) data refresh
        # interval, so zoom/pan/scroll interactions feel responsive.
        self._render_poll_after_id = self.after(100, self._render_poll_tick)

    def _render_poll_tick(self):
        self._render_poll_after_id = None
        if self.render_pool.is_broken():
            self.render_pool.restart()
        for zone_id, result in self.render_pool.poll_results():
            panel = self._zone_panel_by_id.get(zone_id)
            if panel is not None:
                panel.on_render_result(result)
        if self._refresh_after_id is not None or any(panel.running for panel in self.zone_panels):
            self._schedule_render_poll()
    
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
        if self._render_poll_after_id is not None:
            self.after_cancel(self._render_poll_after_id)
            self._render_poll_after_id = None
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
            panel._request_render()

        if self.zone_panels:
            self._update_title(max(panel.history_hours for panel in self.zone_panels))

    def apply_service_config(self, cfg: Dict[str, Any]):
        if not isinstance(cfg, dict):
            return
        self.apply_zone_names(_normalize_zone_names(cfg.get("zone_names", {})))

    def shutdown_render_pool(self):
        """Explicitly tear down the render worker process. Call before the GUI closes."""
        self.render_pool.shutdown()
    
    def destroy(self):
        """Clean up all zone panels when container is destroyed."""
        self.stop_auto_refresh()
        self.render_pool.shutdown()
        super().destroy()
