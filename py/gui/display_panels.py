"""
display_panels.py

Tkinter display panels for CN616A state visualization.
Each panel is a self-contained component that can be composed into a larger app.
"""

import tkinter as tk
from tkinter import ttk
from pathlib import Path
from typing import Dict, Any, List, Optional
import threading
import time
import traceback
import json

from .state_reader import (
    get_telemetry_state, get_config_state, get_rampsoak_state, get_analysis_state,
    get_service_config_state,
    format_timestamp, safe_get
)


class StatePanel(tk.Frame):
    """Base class for state display panels."""
    
    def __init__(self, parent, logs_dir: Path, auto_refresh_interval: float = 2.0, debug: bool = False):
        super().__init__(parent)
        self.logs_dir = Path(logs_dir)
        self.auto_refresh_interval = auto_refresh_interval
        self.debug = debug
        self.refresh_thread = None
        self.running = False
    
    def _debug_log(self, msg: str):
        """Print debug message only if debug mode is enabled."""
        if self.debug:
            print(msg)
    
    def start_auto_refresh(self):
        """Start background thread that refreshes display."""
        if self.running:
            return
        self.running = True
        self.refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self.refresh_thread.start()
    
    def stop_auto_refresh(self):
        """Stop background refresh thread."""
        self.running = False
        if self.refresh_thread:
            self.refresh_thread.join(timeout=1.0)
    
    def _refresh_loop(self):
        """Background thread that periodically refreshes."""
        while self.running:
            try:
                self.after(0, self.refresh)
            except Exception:
                pass
            time.sleep(self.auto_refresh_interval)
    
    def refresh(self):
        """Override in subclass to update display."""
        pass


