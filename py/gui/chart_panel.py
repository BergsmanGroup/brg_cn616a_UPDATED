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
from typing import Dict, List, Tuple, Optional, Any
from datetime import datetime, timedelta
import json
import threading
import time
import traceback
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure


def parse_iso_timestamp(ts_str: str) -> Optional[datetime]:
    """Parse ISO 8601 timestamp string to datetime."""
    if not ts_str:
        return None
    try:
        # Handle both with and without timezone
        return datetime.fromisoformat(ts_str)
    except Exception:
        return None


def find_log_files(logs_dir: Path) -> List[Path]:
    """
    Find telemetry log files, ordered by date/rotation number.
    Handles both cn616a_telemetry_log.jsonl and cn616a_telemetry_log_YYYY-MM-DD_###.jsonl
    """
    pattern_main = "cn616a_telemetry_log.jsonl"
    pattern_rotated = "cn616a_telemetry_log_*.jsonl"
    
    files = []
    
    # Find main log
    main_log = logs_dir / pattern_main
    if main_log.exists():
        files.append(main_log)
    
    # Find rotated logs
    rotated = sorted(logs_dir.glob(pattern_rotated))
    files.extend(rotated)
    
    # Remove duplicates (keep in order: oldest rotated, then main)
    seen = set()
    unique_files = []
    for f in files:
        if f.name not in seen:
            unique_files.append(f)
            seen.add(f.name)
    
    return unique_files