class TelemetryPanel(StatePanel):
    """Display telemetry: PV, setpoint, output per zone."""
    
    def __init__(self, parent, logs_dir: Path, debug: bool = False):
        super().__init__(parent, logs_dir, debug=debug)
        self.create_widgets()
        self.zone_labels = {}
    
    def create_widgets(self):
        # Header
        header = ttk.Frame(self)
        header.pack(fill=tk.X, padx=10, pady=10)
        
        ttk.Label(header, text="CN616A Telemetry", font=("Arial", 14, "bold")).pack(side=tk.LEFT)
        ttk.Label(header, text="(Live)", foreground="green").pack(side=tk.LEFT, padx=10)
        
        # Info line
        self.info_label = ttk.Label(self, text="", font=("Arial", 9))
        self.info_label.pack(fill=tk.X, padx=10)
        
        # Zones frame with scrollbar
        canvas_frame = ttk.Frame(self)
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        canvas = tk.Canvas(canvas_frame, height=300)
        scrollbar = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        self.zones_container = scrollable_frame
        
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    
    def _format_telemetry_zone(self, zone_data: Dict, analysis_data: Dict, zone_config: Dict) -> str:
        """Format telemetry, analysis, config data for a zone."""
        lines = []
        
        # === SENSOR INFO ===
        lines.append("Sensor Info:")
        sensor_type = safe_get(zone_data, "sensor_type", default="N/A")
        sensor_subtype = safe_get(zone_data, "sensor_subtype", default="N/A")
        
        # Map sensor subtypes to thermocouple types (from register map)
        tc_types = {
            0: "B", 1: "C", 2: "E", 3: "J", 
            4: "K", 5: "R", 6: "S", 7: "T", 8: "N"
        }
        tc_type_display = tc_types.get(sensor_subtype, f"Type {sensor_subtype}") if isinstance(sensor_subtype, int) else sensor_subtype
        
        sensor_status = safe_get(zone_config, "sensor_status", default="N/A")
        lines.append(f"  Type: {sensor_type}  |  Thermocouple: {tc_type_display}  |  Status: {sensor_status}")
        
        # === NUMERIC TELEMETRY ===
        lines.append("")  # Blank line separator
        lines.append("Numeric Telemetry:")
        
        pv = safe_get(zone_data, "pv_c", default="N/A")
        sp = safe_get(zone_data, "sp_abs", default="N/A")
        out = safe_get(zone_data, "out_pct", default="N/A")
        
        if isinstance(pv, float):
            pv = f"{pv:.2f}°C"
        if isinstance(sp, float):
            sp = f"{sp:.2f}°C"
        if isinstance(out, float):
            out = f"{out:.1f}%"
        
        lines.append(f"  PV: {pv}  |  SP: {sp}  |  Output: {out}")
        
        # === STATUS TELEMETRY ===
        lines.append("")  # Blank line separator
        lines.append("Status Telemetry:")
        
        control_method = safe_get(zone_data, "control_method", default="N/A")
        autotune_enable = safe_get(zone_data, "autotune_enable", default="N/A")
        control_mode = safe_get(zone_data, "control_mode", default="N/A")
        loop_status = safe_get(zone_data, "loop_status", default="N/A")
        
        # Convert ENABLE/DISABLE to On/Off and always add SP
        autotune_sp = safe_get(zone_data, "autotune_sp", default=None)
        if isinstance(autotune_sp, float):
            autotune_sp_display = f"({autotune_sp:.1f}°C)"
        else:
            autotune_sp_display = ""
        
        if autotune_enable == "ENABLE":
            autotune_display = f"On {autotune_sp_display}".strip()
        elif autotune_enable == "DISABLE":
            autotune_display = f"Off {autotune_sp_display}".strip()
        else:
            autotune_display = autotune_enable
        
        lines.append(f"  Control Method: {control_method}  |  Autotune: {autotune_display}")
        lines.append(f"  Mode: {control_mode}  |  Status: {loop_status}")
        
        # Segment and Ramp/Soak
        segment_idx = safe_get(zone_data, "current_segment_index", default="N/A")
        segment_state = safe_get(zone_data, "current_segment_state", default="N/A")
        ramp_remaining = safe_get(zone_data, "ramp_soak_remaining", default="N/A")
        
        if isinstance(ramp_remaining, float):
            ramp_remaining = f"{ramp_remaining:.1f}s"
        
        lines.append(f"  Segment: {segment_idx} ({segment_state})  |  Ramp/Soak Remaining: {ramp_remaining}")
        
        # === PID PARAMETERS ===
        lines.append("")  # Blank line separator
        lines.append("PID Parameters:")
        
        # Get PID gains directly from telemetry zone data (now part of telemetry polling)
        p_gain = safe_get(zone_data, "p_gain", default=None)
        i_gain = safe_get(zone_data, "i_gain", default=None)
        d_gain = safe_get(zone_data, "d_gain", default=None)
        
        # Format floats nicely
        if p_gain is not None:
            p_gain = f"{p_gain:.4f}"
        else:
            p_gain = "N/A"
        if i_gain is not None:
            i_gain = f"{i_gain:.4f}"
        else:
            i_gain = "N/A"
        if d_gain is not None:
            d_gain = f"{d_gain:.4f}"
        else:
            d_gain = "N/A"
        
        lines.append(f"  P: {p_gain}  |  I: {i_gain}  |  D: {d_gain}")
        
        # Try to get deadband and cycle_time from config if available
        pid_params = zone_config.get("pid_parameters", {})
        deadband = safe_get(pid_params, "deadband", default=None)
        cycle_time = safe_get(pid_params, "cycle_time_s", default=None)
        
        if deadband is not None:
            deadband = f"{deadband:.2f}°C"
        else:
            deadband = "N/A"
        if cycle_time is not None:
            cycle_time = f"{cycle_time:.1f}s"
        else:
            cycle_time = "N/A"
        
        if deadband != "N/A" or cycle_time != "N/A":
            lines.append(f"  Deadband: {deadband}  |  Cycle Time: {cycle_time}")
        
        # === ANALYSIS DATA ===
        if analysis_data:
            lines.append("")  # Blank line separator
            lines.append("Analysis:")
            
            equilibrium = safe_get(analysis_data, "in_equilibrium", default="N/A")
            avg_error = safe_get(analysis_data, "avg_abs_error_c", default="N/A")
            threshold = safe_get(analysis_data, "threshold_c", default="N/A")
            
            if isinstance(avg_error, float):
                avg_error = f"{avg_error:.3f}°C"
            if isinstance(threshold, float):
                threshold = f"{threshold:.2f}°C"
            
            status_icon = "✓" if equilibrium is True else "✗" if equilibrium is False else "?"
            lines.append(f"  {status_icon} Equilibrium: {equilibrium}  |  Avg Error: {avg_error}  |  Threshold: {threshold}")
            
            window = safe_get(analysis_data, "window_s", default="N/A")
            n_points = safe_get(analysis_data, "n_points", default="N/A")
            
            if isinstance(window, float):
                window = f"{window:.0f}s"
            
            lines.append(f"  Window: {window}  |  Points: {n_points}")
        
        return "\n".join(lines)
    
    def refresh(self):
        """Update telemetry display."""
        try:
            telem_state = get_telemetry_state(self.logs_dir)
            analysis_state = get_analysis_state(self.logs_dir)
            config_state = get_config_state(self.logs_dir)
            
            ts = telem_state.get("ts")
            port = telem_state.get("port")
            telemetry = telem_state.get("telemetry", {})
            analysis = analysis_state.get("analysis", {})
            config = config_state.get("config", {})
            zones_config = config.get("zones", {})
            
            # Show the timestamp of whichever is more recent
            analysis_ts = analysis_state.get("ts")
            display_ts = ts if ts else analysis_ts
            
            self.info_label.config(text=f"Last update: {format_timestamp(display_ts)} | Port: {port}")
            
            if not telemetry or "zones" not in telemetry:
                for label in self.zone_labels.values():
                    label.config(text="No data available")
                return
            
            zones_data = telemetry.get("zones", {})
            
            # Clear old labels if needed
            if len(self.zone_labels) != len(zones_data):
                for widget in self.zones_container.winfo_children():
                    widget.destroy()
                self.zone_labels.clear()
            
            # Update or create zone displays
            for zone_id in sorted(zones_data.keys(), key=lambda x: int(x) if x.isdigit() else 999):
                if zone_id not in self.zone_labels:
                    zone_frame = ttk.LabelFrame(self.zones_container, text=f"Zone {zone_id}")
                    zone_frame.pack(fill=tk.X, pady=5)
                    label = ttk.Label(zone_frame, text="", justify=tk.LEFT, font=("Courier", 9))
                    label.pack(padx=10, pady=5)
                    self.zone_labels[zone_id] = label
                
                # Get telemetry, analysis, and config data for this zone
                zone_data = zones_data[zone_id]
                zone_analysis = analysis.get(zone_id, {})
                zone_config = zones_config.get(zone_id, {})
                
                text = self._format_telemetry_zone(zone_data, zone_analysis, zone_config)
                self.zone_labels[zone_id].config(text=text)
        
        except TypeError as e:
            error_msg = f"Type Error - {str(e)}\n{traceback.format_exc()}"
            print(f"[TelemetryPanel.refresh] {error_msg}")
            self.info_label.config(text=f"Error: {str(e)}")
        except Exception as e:
            error_msg = f"Error: {type(e).__name__} - {str(e)}\n{traceback.format_exc()}"
            print(f"[TelemetryPanel.refresh] {error_msg}")
            self.info_label.config(text=f"Error: {str(e)}")


class ConfigPanel(StatePanel):
    """Display configuration: system settings and zone alarms."""
    
    def __init__(self, parent, logs_dir: Path, debug: bool = False, on_viewer_config_changed=None):
        super().__init__(parent, logs_dir, debug=debug)
        self.on_viewer_config_changed = on_viewer_config_changed
        self.create_widgets()
    
    def create_widgets(self):
        # Header
        header = ttk.Frame(self)
        header.pack(fill=tk.X, padx=10, pady=10)
        
        ttk.Label(header, text="CN616A Configuration", font=("Arial", 14, "bold")).pack(side=tk.LEFT)
        
        # Info line
        self.info_label = ttk.Label(self, text="", font=("Arial", 9))
        self.info_label.pack(fill=tk.X, padx=10)
        
        # Notebook for System / Zones
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # child tabs: system, zones, service config
        self.system_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.system_frame, text="System Configuration")
        
        self.zones_notebook = ttk.Notebook(self.notebook)
        self.notebook.add(self.zones_notebook, text="Zones")
        
        self.service_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.service_frame, text="Service Configuration")
        
        # widgets for system tab
        self.system_label = ttk.Label(self.system_frame, text="", justify=tk.LEFT, font=("Courier", 9))
        self.system_label.pack(padx=10, pady=10, anchor="nw")
        
        # widgets for service tab
        self.service_info_label = ttk.Label(self.service_frame, text="", font=("Arial", 9))
        self.service_info_label.pack(fill=tk.X, padx=10)
        self.service_label = ttk.Label(self.service_frame, text="", justify=tk.LEFT, font=("Courier", 9))
        self.service_label.pack(padx=10, pady=10, anchor="nw")
        
        # viewer config form (history, colors, width)
        self.viewer_frame = ttk.Frame(self.service_frame)
        self.viewer_frame.pack(fill=tk.X, padx=10, pady=5)
        # entries dictionary
        self._viewer_entries = {}
        labels = ["History (hrs)", "Line width", "PV color", "SP color", "SP autotune color"]
        keys = ["history_hours", "line_width", "pv_color", "sp_color", "sp_autotune_color"]
        for i,(lbl,key) in enumerate(zip(labels, keys)):
            ttk.Label(self.viewer_frame, text=lbl+":").grid(row=i, column=0, sticky="e", pady=2)
            ent = ttk.Entry(self.viewer_frame, width=15)
            ent.grid(row=i, column=1, pady=2, sticky="w")
            self._viewer_entries[key] = ent
        save_btn = ttk.Button(self.viewer_frame, text="Save viewer settings", command=self._save_viewer_settings)
        save_btn.grid(row=len(labels), column=0, columnspan=2, pady=5)
        
        self.zone_config_labels = {}
    
    def refresh(self):
        """Update config display."""
        try:
            state = get_config_state(self.logs_dir)
            ts = state.get("ts")
            config = state.get("config", {})
            
            self.info_label.config(text=f"Last update: {format_timestamp(ts)}")
            
            if not config:
                self.system_label.config(text="No config data available")
                return
            
            # System section
            system = config.get("system", {})
            system_text = self._format_system_config(system)
            self.system_label.config(text=system_text)
            
            # Zones section
            zones = config.get("zones", {})
            self._update_zones_config(zones)
            
            # Service configuration section (separate state file)
            svc_state = get_service_config_state(self.logs_dir)
            svc_ts = svc_state.get("ts")
            svc_cfg = svc_state.get("config", {})
            self.service_info_label.config(text=f"Last update: {format_timestamp(svc_ts)}")
            if not svc_cfg:
                self.service_label.config(text="No service config data available")
            else:
                self.service_label.config(text=self._format_service_config(svc_cfg))
                # populate viewer settings form if present
                viewer = svc_cfg.get("viewer", {})
                self._populate_viewer_form(viewer)
        
        except TypeError as e:
            error_msg = f"Type Error - {str(e)}\n{traceback.format_exc()}"
            print(f"[ConfigPanel.refresh] {error_msg}")
            self.info_label.config(text=f"Error: {str(e)}")
        except Exception as e:
            error_msg = f"Error: {type(e).__name__} - {str(e)}\n{traceback.format_exc()}"
            print(f"[ConfigPanel.refresh] {error_msg}")
            self.info_label.config(text=f"Error: {str(e)}")
    
    def _populate_viewer_form(self, viewer: Dict[str, Any]):
        """Fill the viewer settings entries from a dict."""
        # viewer may contain history_hours, line_width, pv_color, sp_color, sp_autotune_color
        for key, entry in self._viewer_entries.items():
            val = viewer.get(key)
            if val is not None:
                entry.delete(0, tk.END)
                entry.insert(0, str(val))
        
    def _save_viewer_settings(self):
        """Gather form values, write to service config state file and notify callback."""
        # read existing state
        path = self.logs_dir / "cn616a_service_config_state.json"
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {}
        cfg = state.get("config", {})
        viewer = cfg.get("viewer", {})
        # collect from entries
        for key, entry in self._viewer_entries.items():
            text = entry.get().strip()
            if text == "":
                continue
            # convert numeric where appropriate
            if key in ("history_hours", "line_width"):
                try:
                    val = float(text)
                except ValueError:
                    val = None
            else:
                val = text
            if val is not None:
                viewer[key] = val
        cfg["viewer"] = viewer
        state["config"] = cfg
        # write back
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            self.service_label.config(text=self._format_service_config(cfg))
            if self.on_viewer_config_changed:
                self.on_viewer_config_changed(viewer)
        except Exception as e:
            print(f"[ConfigPanel._save_viewer_settings] error writing config: {e}")

    def _format_system_config(self, system: Dict) -> str:
        """Format system config for display."""
        lines = []
        
        fw_ver = safe_get(system, "fw_version", default={})
        major_minor = safe_get(fw_ver, "major_minor_raw", default=None)
        minor_fix = safe_get(fw_ver, "minor_fix_raw", default=None)
        
        lines.append(f"Firmware: {major_minor}.{minor_fix}" if major_minor else "Firmware: N/A")
        lines.append(f"Temperature Scale: {system.get('temperature_scale', 'N/A')}")
        lines.append(f"Sensor Type: {system.get('sensor_type', 'N/A')}")
        lines.append(f"Modbus Address: {system.get('modbus_address', 'N/A')}")
        lines.append(f"Scan Time: {system.get('scan_time_seconds', 'N/A')}s")
        lines.append(f"System State: {system.get('system_state', 'N/A')}")
        lines.append(f"Alarm Type: {system.get('system_alarm_type', 'N/A')}")
        
        return "\n".join(lines)

    def _format_service_config(self, cfg: Dict) -> str:
        # reuse same formatting logic that was previously in ServicePanel
        lines = []
        lines.append("Connection:")
        serial_port = cfg.get("last_serial_port", "")
        params = cfg.get("last_serial_params", {})
        lines.append(f"  Serial port: {serial_port} {params}")
        tcp_host = cfg.get("last_tcp_host", "")
        tcp_port = cfg.get("last_tcp_port", "")
        lines.append(f"  TCP: {tcp_host}:{tcp_port}")
        
        # polling and zones
        lines.append("")
        lines.append("Polling:")
        lines.append(f"  telemetry_hz: {cfg.get('telemetry_hz')}")
        lines.append(f"  config_hz: {cfg.get('config_hz')}")
        lines.append(f"  rampsoak_hz: {cfg.get('rampsoak_hz')}")
        lines.append(f"  analysis_hz: {cfg.get('analysis_hz')}")
        lines.append(f"  equilibrium_window_s: {cfg.get('equilibrium_window_s')}")
        lines.append(f"  equilibrium_threshold_c: {cfg.get('equilibrium_threshold_c')}")
        
        lines.append("")
        lines.append("Zones:")
        lines.append(f"  mode: {cfg.get('zones_mode')}")
        lines.append(f"  list: {cfg.get('zones_list')}")
        
        lines.append("")
        lines.append(f"  flush_each_line: {cfg.get('flush_each_line')}")
        
        # viewer preferences (display only)
        viewer = cfg.get("viewer", {})
        if viewer:
            lines.append("")
            lines.append("Viewer settings:")
            lines.append(f"  history_hours: {viewer.get('history_hours')}")
            lines.append(f"  line_width: {viewer.get('line_width')}")
            lines.append(f"  pv_color: {viewer.get('pv_color')}")
            lines.append(f"  sp_color: {viewer.get('sp_color')}")
            lines.append(f"  sp_autotune_color: {viewer.get('sp_autotune_color')}")
        return "\n".join(lines)
    
    def _update_zones_config(self, zones: Dict):
        """Update zone configuration tabs."""
        try:
            self._debug_log(f"[_update_zones_config] zones type: {type(zones)}")
            
            # Validate zones is a dict
            if not isinstance(zones, dict):
                self._debug_log(f"[_update_zones_config] zones is not a dict, skipping")
                return
            
            # Save currently active tab to restore after refresh
            current_tab_index = None
            try:
                current_tab_index = self.zones_notebook.index(self.zones_notebook.select())
                self._debug_log(f"[_update_zones_config] Current tab index: {current_tab_index}")
            except Exception:
                pass
            
            # Clear old tabs
            self._debug_log(f"[_update_zones_config] Clearing {len(self.zones_notebook.tabs())} tabs")
            for tab_id in self.zones_notebook.tabs():
                self.zones_notebook.forget(tab_id)
            self.zone_config_labels.clear()
            
            # Get zone IDs safely
            self._debug_log(f"[_update_zones_config] zones.keys(): {list(zones.keys())}")
            
            # Add zone tabs, sort zone IDs numerically if possible
            try:
                zone_ids = sorted(
                    zones.keys(),
                    key=lambda x: int(str(x)) if str(x).isdigit() else 999
                )
                self._debug_log(f"[_update_zones_config] Sorted zone_ids: {zone_ids}")
            except Exception as sort_err:
                print(f"[_update_zones_config] Sort failed: {sort_err}")
                zone_ids = list(zones.keys())
            
            self._debug_log(f"[_update_zones_config] Processing {len(zone_ids)} zones")
            
            for zone_id in zone_ids:
                try:
                    self._debug_log(f"[_update_zones_config] Processing zone_id={zone_id}, type={type(zone_id)}")
                    
                    zone_data = zones[zone_id]
                    self._debug_log(f"[_update_zones_config]   zone_data type: {type(zone_data)}")
                    
                    # Skip if zone_data is not a dict
                    if not isinstance(zone_data, dict):
                        self._debug_log(f"[_update_zones_config]   Skipping zone {zone_id}: not a dict")
                        continue
                    
                    frame = ttk.Frame(self.zones_notebook)
                    self.zones_notebook.add(frame, text=f"Zone {zone_id}")
                    
                    label = ttk.Label(frame, text="", justify=tk.LEFT, font=("Courier", 8))
                    label.pack(padx=10, pady=10, anchor="nw")
                    self.zone_config_labels[zone_id] = label
                    
                    text = self._format_zone_config(zone_data)
                    label.config(text=text)
                    self._debug_log(f"[_update_zones_config]   Zone {zone_id} OK")
                except Exception as zone_err:
                    print(f"[_update_zones_config] Zone {zone_id} failed: {type(zone_err).__name__}: {zone_err}")
                    continue
            
            # Restore the previously active tab if it still exists
            if current_tab_index is not None and current_tab_index < len(self.zones_notebook.tabs()):
                try:
                    self.zones_notebook.select(current_tab_index)
                    self._debug_log(f"[_update_zones_config] Restored tab index {current_tab_index}")
                except Exception:
                    pass
        
        except Exception as e:
            print(f"[_update_zones_config] FATAL ERROR: {type(e).__name__}: {e}")
            print(f"[_update_zones_config] Traceback: {traceback.format_exc()}")
    
    def _format_zone_config(self, zone: Dict) -> str:
        """Format zone config for display."""
        try:
            self._debug_log(f"[_format_zone_config] zone type: {type(zone)}")
            
            if not isinstance(zone, dict):
                return "Invalid zone data"
            
            lines = []
            
            try:
                alarms = zone.get("alarms", {})
                self._debug_log(f"[_format_zone_config] alarms type: {type(alarms)}")
                if isinstance(alarms, dict):
                    lines.append("Alarms:")
                    lines.append(f"  SP High: {alarms.get('sp_high', 'N/A')}")
                    lines.append(f"  SP Low: {alarms.get('sp_low', 'N/A')}")
            except Exception as e:
                print(f"[_format_zone_config] Alarms error: {e}")
                lines.append("Alarms: (error reading)")
            
            try:
                scaling = zone.get("scaling", {})
                self._debug_log(f"[_format_zone_config] scaling type: {type(scaling)}")
                if isinstance(scaling, dict):
                    lines.append("Scaling:")
                    lines.append(f"  Decimal Point: {scaling.get('decimal_point', 'N/A')}")
            except Exception as e:
                print(f"[_format_zone_config] Scaling error: {e}")
                lines.append("Scaling: (error reading)")
            
            return "\n".join(lines) if lines else "No zone config data"
        except Exception as e:
            print(f"[_format_zone_config] FATAL ERROR: {type(e).__name__}: {e}")
            return f"Error: {str(e)}"