def load_telemetry_points(logs_dir: Path, time_window_hours: float = 1.0, debug: bool = False) -> Dict[int, Dict[str, List[Tuple]]]:
    """
    Load telemetry points from JSONL logs within the time window.
    
    Returns dict: {zone_id: {'times': [dt, ...], 'pv': [float, ...], 'sp': [float, ...], 'sp_autotune': [float, ...]}}
    """
    # Get current time in a timezone-aware format
    now = datetime.now().astimezone()
    cutoff_time = now - timedelta(hours=time_window_hours)
    
    if debug:
        print(f"[load_telemetry_points] now={now}, cutoff_time={cutoff_time}")
    
    # Initialize zones 1-6
    zones_data = {z: {"times": [], "pv": [], "sp": [], "sp_autotune": []} for z in range(1, 7)}
    
    log_files = find_log_files(logs_dir)
    if debug:
        print(f"[load_telemetry_points] found {len(log_files)} log files: {[f.name for f in log_files]}")
    
    if not log_files:
        if debug:
            print(f"[load_telemetry_points] no log files found in {logs_dir}")
        return zones_data
    
    # Read logs in order (oldest first, so new data overwrites if duplicate)
    total_lines_read = 0
    total_points = 0
    
    for log_file in log_files:
        try:
            if debug:
                print(f"[load_telemetry_points] reading {log_file.name}...")
            
            line_count = 0
            skipped_old = 0
            points_added = 0
            
            with open(log_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    
                    line_count += 1
                    
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError as e:
                        if debug:
                            print(f"[load_telemetry_points] JSON parse error on {log_file.name} line {line_count}: {e}")
                        continue
                    
                    # Handle both old and new log formats
                    ts_str = obj.get("timestamp_pacific") or obj.get("ts")
                    if not ts_str:
                        if debug and line_count % 100 == 0:  # Log every 100th to avoid spam
                            print(f"[load_telemetry_points] no timestamp on line {line_count}")
                        continue
                    
                    ts = parse_iso_timestamp(ts_str)
                    if not ts:
                        if debug:
                            print(f"[load_telemetry_points] failed to parse timestamp: {ts_str}")
                        continue
                    
                    if ts < cutoff_time:
                        skipped_old += 1
                        continue  # Skip old entries
                    
                    # Extract zone data (handle both old and new formats)
                    zones = obj.get("data", {}).get("zones", {})
                    if not zones:
                        zones = obj.get("telemetry", {}).get("zones", {})
                    
                    for zone_id_str in ["1", "2", "3", "4", "5", "6"]:
                        zone_id = int(zone_id_str)
                        zone_data = zones.get(zone_id_str, {})
                        
                        pv = zone_data.get("pv_c")
                        # Try both old (sp_abs_c) and new (sp_abs) field names
                        sp = zone_data.get("sp_abs_c") or zone_data.get("sp_abs")
                        # Autotune setpoint (also try both formats)
                        sp_autotune = zone_data.get("autotune_sp_c") or zone_data.get("autotune_sp")
                        
                        # Only record if at least one value is not None
                        if pv is not None or sp is not None or sp_autotune is not None:
                            zones_data[zone_id]["times"].append(ts)
                            zones_data[zone_id]["pv"].append(pv)
                            zones_data[zone_id]["sp"].append(sp)
                            zones_data[zone_id]["sp_autotune"].append(sp_autotune)
                            points_added += 1
            
            total_lines_read += line_count
            total_points += points_added
            
            if debug:
                print(f"[load_telemetry_points]   -> {line_count} lines, {skipped_old} skipped (too old), {points_added} points added")
        
        except Exception as e:
            print(f"[load_telemetry_points] Error reading {log_file}: {e}")
            if debug:
                print(f"[load_telemetry_points] {traceback.format_exc()}")
            continue
    
    if debug:
        print(f"[load_telemetry_points] TOTAL: {total_lines_read} lines read, {total_points} points loaded across all zones")
        for z in range(1, 7):
            n_points = len(zones_data[z]["times"])
            if n_points > 0:
                print(f"[load_telemetry_points]   Zone {z}: {n_points} points")
    
    return zones_data


class ZoneChartPanel(tk.Frame):
    """Individual zone chart with PV and setpoints."""
    
    def __init__(self, parent, zone_id: int, logs_dir: Path,
                 viewer_cfg: Dict[str, Any],
                 refresh_interval: float = 2.0, debug: bool = False):
        super().__init__(parent)
        self.zone_id = zone_id
        self.logs_dir = Path(logs_dir)
        self.refresh_interval = refresh_interval
        self.debug = debug
        
        # viewer configuration defaults
        self.history_hours = viewer_cfg.get("history_hours", 1.0)
        self.pv_color = viewer_cfg.get("pv_color", "blue")
        self.sp_color = viewer_cfg.get("sp_color", "red")
        self.sp_autotune_color = viewer_cfg.get("sp_autotune_color", "purple")
        self.line_width = viewer_cfg.get("line_width", 2.5)
        
        # Data for this zone only
        self.zone_data = {"times": [], "pv": [], "sp": [], "sp_autotune": []}
        # Timestamp after which new points should be accepted (used by clear)
        self.clear_cutoff: Optional[datetime] = None
        
        # Threading
        self.running = False
        self.refresh_thread = None
        
        # UI
        self.fig: Optional[Figure] = None
        self.canvas: Optional[FigureCanvasTkAgg] = None
        self.status_label: Optional[ttk.Label] = None
        
        self.create_widgets()
        
        # Schedule initial load after widget is properly displayed
        self.after(100, self._deferred_init)
    
    def _deferred_init(self):
        """Deferred initialization to ensure widget is properly rendered."""
        self.initial_load()
        self.start_auto_refresh()
    
    def create_widgets(self):
        # Header
        header = ttk.Frame(self)
        header.pack(fill=tk.X, padx=10, pady=10)
        
        ttk.Label(header, text=f"Zone {self.zone_id} (1-hr window)", 
                 font=("Arial", 12, "bold")).pack(side=tk.LEFT)
        
        # Clear button (only for this zone)
        clear_btn = ttk.Button(header, text="Clear This Zone", command=self.clear_chart)
        clear_btn.pack(side=tk.RIGHT, padx=5)
        
        # Status label
        self.status_label = ttk.Label(self, text="", font=("Arial", 9))
        self.status_label.pack(fill=tk.X, padx=10)
        
        # Canvas frame for matplotlib
        self.canvas_frame = ttk.Frame(self)
        self.canvas_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Create matplotlib figure (single subplot)
        self.fig = Figure(figsize=(12, 5), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.canvas_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
    
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
        """Start background thread that refreshes chart."""
        if self.running:
            return
        self.running = True
        self.refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self.refresh_thread.start()
    
    def _refresh_loop(self):
        """Background thread loop for refresh."""
        while self.running:
            try:
                self.refresh()
            except Exception as e:
                if self.debug:
                    print(f"[ZoneChartPanel._refresh_loop Z{self.zone_id}] {traceback.format_exc()}")
            time.sleep(self.refresh_interval)
    
    def stop_auto_refresh(self):
        """Stop background refresh thread."""
        self.running = False
        if self.refresh_thread:
            self.refresh_thread.join(timeout=1)
    
    def refresh(self):
        """Check for new telemetry and update chart."""
        try:
            new_zones_data = load_telemetry_points(self.logs_dir, time_window_hours=self.history_hours, debug=False)
            new_zone_data = new_zones_data[self.zone_id]
            
            if self.clear_cutoff:
                # apply cutoff filter
                filtered = {"times": [], "pv": [], "sp": [], "sp_autotune": []}
                for t,p,s,sa in zip(new_zone_data["times"], new_zone_data["pv"], new_zone_data["sp"], new_zone_data.get("sp_autotune", [])):
                    if t > self.clear_cutoff:
                        filtered["times"].append(t)
                        filtered["pv"].append(p)
                        filtered["sp"].append(s)
                        filtered["sp_autotune"].append(sa)
                new_zone_data = filtered
            
            # Check if data changed (after filtering)
            if len(new_zone_data["times"]) != len(self.zone_data["times"]):
                self.zone_data = new_zone_data
                self._update_plot()
                total_points = len(self.zone_data["times"])
                self.status_label.config(text=f"Updated: {total_points} points")
                if self.debug and total_points > 0:
                    print(f"[ZoneChartPanel.refresh Z{self.zone_id}] {total_points} points")
        
        except Exception as e:
            if self.debug:
                print(f"[ZoneChartPanel.refresh Z{self.zone_id}] {traceback.format_exc()}")
    
    def clear_chart(self):
        """Clear this zone's chart display and set cutoff to now.
        Future loads will ignore older data until new points arrive."""
        self.zone_data = {"times": [], "pv": [], "sp": [], "sp_autotune": []}
        self.clear_cutoff = datetime.now().astimezone()
        if self.debug:
            print(f"[ZoneChartPanel.clear_chart Z{self.zone_id}] cutoff set to {self.clear_cutoff}")
        self._update_plot()
        self.status_label.config(text="Chart cleared")
    
    def _update_plot(self):
        """Redraw the matplotlib chart for this zone."""
        try:
            if self.debug:
                print(f"[ZoneChartPanel._update_plot Z{self.zone_id}] Starting plot update...")
            
            self.fig.clear()
            ax_pv = self.fig.add_subplot(111)
            ax_sp = ax_pv.twinx()  # Second Y-axis on the right
            
            times = self.zone_data["times"]
            pvs = self.zone_data["pv"]
            sps = self.zone_data["sp"]
            sp_autotunes = self.zone_data["sp_autotune"]
            
            # Convert all times to Pacific timezone for consistent display
            pacific = ZoneInfo("America/Los_Angeles")
            times_pacific = [t.astimezone(pacific) if t.tzinfo != pacific else t for t in times]
            
            if not times:
                ax_pv.text(0.5, 0.5, "No data", ha="center", va="center", 
                          transform=ax_pv.transAxes, fontsize=14)
                self.fig.suptitle(f"Zone {self.zone_id}", fontsize=12, fontweight="bold")
                self.canvas.draw()
                return
            
            now = datetime.now().astimezone(pacific)
            one_hour_ago = now - timedelta(hours=self.history_hours)
            
            # determine x-range: use data bounds but cap to 1 hour window
            if times:
                min_time = min(times)
                max_time = max(times)
                # ensure max_time is not in future
                if max_time > now:
                    max_time = now
                # if span >1h, slide window to last hour
                if (max_time - min_time) > timedelta(hours=1):
                    min_time = max_time - timedelta(hours=1)
            else:
                min_time = one_hour_ago
                max_time = now
            
            # Plot PV on left axis (solid blue line)
            pv_times = [t for t, p in zip(times, pvs) if p is not None]
            pv_vals = [p for p in pvs if p is not None]
            if pv_vals:
                ax_pv.plot(pv_times, pv_vals,
                           color=self.pv_color,
                           linewidth=self.line_width,
                           label="PV", linestyle="-")
                if self.debug:
                    print(f"[ZoneChartPanel._update_plot Z{self.zone_id}] plotted {len(pv_vals)} PV points")
            
            # Plot absolute setpoint on right axis
            sp_times = [t for t, s in zip(times, sps) if s is not None]
            sp_vals = [s for s in sps if s is not None]
            if sp_vals:
                ax_sp.plot(sp_times, sp_vals,
                           color=self.sp_color,
                           linewidth=self.line_width,
                           label="SP Abs", linestyle="-")
                if self.debug:
                    print(f"[ZoneChartPanel._update_plot Z{self.zone_id}] plotted {len(sp_vals)} SP Abs points")
            
            # Plot autotune setpoint on right axis
            sp_auto_times = [t for t, s in zip(times, sp_autotunes) if s is not None]
            sp_auto_vals = [s for s in sp_autotunes if s is not None]
            if sp_auto_vals:
                ax_sp.plot(sp_auto_times, sp_auto_vals,
                           color=self.sp_autotune_color,
                           linewidth=self.line_width,
                           label="SP Autotune", linestyle="--")
                if self.debug:
                    print(f"[ZoneChartPanel._update_plot Z{self.zone_id}] plotted {len(sp_auto_vals)} SP Autotune points")
            
            # Configure axes
            ax_pv.set_title(f"Zone {self.zone_id}", fontweight="bold", fontsize=11)
            ax_pv.set_xlabel("Time", fontsize=10)
            ax_pv.set_ylabel("PV (°C)", color="blue", fontsize=10, fontweight="bold")
            ax_sp.set_ylabel("Setpoint (°C)", color="darkred", fontsize=10, fontweight="bold")
            
            ax_pv.tick_params(axis="y", labelcolor="blue", labelsize=9)
            ax_sp.tick_params(axis="y", labelcolor="darkred", labelsize=9)
            ax_pv.tick_params(axis="x", labelsize=9)
            
            # Set X-axis range using computed bounds
            ax_pv.set_xlim(min_time, max_time)
            
            # Rotate x-axis labels
            for label in ax_pv.get_xticklabels():
                label.set_rotation(45)
                label.set_ha("right")
            
            ax_pv.grid(True, alpha=0.3, linestyle="-", linewidth=0.5)
            
            # Add combined legend
            lines_pv = ax_pv.get_lines()
            lines_sp = ax_sp.get_lines()
            all_lines = lines_pv + lines_sp
            if all_lines:
                labels = [l.get_label() for l in all_lines]
                ax_pv.legend(all_lines, labels, loc="upper left", fontsize=9)
            
            # Tight layout
            self.fig.tight_layout()
            
            # Redraw canvas
            self.canvas.draw()
            self.canvas.get_tk_widget().update_idletasks()
            
            if self.debug:
                print(f"[ZoneChartPanel._update_plot Z{self.zone_id}] Canvas drawn successfully")
        
        except Exception as e:
            error_msg = f"Plot error: {str(e)}"
            self.status_label.config(text=error_msg)
            if self.debug:
                print(f"[ZoneChartPanel._update_plot Z{self.zone_id}] {error_msg}\n{traceback.format_exc()}")
    
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
        
        self.create_widgets()
        
        # Schedule initial load after widget is properly displayed
        self.after(100, self._deferred_init)
    
    def _deferred_init(self):
        """Deferred initialization to ensure widgets are properly rendered."""
        # All zone panels start their own auto-refresh
        pass
    
    def create_widgets(self):
        # Header
        header = ttk.Frame(self)
        header.pack(fill=tk.X, padx=10, pady=10)
        
        ttk.Label(header, text="Live Telemetry - Per Zone Charts (1-hr window)", 
                 font=("Arial", 14, "bold")).pack(side=tk.LEFT)
        
        # Sub-notebook for zones
        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # pull viewer settings from service config
        from .state_reader import get_service_config_state
        svc = get_service_config_state(self.logs_dir)
        viewer_cfg = svc.get("config", {}).get("viewer", {})
        # Create a panel for each zone using the same viewer config
        for zone_id in range(1, 7):
            zone_panel = ZoneChartPanel(
                notebook, zone_id, self.logs_dir,
                viewer_cfg,
                refresh_interval=self.refresh_interval,
                debug=self.debug
            )
            notebook.add(zone_panel, text=f"Zone {zone_id}")
            self.zone_panels.append(zone_panel)
    
    def start_auto_refresh(self):
        """Start auto-refresh on all zone panels."""
        for panel in self.zone_panels:
            panel.start_auto_refresh()
    
    def refresh(self):
        """Refresh all zone panels."""
        for panel in self.zone_panels:
            panel.refresh()
    
    def stop_auto_refresh(self):
        """Stop auto-refresh on all zone panels."""
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
            # force a refresh so lines redraw
            panel.refresh()
    
    def destroy(self):
        """Clean up all zone panels when container is destroyed."""
        self.stop_auto_refresh()
        super().destroy()