class RampSoakPanel(StatePanel):
    """Display ramp/soak configuration."""
    
    def __init__(self, parent, logs_dir: Path, debug: bool = False):
        super().__init__(parent, logs_dir, debug=debug)
        self.create_widgets()
    
    def create_widgets(self):
        header = ttk.Frame(self)
        header.pack(fill=tk.X, padx=10, pady=10)
        
        ttk.Label(header, text="Ramp/Soak Control", font=("Arial", 14, "bold")).pack(side=tk.LEFT)
        
        self.info_label = ttk.Label(self, text="", font=("Arial", 9))
        self.info_label.pack(fill=tk.X, padx=10)
        
        self.content_label = ttk.Label(self, text="", justify=tk.LEFT, font=("Courier", 9))
        self.content_label.pack(fill=tk.BOTH, expand=True, padx=10, pady=10, anchor="nw")
    
    def refresh(self):
        """Update ramp/soak display."""
        try:
            state = get_rampsoak_state(self.logs_dir)
            ts = state.get("ts")
            rampsoak = state.get("rampsoak", {})
            
            self.info_label.config(text=f"Last update: {format_timestamp(ts)}")
            
            if not rampsoak:
                self.content_label.config(text="No ramp/soak data available")
                return
            
            # Simple display: show structure
            text = self._format_rampsoak(rampsoak)
            self.content_label.config(text=text)
        
        except TypeError as e:
            error_msg = f"Type Error - {str(e)}\n{traceback.format_exc()}"
            print(f"[RampSoakPanel.refresh] {error_msg}")
            self.info_label.config(text=f"Error: {str(e)}")
        except Exception as e:
            error_msg = f"Error: {type(e).__name__} - {str(e)}\n{traceback.format_exc()}"
            print(f"[RampSoakPanel.refresh] {error_msg}")
            self.info_label.config(text=f"Error: {str(e)}")


    
    def _format_rampsoak(self, rampsoak: Dict) -> str:
        """Format ramp/soak data for display."""
        try:
            zones = rampsoak.get("zones", {})
            
            if not isinstance(zones, dict):
                print(f"[_format_rampsoak] zones is not a dict: {type(zones)}")
                return "Invalid ramp/soak data"
            
            if not zones:
                return "No zones configured"
            
            lines = []
            for zone_id in sorted(zones.keys(), key=lambda x: int(str(x)) if str(x).isdigit() else 999):
                try:
                    zone_data = zones[zone_id]
                    segments = zone_data.get("segments", []) if isinstance(zone_data, dict) else []
                    lines.append(f"Zone {zone_id}: {len(segments)} segments")
                except Exception as e:
                    print(f"[_format_rampsoak] Zone {zone_id} error: {e}")
                    lines.append(f"Zone {zone_id}: (error reading)")
            
            return "\n".join(lines)
        except Exception as e:
            print(f"[_format_rampsoak] FATAL ERROR: {type(e).__name__}: {e}")
            print(f"[_format_rampsoak] Traceback: {traceback.format_exc()}")
            return f"Error: {str(e)}"
