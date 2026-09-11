"""
display_panels.py

Tkinter display panels for CN616A state visualization.
Each panel is a self-contained component that can be composed into a larger app.
"""

import tkinter as tk
from tkinter import ttk
from pathlib import Path
from typing import Dict, Any, List, Optional
import time
import traceback
import json
import socket
import uuid
import logging
from datetime import datetime

from .state_reader import (
    get_telemetry_state, get_config_state, get_rampsoak_state, get_analysis_state,
    get_service_config_state,
    format_timestamp, safe_get
)

LOGGER = logging.getLogger("cn616a.gui")


def _normalize_zone_names(raw: Any) -> Dict[str, str]:
    names = {str(z): f"Zone {z}" for z in range(1, 7)}
    if isinstance(raw, dict):
        for z in range(1, 7):
            text = raw.get(str(z), raw.get(z, names[str(z)]))
            text = str(text).strip() if text is not None else ""
            names[str(z)] = text or f"Zone {z}"
    elif isinstance(raw, (list, tuple)):
        for idx, text in enumerate(raw[:6], start=1):
            value = str(text).strip() if text is not None else ""
            names[str(idx)] = value or f"Zone {idx}"
    return names


def _load_zone_names_from_logs(logs_dir: Path) -> Dict[str, str]:
    svc_state = get_service_config_state(logs_dir)
    cfg = svc_state.get("config", {}) if isinstance(svc_state, dict) else {}
    return _normalize_zone_names(cfg.get("zone_names", {}))


class StatePanel(tk.Frame):
    """Base class for state display panels."""
    
    def __init__(self, parent, logs_dir: Path, auto_refresh_interval: float = 2.0, debug: bool = False):
        super().__init__(parent)
        self.logs_dir = Path(logs_dir)
        self.auto_refresh_interval = auto_refresh_interval
        self.debug = debug
        self.running = False
        self._after_id: Optional[str] = None
    
    def _debug_log(self, msg: str):
        """Print debug message only if debug mode is enabled."""
        if self.debug:
            print(msg)
    
    def start_auto_refresh(self):
        """Start main-thread refresh loop."""
        if self.running:
            return
        self.running = True
        self._schedule_refresh(initial=True)
    
    def stop_auto_refresh(self):
        """Stop main-thread refresh loop."""
        self.running = False
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                LOGGER.exception("Failed canceling refresh timer")
            finally:
                self._after_id = None
    
    def _schedule_refresh(self, *, initial: bool = False):
        if not self.running:
            return
        delay_ms = 0 if initial else max(100, int(float(self.auto_refresh_interval) * 1000))
        self._after_id = self.after(delay_ms, self._refresh_tick)

    def _refresh_tick(self):
        self._after_id = None
        if not self.running:
            return
        try:
            self.refresh()
        except tk.TclError:
            LOGGER.info("Stopping refresh loop after TclError during shutdown")
            self.running = False
            return
        except Exception:
            LOGGER.exception("Unhandled exception in panel refresh loop")
        self._schedule_refresh()
    
    def refresh(self):
        """Override in subclass to update display."""
        pass


class TelemetryPanel(StatePanel):
    """Display telemetry: PV, setpoint, output per zone."""
    
    def __init__(self, parent, logs_dir: Path, debug: bool = False):
        super().__init__(parent, logs_dir, debug=debug)
        self.create_widgets()
        self.zone_numeric_labels = {}
        self.zone_detail_labels = {}
        self.zone_frames = {}
    
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
        for col in range(3):
            self.zones_container.columnconfigure(col, weight=1)
        
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    
    def _format_numeric_telemetry(self, zone_data: Dict) -> str:
        """Format top-of-card numeric telemetry summary."""
        pv = safe_get(zone_data, "pv_c", default="N/A")
        sp = safe_get(zone_data, "sp_abs", default="N/A")
        out = safe_get(zone_data, "out_pct", default="N/A")

        if isinstance(pv, (int, float)):
            pv = f"{float(pv):.2f} C"
        if isinstance(sp, (int, float)):
            sp = f"{float(sp):.2f} C"
        if isinstance(out, (int, float)):
            out = f"{float(out):.1f} %"

        return f"PV: {pv}    SP: {sp}\nOutput: {out}"

    def _format_telemetry_zone_details(self, zone_data: Dict, analysis_data: Dict, zone_config: Dict) -> str:
        """Format non-numeric telemetry/config/analysis details."""
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

            if equilibrium is True:
                equilibrium_txt = "Yes"
            elif equilibrium is False:
                equilibrium_txt = "No"
            else:
                equilibrium_txt = "N/A"
            
            status_icon = "✓" if equilibrium is True else "✗" if equilibrium is False else "?"
            lines.append(f"  {status_icon} Equilibrium? {equilibrium_txt}  |  MAE (|e|): {avg_error}  |  Threshold: {threshold}")
            
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
            zone_names = _load_zone_names_from_logs(self.logs_dir)
            
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
                for label in self.zone_numeric_labels.values():
                    label.config(text="PV: N/A    SP: N/A\nOutput: N/A")
                for label in self.zone_detail_labels.values():
                    label.config(text="No data available")
                return
            
            zones_data = telemetry.get("zones", {})
            
            # Clear old labels if needed
            if set(self.zone_detail_labels.keys()) != set(zones_data.keys()):
                for widget in self.zones_container.winfo_children():
                    widget.destroy()
                self.zone_numeric_labels.clear()
                self.zone_detail_labels.clear()
                self.zone_frames.clear()
            
            # Update or create zone displays
            sorted_zone_ids = sorted(zones_data.keys(), key=lambda x: int(x) if x.isdigit() else 999)
            for idx, zone_id in enumerate(sorted_zone_ids):
                zone_display = zone_names.get(str(zone_id), f"Zone {zone_id}")
                if zone_id not in self.zone_detail_labels:
                    zone_frame = ttk.LabelFrame(self.zones_container, text=zone_display)
                    row = idx // 3
                    col = idx % 3
                    zone_frame.grid(row=row, column=col, sticky="nsew", padx=6, pady=6)

                    numeric_label = tk.Label(
                        zone_frame,
                        text="",
                        justify=tk.LEFT,
                        anchor="w",
                        fg="red",
                        font=("Arial", 12, "bold"),
                    )
                    numeric_label.pack(fill=tk.X, padx=10, pady=(6, 2))

                    details_label = ttk.Label(zone_frame, text="", justify=tk.LEFT, font=("Courier", 8))
                    details_label.pack(padx=10, pady=(2, 6), anchor="w")

                    self.zone_numeric_labels[zone_id] = numeric_label
                    self.zone_detail_labels[zone_id] = details_label
                    self.zone_frames[zone_id] = zone_frame
                else:
                    self.zone_frames[zone_id].config(text=zone_display)
                
                # Get telemetry, analysis, and config data for this zone
                zone_data = zones_data[zone_id]
                zone_analysis = analysis.get(zone_id, {})
                zone_config = zones_config.get(zone_id, {})
                
                numeric_text = self._format_numeric_telemetry(zone_data)
                details_text = self._format_telemetry_zone_details(zone_data, zone_analysis, zone_config)
                self.zone_numeric_labels[zone_id].config(text=numeric_text)
                self.zone_detail_labels[zone_id].config(text=details_text)
        
        except TypeError as e:
            error_msg = f"Type Error - {str(e)}\n{traceback.format_exc()}"
            print(f"[TelemetryPanel.refresh] {error_msg}")
            LOGGER.exception("TelemetryPanel.refresh type error")
            self.info_label.config(text=f"Error: {str(e)}")
        except Exception as e:
            error_msg = f"Error: {type(e).__name__} - {str(e)}\n{traceback.format_exc()}"
            print(f"[TelemetryPanel.refresh] {error_msg}")
            LOGGER.exception("TelemetryPanel.refresh failed")
            self.info_label.config(text=f"Error: {str(e)}")


class ConfigPanel(StatePanel):
    """Display configuration: system settings and zone alarms."""
    
    def __init__(self, parent, logs_dir: Path, debug: bool = False, on_viewer_config_changed=None, on_service_config_changed=None):
        super().__init__(parent, logs_dir, debug=debug)
        self.on_viewer_config_changed = on_viewer_config_changed
        self.on_service_config_changed = on_service_config_changed
        self._service_cfg_cache = {}
        self._form_updating = False
        self._form_dirty = False
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
        self.service_status_label = ttk.Label(self.service_frame, text="", font=("Arial", 9), foreground="gray")
        self.service_status_label.pack(fill=tk.X, padx=10, pady=(0, 6))

        self.service_actions_frame = ttk.Frame(self.service_frame)
        self.service_actions_frame.pack(fill=tk.X, padx=10, pady=(0, 6))
        ttk.Button(self.service_actions_frame, text="Apply Service Settings", command=self._on_apply_settings_clicked).pack(side=tk.RIGHT)

        # Scrollable form container to keep all controls accessible on smaller windows
        service_form_container = ttk.Frame(self.service_frame)
        service_form_container.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        self.service_form_canvas = tk.Canvas(service_form_container, highlightthickness=0)
        self.service_form_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.service_form_scrollbar = ttk.Scrollbar(
            service_form_container,
            orient=tk.VERTICAL,
            command=self.service_form_canvas.yview,
        )
        self.service_form_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.service_form_canvas.configure(yscrollcommand=self.service_form_scrollbar.set)

        self.service_form_frame = ttk.Frame(self.service_form_canvas)
        self._service_form_window = self.service_form_canvas.create_window(
            (0, 0), window=self.service_form_frame, anchor="nw"
        )

        self.service_form_frame.bind(
            "<Configure>",
            lambda _event: self.service_form_canvas.configure(
                scrollregion=self.service_form_canvas.bbox("all")
            ),
        )
        self.service_form_canvas.bind(
            "<Configure>",
            lambda event: self.service_form_canvas.itemconfigure(
                self._service_form_window, width=event.width
            ),
        )

        self.service_form_frame.columnconfigure(0, weight=1)
        self.service_form_frame.columnconfigure(1, weight=1)

        # ---- Connection section ----
        connection_box = ttk.LabelFrame(self.service_form_frame, text="Connection")
        connection_box.grid(row=0, column=0, columnspan=2, sticky="ew", padx=(0, 0), pady=(0, 8))

        self.conn_serial_port = ttk.Entry(connection_box, width=18)
        self.conn_baudrate = ttk.Entry(connection_box, width=18)
        self.conn_parity = ttk.Combobox(connection_box, width=15, state="readonly", values=["N", "E", "O"])
        self.conn_stopbits = ttk.Combobox(connection_box, width=15, state="readonly", values=["1", "2"])
        self.conn_bytesize = ttk.Combobox(connection_box, width=15, state="readonly", values=["7", "8"])
        self.conn_timeout = ttk.Entry(connection_box, width=18)
        self.conn_tcp_host = ttk.Entry(connection_box, width=18)
        self.conn_tcp_port = ttk.Entry(connection_box, width=18)

        connection_rows = [
            ("Serial port", self.conn_serial_port),
            ("Baudrate", self.conn_baudrate),
            ("Parity", self.conn_parity),
            ("Stop bits", self.conn_stopbits),
            ("Byte size", self.conn_bytesize),
            ("Timeout (s)", self.conn_timeout),
            ("Service host", self.conn_tcp_host),
            ("Service TCP port", self.conn_tcp_port),
        ]
        for row, (label, widget) in enumerate(connection_rows):
            ttk.Label(connection_box, text=label + ":").grid(row=row, column=0, sticky="e", padx=(8, 8), pady=3)
            widget.grid(row=row, column=1, sticky="w", padx=(0, 8), pady=3)
            if isinstance(widget, ttk.Entry):
                widget.bind("<KeyRelease>", self._on_form_edited)
                widget.bind("<FocusOut>", self._on_form_edited)
            else:
                widget.bind("<<ComboboxSelected>>", self._on_form_edited)

        conn_btn_row = len(connection_rows)
        ttk.Button(connection_box, text="Connect", command=self._on_connect_clicked).grid(row=conn_btn_row, column=0, pady=(6, 8), padx=8, sticky="e")
        ttk.Button(connection_box, text="Disconnect", command=self._on_disconnect_clicked).grid(row=conn_btn_row, column=1, pady=(6, 8), padx=(0, 8), sticky="w")
        ttk.Button(connection_box, text="Refresh Connection", command=self._on_refresh_connection_clicked).grid(row=conn_btn_row, column=2, pady=(6, 8), padx=(0, 8), sticky="w")

        # ---- Polling section ----
        polling_box = ttk.LabelFrame(self.service_form_frame, text="Polling")
        polling_box.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(0, 8))

        self.polling_telemetry_hz = ttk.Entry(polling_box, width=18)
        self.polling_config_hz = ttk.Entry(polling_box, width=18)
        self.polling_rampsoak_hz = ttk.Entry(polling_box, width=18)
        self.polling_analysis_hz = ttk.Entry(polling_box, width=18)
        self.polling_gui_refresh_hz = ttk.Entry(polling_box, width=18)
        self.polling_eq_window = ttk.Entry(polling_box, width=18)
        self.polling_eq_threshold = ttk.Entry(polling_box, width=18)

        polling_rows = [
            ("Telemetry (Hz)", self.polling_telemetry_hz),
            ("Config (Hz)", self.polling_config_hz),
            ("Ramp/Soak (Hz)", self.polling_rampsoak_hz),
            ("Analysis (Hz)", self.polling_analysis_hz),
            ("GUI refresh (Hz)", self.polling_gui_refresh_hz),
            ("Equilibrium window (s)", self.polling_eq_window),
            ("Equilibrium threshold (°C)", self.polling_eq_threshold),
        ]
        for row, (label, widget) in enumerate(polling_rows):
            ttk.Label(polling_box, text=label + ":").grid(row=row, column=0, sticky="e", padx=(8, 8), pady=3)
            widget.grid(row=row, column=1, sticky="w", padx=(0, 8), pady=3)
            widget.bind("<KeyRelease>", self._on_form_edited)
            widget.bind("<FocusOut>", self._on_form_edited)

        self.flush_each_line_var = tk.BooleanVar(value=False)
        flush_check = ttk.Checkbutton(
            polling_box,
            text="Flush each log line",
            variable=self.flush_each_line_var,
            command=self._on_form_edited,
        )
        flush_check.grid(row=len(polling_rows), column=1, sticky="w", padx=(0, 8), pady=(3, 8))

        # ---- Log rotation section ----
        rotation_box = ttk.LabelFrame(self.service_form_frame, text="Log Rotation")
        rotation_box.grid(row=2, column=0, sticky="nsew", padx=(0, 6), pady=(0, 8))
        rotation_box.columnconfigure(1, weight=1)
        rotation_box.columnconfigure(3, weight=1)

        self.max_service_config_log_bytes = ttk.Entry(rotation_box, width=18)
        self.max_service_config_log_files = ttk.Entry(rotation_box, width=18)
        self.max_telemetry_log_bytes = ttk.Entry(rotation_box, width=18)
        self.max_telemetry_log_files = ttk.Entry(rotation_box, width=18)
        self.max_config_log_bytes = ttk.Entry(rotation_box, width=18)
        self.max_config_log_files = ttk.Entry(rotation_box, width=18)
        self.max_rampsoak_log_bytes = ttk.Entry(rotation_box, width=18)
        self.max_rampsoak_log_files = ttk.Entry(rotation_box, width=18)
        self.max_analysis_log_bytes = ttk.Entry(rotation_box, width=18)
        self.max_analysis_log_files = ttk.Entry(rotation_box, width=18)

        rotation_rows = [
            ("Service config max bytes", self.max_service_config_log_bytes, "Service config max files", self.max_service_config_log_files),
            ("Telemetry max bytes", self.max_telemetry_log_bytes, "Telemetry max files", self.max_telemetry_log_files),
            ("Config max bytes", self.max_config_log_bytes, "Config max files", self.max_config_log_files),
            ("Ramp/Soak max bytes", self.max_rampsoak_log_bytes, "Ramp/Soak max files", self.max_rampsoak_log_files),
            ("Analysis max bytes", self.max_analysis_log_bytes, "Analysis max files", self.max_analysis_log_files),
        ]
        for row, (label_a, widget_a, label_b, widget_b) in enumerate(rotation_rows):
            ttk.Label(rotation_box, text=label_a + ":").grid(row=row, column=0, sticky="e", padx=(8, 8), pady=3)
            widget_a.grid(row=row, column=1, sticky="ew", padx=(0, 8), pady=3)
            ttk.Label(rotation_box, text=label_b + ":").grid(row=row, column=2, sticky="e", padx=(8, 8), pady=3)
            widget_b.grid(row=row, column=3, sticky="ew", padx=(0, 8), pady=3)
            widget_a.bind("<KeyRelease>", self._on_form_edited)
            widget_a.bind("<FocusOut>", self._on_form_edited)
            widget_b.bind("<KeyRelease>", self._on_form_edited)
            widget_b.bind("<FocusOut>", self._on_form_edited)

        # ---- Zones section ----
        zones_box = ttk.LabelFrame(self.service_form_frame, text="Zones")
        zones_box.grid(row=3, column=0, sticky="nsew", padx=(0, 6), pady=(0, 8))
        zones_box.columnconfigure(1, weight=1)
        zones_box.columnconfigure(3, weight=1)

        self.zones_mode_var = tk.StringVar(value="auto")
        self.zones_list_entry = ttk.Entry(zones_box, width=20)
        self.zone_name_entries = {str(z): ttk.Entry(zones_box, width=20) for z in range(1, 7)}

        ttk.Label(zones_box, text="Mode:").grid(row=0, column=0, sticky="e", padx=(8, 8), pady=3)
        mode_frame = ttk.Frame(zones_box)
        mode_frame.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=3)
        ttk.Radiobutton(mode_frame, text="Auto", variable=self.zones_mode_var, value="auto", command=self._on_form_edited).pack(side=tk.LEFT)
        ttk.Radiobutton(mode_frame, text="List", variable=self.zones_mode_var, value="list", command=self._on_form_edited).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(zones_box, text="Zone list:").grid(row=1, column=0, sticky="e", padx=(8, 8), pady=3)
        self.zones_list_entry.grid(row=1, column=1, columnspan=3, sticky="ew", padx=(0, 8), pady=3)
        self.zones_list_entry.bind("<KeyRelease>", self._on_form_edited)
        self.zones_list_entry.bind("<FocusOut>", self._on_form_edited)

        for idx in range(1, 7):
            row = 2 + ((idx - 1) % 3)
            col_offset = 0 if idx <= 3 else 2
            entry = self.zone_name_entries[str(idx)]
            ttk.Label(zones_box, text=f"Zone {idx} name:").grid(row=row, column=col_offset, sticky="e", padx=(8, 8), pady=3)
            entry.grid(row=row, column=col_offset + 1, sticky="ew", padx=(0, 8), pady=3)
            entry.bind("<KeyRelease>", self._on_form_edited)
            entry.bind("<FocusOut>", self._on_form_edited)

        # ---- Viewer section ----
        viewer_box = ttk.LabelFrame(self.service_form_frame, text="Viewer Settings")
        viewer_box.grid(row=1, column=1, rowspan=2, sticky="nsew", padx=(6, 0), pady=(0, 8))

        self.viewer_history_hours = ttk.Entry(viewer_box, width=18)
        self.viewer_line_width = ttk.Entry(viewer_box, width=18)
        color_options = ["black", "blue", "red", "green", "orange", "purple", "gray"]
        self.viewer_pv_color = ttk.Combobox(viewer_box, width=15, state="readonly", values=color_options)
        self.viewer_sp_color = ttk.Combobox(viewer_box, width=15, state="readonly", values=color_options)
        self.viewer_sp_autotune_color = ttk.Combobox(viewer_box, width=15, state="readonly", values=color_options)
        self.viewer_show_sp_abs_var = tk.BooleanVar(value=True)
        self.viewer_show_sp_autotune_var = tk.BooleanVar(value=True)
        self.viewer_show_mae_var = tk.BooleanVar(value=True)

        viewer_rows = [
            ("History (s)", self.viewer_history_hours),
            ("Line width", self.viewer_line_width),
            ("PV color", self.viewer_pv_color),
            ("SP color", self.viewer_sp_color),
            ("SP autotune color", self.viewer_sp_autotune_color),
        ]
        for row, (label, widget) in enumerate(viewer_rows):
            ttk.Label(viewer_box, text=label + ":").grid(row=row, column=0, sticky="e", padx=(8, 8), pady=3)
            widget.grid(row=row, column=1, sticky="w", padx=(0, 8), pady=3)
            if isinstance(widget, ttk.Entry):
                widget.bind("<KeyRelease>", self._on_form_edited)
                widget.bind("<FocusOut>", self._on_form_edited)
            else:
                widget.bind("<<ComboboxSelected>>", self._on_form_edited)

        check_row = len(viewer_rows)
        ttk.Checkbutton(
            viewer_box,
            text="Show SP Abs",
            variable=self.viewer_show_sp_abs_var,
            command=self._on_form_edited,
        ).grid(row=check_row, column=1, sticky="w", padx=(0, 8), pady=(4, 2))
        ttk.Checkbutton(
            viewer_box,
            text="Show SP Autotune",
            variable=self.viewer_show_sp_autotune_var,
            command=self._on_form_edited,
        ).grid(row=check_row + 1, column=1, sticky="w", padx=(0, 8), pady=2)
        ttk.Checkbutton(
            viewer_box,
            text="Show MAE",
            variable=self.viewer_show_mae_var,
            command=self._on_form_edited,
        ).grid(row=check_row + 2, column=1, sticky="w", padx=(0, 8), pady=(2, 6))

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
            
            # Service configuration section (separate state file)
            svc_state = get_service_config_state(self.logs_dir)
            svc_ts = svc_state.get("ts")
            svc_cfg = svc_state.get("config", {})
            zone_names = _normalize_zone_names(svc_cfg.get("zone_names", {})) if isinstance(svc_cfg, dict) else _normalize_zone_names({})
            self._update_zones_config(zones, zone_names)
            self.service_info_label.config(text=f"Last update: {format_timestamp(svc_ts)}")
            if not svc_cfg:
                self.service_status_label.config(text="No service config data available", foreground="gray")
            else:
                self._service_cfg_cache = dict(svc_cfg)
                if not self._form_dirty:
                    self._populate_service_form(svc_cfg)
        
        except TypeError as e:
            error_msg = f"Type Error - {str(e)}\n{traceback.format_exc()}"
            print(f"[ConfigPanel.refresh] {error_msg}")
            LOGGER.exception("ConfigPanel.refresh type error")
            self.info_label.config(text=f"Error: {str(e)}")
        except Exception as e:
            error_msg = f"Error: {type(e).__name__} - {str(e)}\n{traceback.format_exc()}"
            print(f"[ConfigPanel.refresh] {error_msg}")
            LOGGER.exception("ConfigPanel.refresh failed")
            self.info_label.config(text=f"Error: {str(e)}")
    
    def _set_entry_text(self, entry: ttk.Entry, value: Any):
        focused_widget = self._safe_focus_get()
        if focused_widget is entry:
            return
        entry.delete(0, tk.END)
        entry.insert(0, "" if value is None else str(value))

    def _set_combo_value(self, combo: ttk.Combobox, value: Any):
        if self._safe_focus_get() is combo:
            return
        if value is None:
            return
        combo.set(str(value))

    def _safe_focus_get(self):
        try:
            return self.focus_get()
        except Exception:
            return None

    def _populate_service_form(self, cfg: Dict[str, Any]):
        self._form_updating = True
        try:
            params = cfg.get("last_serial_params", {}) if isinstance(cfg.get("last_serial_params", {}), dict) else {}

            self._set_entry_text(self.conn_serial_port, cfg.get("last_serial_port", ""))
            self._set_entry_text(self.conn_baudrate, params.get("baudrate", 115200))
            self._set_combo_value(self.conn_parity, params.get("parity", "N"))
            self._set_combo_value(self.conn_stopbits, params.get("stopbits", 1))
            self._set_combo_value(self.conn_bytesize, params.get("bytesize", 8))
            self._set_entry_text(self.conn_timeout, params.get("timeout", 1.0))
            self._set_entry_text(self.conn_tcp_host, cfg.get("last_tcp_host", "127.0.0.1"))
            self._set_entry_text(self.conn_tcp_port, cfg.get("last_tcp_port", 8765))

            self._set_entry_text(self.polling_telemetry_hz, cfg.get("telemetry_hz", 2.0))
            self._set_entry_text(self.polling_config_hz, cfg.get("config_hz", 0.2))
            self._set_entry_text(self.polling_rampsoak_hz, cfg.get("rampsoak_hz", 0.0))
            self._set_entry_text(self.polling_analysis_hz, cfg.get("analysis_hz", 1.0))
            self._set_entry_text(self.polling_gui_refresh_hz, cfg.get("gui_refresh_hz", 2.0))
            self._set_entry_text(self.polling_eq_window, cfg.get("equilibrium_window_s", 30.0))
            self._set_entry_text(self.polling_eq_threshold, cfg.get("equilibrium_threshold_c", 0.25))
            self.flush_each_line_var.set(bool(cfg.get("flush_each_line", False)))

            self._set_entry_text(self.max_service_config_log_bytes, cfg.get("max_service_config_log_bytes", 2_000_000))
            self._set_entry_text(self.max_service_config_log_files, cfg.get("max_service_config_log_files", 10))
            self._set_entry_text(self.max_telemetry_log_bytes, cfg.get("max_telemetry_log_bytes", 10_000_000))
            self._set_entry_text(self.max_telemetry_log_files, cfg.get("max_telemetry_log_files", 10))
            self._set_entry_text(self.max_config_log_bytes, cfg.get("max_config_log_bytes", 5_000_000))
            self._set_entry_text(self.max_config_log_files, cfg.get("max_config_log_files", 10))
            self._set_entry_text(self.max_rampsoak_log_bytes, cfg.get("max_rampsoak_log_bytes", 5_000_000))
            self._set_entry_text(self.max_rampsoak_log_files, cfg.get("max_rampsoak_log_files", 10))
            self._set_entry_text(self.max_analysis_log_bytes, cfg.get("max_analysis_log_bytes", 10_000_000))
            self._set_entry_text(self.max_analysis_log_files, cfg.get("max_analysis_log_files", 10))

            zones_mode = str(cfg.get("zones_mode", "auto") or "auto")
            self.zones_mode_var.set("list" if zones_mode == "list" else "auto")
            zones_list = cfg.get("zones_list", [1, 2, 3, 4, 5, 6])
            if isinstance(zones_list, (list, tuple)):
                zones_text = ",".join(str(z) for z in zones_list)
            else:
                zones_text = ""
            self._set_entry_text(self.zones_list_entry, zones_text)

            zone_names = _normalize_zone_names(cfg.get("zone_names", {}))
            for z in range(1, 7):
                self._set_entry_text(self.zone_name_entries[str(z)], zone_names[str(z)])

            viewer = self._extract_viewer_from_cfg(cfg)
            history_hours = float(viewer.get("history_hours", 1.0) or 1.0)
            self._set_entry_text(self.viewer_history_hours, history_hours * 3600.0)
            self._set_entry_text(self.viewer_line_width, viewer.get("line_width", 2.5))
            self._set_combo_value(self.viewer_pv_color, viewer.get("pv_color", "blue"))
            self._set_combo_value(self.viewer_sp_color, viewer.get("sp_color", "red"))
            self._set_combo_value(self.viewer_sp_autotune_color, viewer.get("sp_autotune_color", "purple"))
            self.viewer_show_sp_abs_var.set(bool(viewer.get("show_sp_abs", True)))
            self.viewer_show_sp_autotune_var.set(bool(viewer.get("show_sp_autotune", True)))
            self.viewer_show_mae_var.set(bool(viewer.get("show_mae", True)))
            self._form_dirty = False
        finally:
            self._form_updating = False

    def _extract_viewer_from_cfg(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Read viewer settings from nested viewer object or legacy flat keys."""
        viewer = dict(cfg.get("viewer", {}) or {})
        if "history_hours" not in viewer and cfg.get("viewer_history_hours") is not None:
            viewer["history_hours"] = cfg.get("viewer_history_hours")
        if "line_width" not in viewer and cfg.get("viewer_line_width") is not None:
            viewer["line_width"] = cfg.get("viewer_line_width")
        if "pv_color" not in viewer and cfg.get("viewer_pv_color") is not None:
            viewer["pv_color"] = cfg.get("viewer_pv_color")
        if "sp_color" not in viewer and cfg.get("viewer_sp_color") is not None:
            viewer["sp_color"] = cfg.get("viewer_sp_color")
        if "sp_autotune_color" not in viewer and cfg.get("viewer_sp_autotune_color") is not None:
            viewer["sp_autotune_color"] = cfg.get("viewer_sp_autotune_color")
        if "show_sp_abs" not in viewer and cfg.get("viewer_show_sp_abs") is not None:
            viewer["show_sp_abs"] = cfg.get("viewer_show_sp_abs")
        if "show_sp_autotune" not in viewer and cfg.get("viewer_show_sp_autotune") is not None:
            viewer["show_sp_autotune"] = cfg.get("viewer_show_sp_autotune")
        if "show_mae" not in viewer and cfg.get("viewer_show_mae") is not None:
            viewer["show_mae"] = cfg.get("viewer_show_mae")
        return viewer

    def _safe_float(self, value: str) -> Optional[float]:
        text = str(value).strip()
        if text == "":
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _safe_int(self, value: str) -> Optional[int]:
        text = str(value).strip()
        if text == "":
            return None
        try:
            return int(text)
        except ValueError:
            return None

    def _get_service_endpoint(self) -> tuple[str, int]:
        host = self.conn_tcp_host.get().strip() or "127.0.0.1"
        port = self._safe_int(self.conn_tcp_port.get())
        if port is None:
            port = int(self._service_cfg_cache.get("last_tcp_port", 8765) or 8765)
        return host, port

    def _send_service_cmd(self, op: str, **fields) -> Dict[str, Any]:
        host, port = self._get_service_endpoint()
        msg = {"id": uuid.uuid4().hex[:8], "op": op}
        msg.update(fields)
        data = (json.dumps(msg) + "\n").encode("utf-8")
        with socket.create_connection((host, port), timeout=2.0) as s:
            s.sendall(data)
            s.settimeout(2.0)
            resp = s.recv(65536).decode("utf-8", errors="ignore").strip()
        return json.loads(resp) if resp else {"ok": False, "error": "empty response"}

    def _set_service_status(self, message: str, ok: bool = True):
        self.service_status_label.config(text=message, foreground=("green" if ok else "red"))

    def _on_form_edited(self, _event=None):
        if self._form_updating:
            return
        self._form_dirty = True
        self.service_status_label.config(text="Unsaved changes", foreground="orange")

    def _apply_service_patch(self, patch: Dict[str, Any], *, notify_viewer: bool = False):
        if self._form_updating:
            return
        if not patch:
            return
        try:
            resp = self._send_service_cmd("set_service_config", patch=patch)
            if not resp.get("ok"):
                self._set_service_status(f"Apply failed: {resp.get('error', 'unknown error')}", ok=False)
                return

            cfg = resp.get("service_config", {})
            if isinstance(cfg, dict):
                self._service_cfg_cache = dict(cfg)
                self._populate_service_form(cfg)

            self._set_service_status("Settings applied", ok=True)
            self._form_dirty = False

            if notify_viewer and self.on_viewer_config_changed:
                viewer = self._extract_viewer_from_cfg(self._service_cfg_cache)
                self.on_viewer_config_changed(viewer)
            if self.on_service_config_changed and isinstance(self._service_cfg_cache, dict):
                self.on_service_config_changed(self._service_cfg_cache)
        except Exception as e:
            self._set_service_status(f"Service unreachable: {e}", ok=False)

    def _build_polling_patch(self) -> Dict[str, Any]:
        patch: Dict[str, Any] = {}
        pairs = [
            ("telemetry_hz", self.polling_telemetry_hz.get()),
            ("config_hz", self.polling_config_hz.get()),
            ("rampsoak_hz", self.polling_rampsoak_hz.get()),
            ("analysis_hz", self.polling_analysis_hz.get()),
            ("gui_refresh_hz", self.polling_gui_refresh_hz.get()),
            ("equilibrium_window_s", self.polling_eq_window.get()),
            ("equilibrium_threshold_c", self.polling_eq_threshold.get()),
        ]
        for key, value in pairs:
            num = self._safe_float(value)
            if num is not None:
                patch[key] = num
        patch["flush_each_line"] = bool(self.flush_each_line_var.get())
        return patch

    def _build_log_rotation_patch(self) -> Dict[str, Any]:
        patch: Dict[str, Any] = {}
        pairs = [
            ("max_service_config_log_bytes", self.max_service_config_log_bytes.get()),
            ("max_service_config_log_files", self.max_service_config_log_files.get()),
            ("max_telemetry_log_bytes", self.max_telemetry_log_bytes.get()),
            ("max_telemetry_log_files", self.max_telemetry_log_files.get()),
            ("max_config_log_bytes", self.max_config_log_bytes.get()),
            ("max_config_log_files", self.max_config_log_files.get()),
            ("max_rampsoak_log_bytes", self.max_rampsoak_log_bytes.get()),
            ("max_rampsoak_log_files", self.max_rampsoak_log_files.get()),
            ("max_analysis_log_bytes", self.max_analysis_log_bytes.get()),
            ("max_analysis_log_files", self.max_analysis_log_files.get()),
        ]
        for key, value in pairs:
            if key.endswith("_files"):
                num = self._safe_int(value)
                if num is not None:
                    patch[key] = max(1, num)
            else:
                num = self._safe_int(value)
                if num is not None:
                    patch[key] = max(1, num)
        return patch

    def _build_zones_patch(self) -> Dict[str, Any]:
        zone_names = {}
        for z in range(1, 7):
            raw = self.zone_name_entries[str(z)].get().strip()
            zone_names[str(z)] = raw or f"Zone {z}"

        mode = self.zones_mode_var.get().strip().lower()
        if mode == "list":
            zones = []
            for token in self.zones_list_entry.get().split(","):
                token = token.strip()
                if not token:
                    continue
                if token.isdigit():
                    zones.append(int(token))
            zones = [z for z in zones if 1 <= z <= 6]
            if zones:
                return {"zones_mode": "list", "zones_list": zones, "zone_names": zone_names}
            return {"zones_mode": "auto", "zone_names": zone_names}
        return {"zones_mode": "auto", "zone_names": zone_names}

    def _build_viewer_patch(self) -> Dict[str, Any]:
        history_seconds = self._safe_float(self.viewer_history_hours.get())
        if history_seconds is None or history_seconds <= 0:
            history_seconds = 3600.0

        viewer = {
            "history_hours": history_seconds / 3600.0,
            "line_width": self._safe_float(self.viewer_line_width.get()) or 2.5,
            "pv_color": self.viewer_pv_color.get().strip() or "blue",
            "sp_color": self.viewer_sp_color.get().strip() or "red",
            "sp_autotune_color": self.viewer_sp_autotune_color.get().strip() or "purple",
            "show_sp_abs": bool(self.viewer_show_sp_abs_var.get()),
            "show_sp_autotune": bool(self.viewer_show_sp_autotune_var.get()),
            "show_mae": bool(self.viewer_show_mae_var.get()),
        }
        return {
            "viewer": viewer,
            "viewer_history_hours": viewer["history_hours"],
            "viewer_line_width": viewer["line_width"],
            "viewer_pv_color": viewer["pv_color"],
            "viewer_sp_color": viewer["sp_color"],
            "viewer_sp_autotune_color": viewer["sp_autotune_color"],
            "viewer_show_sp_abs": viewer["show_sp_abs"],
            "viewer_show_sp_autotune": viewer["show_sp_autotune"],
            "viewer_show_mae": viewer["show_mae"],
        }

    def _build_connection_patch(self) -> Dict[str, Any]:
        params = {}
        baud = self._safe_int(self.conn_baudrate.get())
        if baud is not None:
            params["baudrate"] = baud
        parity = self.conn_parity.get().strip()
        if parity:
            params["parity"] = parity
        stopbits = self._safe_int(self.conn_stopbits.get())
        if stopbits is not None:
            params["stopbits"] = stopbits
        bytesize = self._safe_int(self.conn_bytesize.get())
        if bytesize is not None:
            params["bytesize"] = bytesize
        timeout_s = self._safe_float(self.conn_timeout.get())
        if timeout_s is not None:
            params["timeout"] = timeout_s

        patch = {
            "last_serial_port": self.conn_serial_port.get().strip(),
            "last_serial_params": params,
            "last_tcp_host": self.conn_tcp_host.get().strip() or "127.0.0.1",
            "last_tcp_port": self._safe_int(self.conn_tcp_port.get()) or 8765,
        }
        return patch

    def _on_apply_settings_clicked(self):
        combined_patch = {}
        combined_patch.update(self._build_polling_patch())
        combined_patch.update(self._build_log_rotation_patch())
        combined_patch.update(self._build_zones_patch())
        combined_patch.update(self._build_viewer_patch())
        if not combined_patch:
            self._set_service_status("No settings to apply", ok=False)
            return
        self._apply_service_patch(combined_patch, notify_viewer=True)

    def _on_connect_clicked(self):
        connection_patch = self._build_connection_patch()
        self._apply_service_patch(connection_patch)
        try:
            resp = self._send_service_cmd("connect_serial")
            if resp.get("ok"):
                self._set_service_status("Connected", ok=True)
            else:
                self._set_service_status(f"Connect failed: {resp.get('error', 'unknown error')}", ok=False)
        except Exception as e:
            self._set_service_status(f"Connect failed: {e}", ok=False)

    def _on_disconnect_clicked(self):
        try:
            resp = self._send_service_cmd("disconnect_serial")
            if resp.get("ok"):
                self._set_service_status("Disconnected", ok=True)
            else:
                self._set_service_status(f"Disconnect failed: {resp.get('error', 'unknown error')}", ok=False)
        except Exception as e:
            self._set_service_status(f"Disconnect failed: {e}", ok=False)

    def _on_refresh_connection_clicked(self):
        connection_patch = self._build_connection_patch()
        self._apply_service_patch(connection_patch)
        try:
            resp = self._send_service_cmd("refresh_connection")
            if resp.get("ok"):
                self._set_service_status("Connection refreshed", ok=True)
            else:
                self._set_service_status(f"Refresh failed: {resp.get('error', 'unknown error')}", ok=False)
        except Exception as e:
            self._set_service_status(f"Refresh failed: {e}", ok=False)

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
        # retained for compatibility if needed elsewhere
        return json.dumps(cfg, indent=2)
    
    def _update_zones_config(self, zones: Dict, zone_names: Optional[Dict[str, str]] = None):
        """Update zone configuration tabs."""
        try:
            self._debug_log(f"[_update_zones_config] zones type: {type(zones)}")
            zone_names = zone_names or {}
            
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
                    zone_display = zone_names.get(str(zone_id), f"Zone {zone_id}")
                    self.zones_notebook.add(frame, text=zone_display)
                    
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
    """Ramp/Soak profile viewer and editor (CN616A manual Functions 74-79).

    Lets the user pick a zone, choose Standard vs Ramp/Soak control mode,
    set how many of the 20 segments are active, start/stop the profile, and
    edit each segment's Setpoint / Slope / Time. The segment table and the
    "Number of Segments" / control-mode controls are write-only from the
    user's perspective: refresh() only ever repopulates them when there are
    no unsaved local edits, so a periodic auto-refresh can never clobber an
    in-progress edit (the same class of bug fixed in command_panel.py).
    """

    NUM_SEGMENT_ROWS = 20

    _CONTROL_MODE_LABELS = {
        "STANDARD_CONTROL": "Standard (no Ramp/Soak)",
        "RAMP_SOAK_1_STOP": "Ramp/Soak - Stop at end",
        "RAMP_SOAK_2_HOLD": "Ramp/Soak - Hold at end",
    }
    _CONTROL_MODE_WIRE = {v: k for k, v in _CONTROL_MODE_LABELS.items()}

    def __init__(self, parent, logs_dir: Path, debug: bool = False):
        super().__init__(parent, logs_dir, debug=debug)
        self._latest_rampsoak_zones: Dict[str, Any] = {}
        self._zone_token_to_id: Dict[str, str] = {}
        self._segments_dirty = False
        self._num_segments_dirty = False
        self._control_mode_dirty = False
        self.create_widgets()

    def create_widgets(self):
        header = ttk.Frame(self)
        header.pack(fill=tk.X, padx=10, pady=10)
        ttk.Label(header, text="Ramp/Soak Control", font=("Arial", 14, "bold")).pack(side=tk.LEFT)

        self.info_label = ttk.Label(self, text="", font=("Arial", 9))
        self.info_label.pack(fill=tk.X, padx=10)

        help_text = (
            "Each zone can run a Ramp/Soak profile of up to 20 segments. Segment 1 always ramps from "
            "the current temperature to its Setpoint. To SOAK (hold a temperature), give a segment the "
            "same Setpoint as the one before it, leave Slope at 0, and enter a Time. To RAMP, give a "
            "different Setpoint and set either a Slope (rate) or a Time (duration) - if both are set, "
            "Slope wins and Time is ignored. Editing a segment never affects one that is currently "
            "running; changes take effect once the profile reaches that segment."
        )
        ttk.Label(self, text=help_text, wraplength=940, justify=tk.LEFT, foreground="#444444").pack(
            fill=tk.X, padx=10, pady=(0, 8)
        )

        # ---- Zone selector + live status ----
        status_card = ttk.LabelFrame(self, text="Zone & Live Status")
        status_card.pack(fill=tk.X, padx=10, pady=(0, 8))

        ttk.Label(status_card, text="Zone:").grid(row=0, column=0, sticky="e", padx=(8, 6), pady=6)
        self.zone_var = tk.StringVar(value="")
        self.zone_combo = ttk.Combobox(status_card, textvariable=self.zone_var, state="readonly", width=22, values=[])
        self.zone_combo.grid(row=0, column=1, sticky="w", pady=6)
        self.zone_combo.bind("<<ComboboxSelected>>", self._on_zone_selected)

        self.status_label = ttk.Label(status_card, text="", foreground="gray", font=("Arial", 9))
        self.status_label.grid(row=0, column=2, sticky="w", padx=(16, 8), pady=6)

        self.live_status_label = ttk.Label(status_card, text="", font=("Arial", 9))
        self.live_status_label.grid(row=1, column=0, columnspan=3, sticky="w", padx=(8, 6), pady=(0, 8))

        # ---- Profile controls ----
        controls_card = ttk.LabelFrame(self, text="Profile Controls")
        controls_card.pack(fill=tk.X, padx=10, pady=(0, 8))

        ttk.Label(controls_card, text="Control Mode:").grid(row=0, column=0, sticky="e", padx=(8, 6), pady=6)
        self.control_mode_var = tk.StringVar(value=self._CONTROL_MODE_LABELS["STANDARD_CONTROL"])
        self.control_mode_combo = ttk.Combobox(
            controls_card, textvariable=self.control_mode_var, state="readonly", width=28,
            values=list(self._CONTROL_MODE_LABELS.values()),
        )
        self.control_mode_combo.grid(row=0, column=1, sticky="w", pady=6)
        self.control_mode_combo.bind("<<ComboboxSelected>>", self._on_control_mode_edited)
        ttk.Button(controls_card, text="Apply Mode", command=self._on_apply_control_mode).grid(
            row=0, column=2, sticky="w", padx=(6, 20), pady=6
        )

        ttk.Label(controls_card, text="Number of Segments (0-20):").grid(row=0, column=3, sticky="e", padx=(8, 6), pady=6)
        self.num_segments_var = tk.StringVar(value="")
        self.num_segments_entry = ttk.Entry(controls_card, textvariable=self.num_segments_var, width=6)
        self.num_segments_entry.grid(row=0, column=4, sticky="w", pady=6)
        self.num_segments_entry.bind("<KeyRelease>", self._on_num_segments_edited)
        self.num_segments_entry.bind("<FocusIn>", self._select_all_on_focus)
        ttk.Button(controls_card, text="Apply Count", command=self._on_apply_num_segments).grid(
            row=0, column=5, sticky="w", padx=(6, 8), pady=6
        )

        button_row = ttk.Frame(controls_card)
        button_row.grid(row=1, column=0, columnspan=6, sticky="w", padx=(8, 6), pady=(0, 8))
        ttk.Button(button_row, text="Start Profile (this zone)",
                   command=lambda: self._on_start_stop(True, all_zones=False)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(button_row, text="Stop Profile (this zone)",
                   command=lambda: self._on_start_stop(False, all_zones=False)).pack(side=tk.LEFT, padx=(0, 20))
        ttk.Button(button_row, text="Start All Zones",
                   command=lambda: self._on_start_stop(True, all_zones=True)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(button_row, text="Stop All Zones",
                   command=lambda: self._on_start_stop(False, all_zones=True)).pack(side=tk.LEFT)

        # ---- Segment table ----
        table_card = ttk.LabelFrame(self, text="Segments")
        table_card.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        table_toolbar = ttk.Frame(table_card)
        table_toolbar.pack(fill=tk.X, padx=8, pady=(6, 4))
        ttk.Button(table_toolbar, text="Reload From Controller", command=self._on_reload_from_controller).pack(side=tk.LEFT)
        ttk.Button(table_toolbar, text="Save Segment Changes", command=self._on_save_segments).pack(side=tk.LEFT, padx=(8, 0))
        self.table_status_label = ttk.Label(table_toolbar, text="", foreground="gray")
        self.table_status_label.pack(side=tk.LEFT, padx=(16, 0))

        canvas_frame = ttk.Frame(table_card)
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        rows_canvas = tk.Canvas(canvas_frame, height=380)
        scrollbar = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=rows_canvas.yview)
        rows_frame = ttk.Frame(rows_canvas)
        rows_frame.bind("<Configure>", lambda e: rows_canvas.configure(scrollregion=rows_canvas.bbox("all")))
        rows_canvas.create_window((0, 0), window=rows_frame, anchor="nw")
        rows_canvas.configure(yscrollcommand=scrollbar.set)
        rows_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        col_header = ttk.Frame(rows_frame)
        col_header.pack(fill=tk.X, pady=(0, 4))
        for col_text, width in (("Seg", 4), ("Setpoint (\u00b0C)", 14), ("Slope (\u00b0C/min)", 14), ("Time (h)", 10), ("Type", 16)):
            ttk.Label(col_header, text=col_text, font=("Arial", 9, "bold"), width=width, anchor="w").pack(side=tk.LEFT, padx=4)

        self.segment_rows: List[Dict[str, Any]] = []
        for seg_num in range(1, self.NUM_SEGMENT_ROWS + 1):
            row_frame = ttk.Frame(rows_frame)
            row_frame.pack(fill=tk.X, pady=1)

            seg_label = ttk.Label(row_frame, text=str(seg_num), width=4, anchor="w")
            seg_label.pack(side=tk.LEFT, padx=4)

            sp_var = tk.StringVar(value="")
            sp_entry = ttk.Entry(row_frame, textvariable=sp_var, width=14)
            sp_entry.pack(side=tk.LEFT, padx=4)
            sp_entry.bind("<KeyRelease>", self._on_segment_edited)
            sp_entry.bind("<FocusIn>", self._select_all_on_focus)

            slope_var = tk.StringVar(value="")
            slope_entry = ttk.Entry(row_frame, textvariable=slope_var, width=14)
            slope_entry.pack(side=tk.LEFT, padx=4)
            slope_entry.bind("<KeyRelease>", self._on_segment_edited)
            slope_entry.bind("<FocusIn>", self._select_all_on_focus)

            time_var = tk.StringVar(value="")
            time_entry = ttk.Entry(row_frame, textvariable=time_var, width=10)
            time_entry.pack(side=tk.LEFT, padx=4)
            time_entry.bind("<KeyRelease>", self._on_segment_edited)
            time_entry.bind("<FocusIn>", self._select_all_on_focus)

            type_label = ttk.Label(row_frame, text="", width=16, anchor="w", foreground="gray")
            type_label.pack(side=tk.LEFT, padx=4)

            self.segment_rows.append({
                "seg_label": seg_label,
                "sp_var": sp_var, "slope_var": slope_var, "time_var": time_var,
                "sp_entry": sp_entry, "slope_entry": slope_entry, "time_entry": time_entry,
                "type_label": type_label,
            })

    # -----------------------------
    # Small shared helpers
    # -----------------------------
    def _select_all_on_focus(self, event=None):
        widget = event.widget if event is not None else None
        if widget is None:
            return
        widget.select_range(0, tk.END)
        widget.icursor(tk.END)

    def _safe_float(self, value: Any) -> Optional[float]:
        text = str(value).strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _format_num(self, value: Any) -> str:
        if isinstance(value, (int, float)):
            return f"{float(value):.3f}".rstrip("0").rstrip(".")
        return ""

    def _safe_focus_get(self):
        try:
            return self.focus_get()
        except Exception:
            return None

    def _set_status(self, message: str, *, ok: bool = True):
        self.status_label.config(text=message, foreground=("green" if ok else "red"))

    def _set_table_status(self, message: str, *, ok: bool = True):
        self.table_status_label.config(text=message, foreground=("green" if ok else "red"))

    def _get_command_endpoint(self) -> tuple[str, int]:
        svc_state = get_service_config_state(self.logs_dir)
        cfg = svc_state.get("config", {}) if isinstance(svc_state, dict) else {}
        host = str(cfg.get("last_tcp_host", "127.0.0.1") or "127.0.0.1")
        try:
            port = int(cfg.get("last_tcp_port", 8765) or 8765)
        except Exception:
            port = 8765
        return host, port

    def _send_command(self, op: str, **fields) -> Dict[str, Any]:
        host, port = self._get_command_endpoint()
        msg = {"id": uuid.uuid4().hex[:8], "op": op}
        msg.update(fields)
        data = (json.dumps(msg) + "\n").encode("utf-8")
        with socket.create_connection((host, port), timeout=2.0) as s:
            s.sendall(data)
            s.settimeout(2.0)
            resp = s.recv(65536).decode("utf-8", errors="ignore").strip()
        return json.loads(resp) if resp else {"ok": False, "error": "empty response"}

    def _selected_zone_id(self) -> Optional[str]:
        token = self.zone_var.get().strip()
        if not token:
            return None
        return self._zone_token_to_id.get(token)

    # -----------------------------
    # Zone selector + population
    # -----------------------------
    def _refresh_zone_selector(self, zone_names: Dict[str, str]):
        zone_ids = sorted(
            [str(z) for z in self._latest_rampsoak_zones.keys()] or [str(z) for z in range(1, 7)],
            key=lambda x: int(x) if str(x).isdigit() else 999,
        )
        tokens = []
        mapping = {}
        for zone_id in zone_ids:
            token = f"{zone_id} - {zone_names.get(zone_id, f'Zone {zone_id}')}"
            tokens.append(token)
            mapping[token] = zone_id

        # Only reconfigure when the token list actually changed - repeatedly reconfiguring a
        # Combobox's values (even when unchanged) can corrupt its dropdown state on Windows.
        if tokens != list(self.zone_combo.cget("values")):
            self.zone_combo.configure(values=tokens)
        self._zone_token_to_id = mapping

        if not tokens:
            return
        if self.zone_var.get() not in mapping:
            self.zone_var.set(tokens[0])
            self._populate_for_zone(mapping[tokens[0]], force=True)

    def _on_zone_selected(self, _event=None):
        zone_id = self._selected_zone_id()
        if zone_id:
            self._segments_dirty = False
            self._num_segments_dirty = False
            self._control_mode_dirty = False
            self._populate_for_zone(zone_id, force=True)
            self._set_table_status("Zone changed, unsent edits discarded", ok=True)
            self._update_live_status()

    def _populate_for_zone(self, zone_id: str, *, force: bool = False):
        zone_data = self._latest_rampsoak_zones.get(str(zone_id), {})
        if not isinstance(zone_data, dict):
            zone_data = {}

        if not self._num_segments_dirty or force:
            num_segments = zone_data.get("num_segments")
            self.num_segments_var.set(str(int(num_segments)) if isinstance(num_segments, (int, float)) else "")

        if not self._segments_dirty or force:
            segments = zone_data.get("segments", {})
            segments = segments if isinstance(segments, dict) else {}
            for idx, row in enumerate(self.segment_rows):
                seg = segments.get(str(idx + 1), {})
                seg = seg if isinstance(seg, dict) else {}
                row["sp_var"].set(self._format_num(seg.get("sp_c")))
                row["slope_var"].set(self._format_num(seg.get("slope_c_per_min")))
                row["time_var"].set(self._format_num(seg.get("time_h")))
            self._segments_dirty = False

        if force:
            self._num_segments_dirty = False

        self._refresh_type_hints()
        self._apply_active_row_styling()

    def _apply_active_row_styling(self):
        count = self._safe_float(self.num_segments_var.get())
        for idx, row in enumerate(self.segment_rows):
            active = count is None or idx < int(count)
            row["seg_label"].config(foreground=("black" if active else "gray"))

    # -----------------------------
    # Segment "Type" hint column (Ramp / Soak, read-only, computed locally)
    # -----------------------------
    def _segment_type_hint(self, idx: int, sp: Optional[float], slope: Optional[float],
                            time_h: Optional[float], prev_sp: Optional[float]) -> str:
        if sp is None:
            return ""
        if idx == 0:
            return "Ramp (from PV)"
        if prev_sp is not None and abs(sp - prev_sp) < 1e-9:
            return "Soak"
        if slope:
            return "Ramp (by slope)"
        if time_h:
            return "Ramp (by time)"
        return "Ramp"

    def _refresh_type_hints(self):
        prev_sp: Optional[float] = None
        for idx, row in enumerate(self.segment_rows):
            sp = self._safe_float(row["sp_var"].get())
            slope = self._safe_float(row["slope_var"].get())
            time_h = self._safe_float(row["time_var"].get())
            row["type_label"].config(text=self._segment_type_hint(idx, sp, slope, time_h, prev_sp))
            if sp is not None:
                prev_sp = sp

    # -----------------------------
    # Live status (from telemetry - already polled independently of ramp/soak reads)
    # -----------------------------
    def _update_live_status(self):
        zone_id = self._selected_zone_id()
        if not zone_id:
            self.live_status_label.config(text="")
            return

        telem = get_telemetry_state(self.logs_dir)
        zones = ((telem or {}).get("telemetry", {}) or {}).get("zones", {})
        zdata = zones.get(str(zone_id), {}) if isinstance(zones, dict) else {}

        seg_idx = safe_get(zdata, "current_segment_index", default="N/A")
        seg_state = safe_get(zdata, "current_segment_state", default="N/A")
        control_mode = safe_get(zdata, "control_mode", default="N/A")
        loop_status = safe_get(zdata, "loop_status", default="N/A")
        remaining = safe_get(zdata, "ramp_soak_remaining", default=None)
        remaining_txt = f"{float(remaining):.2f}\u00b0C" if isinstance(remaining, (int, float)) else "N/A"

        self.live_status_label.config(
            text=(f"Current Segment: {seg_idx} ({seg_state})    Control Mode: {control_mode}    "
                  f"Loop Status: {loop_status}    Ramp/Soak Remaining: {remaining_txt}")
        )

        if not self._control_mode_dirty:
            label = self._CONTROL_MODE_LABELS.get(str(control_mode).strip().upper())
            if label:
                self.control_mode_var.set(label)

    # -----------------------------
    # Edit tracking
    # -----------------------------
    def _on_segment_edited(self, _event=None):
        self._segments_dirty = True
        self._set_table_status("Unsaved segment edits", ok=True)
        self._refresh_type_hints()

    def _on_num_segments_edited(self, _event=None):
        self._num_segments_dirty = True
        self._set_status("Unsaved segment-count edit", ok=True)

    def _on_control_mode_edited(self, _event=None):
        self._control_mode_dirty = True

    # -----------------------------
    # Actions
    # -----------------------------
    def _on_apply_control_mode(self):
        zone_id = self._selected_zone_id()
        if not zone_id:
            self._set_status("Select a zone first", ok=False)
            return
        wire = self._CONTROL_MODE_WIRE.get(self.control_mode_var.get())
        if wire is None:
            self._set_status("Choose a control mode first", ok=False)
            return
        try:
            resp = self._send_command("set_control_mode", zone=int(zone_id), mode=wire)
            if resp.get("ok"):
                self._control_mode_dirty = False
                self._set_status(f"Zone {zone_id} control mode updated", ok=True)
            else:
                self._set_status(f"Failed: {resp.get('error', 'unknown error')}", ok=False)
        except Exception as e:
            self._set_status(f"Service unreachable: {e}", ok=False)

    def _on_apply_num_segments(self):
        zone_id = self._selected_zone_id()
        if not zone_id:
            self._set_status("Select a zone first", ok=False)
            return
        count = self._safe_float(self.num_segments_var.get())
        if count is None or count != int(count) or not (0 <= count <= 20):
            self._set_status("Number of segments must be a whole number from 0 to 20", ok=False)
            return
        try:
            resp = self._send_command("set_num_segments", zone=int(zone_id), count=int(count))
            if resp.get("ok"):
                self._num_segments_dirty = False
                self._apply_active_row_styling()
                self._set_status(f"Zone {zone_id} segment count set to {int(count)}", ok=True)
            else:
                self._set_status(f"Failed: {resp.get('error', 'unknown error')}", ok=False)
        except Exception as e:
            self._set_status(f"Service unreachable: {e}", ok=False)

    def _on_start_stop(self, enable: bool, *, all_zones: bool):
        if all_zones:
            zones = sorted((int(z) for z in self._zone_token_to_id.values()), key=int) or list(range(1, 7))
        else:
            zone_id = self._selected_zone_id()
            if not zone_id:
                self._set_status("Select a zone first", ok=False)
                return
            zones = [int(zone_id)]
        try:
            resp = self._send_command("set_start_profile", zones=zones, enable=enable)
            if resp.get("ok"):
                action = "started" if enable else "stopped"
                target = "all zones" if all_zones else f"zone {zones[0]}"
                self._set_status(f"Ramp/Soak {action} for {target}", ok=True)
            else:
                self._set_status(f"Failed: {resp.get('error', 'unknown error')}", ok=False)
        except Exception as e:
            self._set_status(f"Service unreachable: {e}", ok=False)

    def _on_reload_from_controller(self):
        try:
            resp = self._send_command("read_rampsoak")
        except Exception as e:
            self._set_table_status(f"Service unreachable: {e}", ok=False)
            return
        if not resp.get("ok"):
            self._set_table_status(f"Reload failed: {resp.get('error', 'unknown error')}", ok=False)
            return

        state = get_rampsoak_state(self.logs_dir)
        zones = ((state or {}).get("rampsoak", {}) or {}).get("zones", {})
        self._latest_rampsoak_zones = zones if isinstance(zones, dict) else {}

        zone_id = self._selected_zone_id()
        if zone_id:
            self._segments_dirty = False
            self._num_segments_dirty = False
            self._populate_for_zone(zone_id, force=True)
        self._set_table_status("Reloaded from controller", ok=True)

    def _on_save_segments(self):
        zone_id = self._selected_zone_id()
        if not zone_id:
            self._set_table_status("Select a zone first", ok=False)
            return

        segments: Dict[str, Dict[str, float]] = {}
        for idx, row in enumerate(self.segment_rows):
            sp = self._safe_float(row["sp_var"].get())
            slope = self._safe_float(row["slope_var"].get())
            time_h = self._safe_float(row["time_var"].get())
            if sp is None and slope is None and time_h is None:
                continue
            segments[str(idx + 1)] = {
                "sp_c": sp if sp is not None else 0.0,
                "slope_c_per_min": slope if slope is not None else 0.0,
                "time_h": time_h if time_h is not None else 0.0,
            }

        if not segments:
            self._set_table_status("No segment values to save", ok=False)
            return

        try:
            resp = self._send_command("set_rampsoak_segments", zone=int(zone_id), segments=segments)
            if resp.get("ok"):
                self._segments_dirty = False
                self._set_table_status(f"Saved {len(segments)} segment(s) to zone {zone_id}", ok=True)
            else:
                self._set_table_status(f"Save failed: {resp.get('error', 'unknown error')}", ok=False)
        except Exception as e:
            self._set_table_status(f"Service unreachable: {e}", ok=False)

    # -----------------------------
    # Lifecycle hooks
    # -----------------------------
    def on_tab_selected(self):
        """Called by main GUI when the Ramp/Soak tab is shown; forces a fresh controller read."""
        try:
            self._on_reload_from_controller()
        except Exception:
            LOGGER.exception("RampSoakPanel.on_tab_selected failed")

    def refresh(self):
        """Periodic refresh: updates live status and zone list; never overwrites unsaved edits."""
        try:
            focused = self._safe_focus_get()
            if focused == self.zone_combo:
                return

            state = get_rampsoak_state(self.logs_dir)
            ts = state.get("ts") if isinstance(state, dict) else None
            rampsoak = state.get("rampsoak", {}) if isinstance(state, dict) else {}
            zones = rampsoak.get("zones", {}) if isinstance(rampsoak, dict) else {}
            self._latest_rampsoak_zones = zones if isinstance(zones, dict) else {}

            if zones:
                self.info_label.config(text=f"Last update: {format_timestamp(ts)}")
            else:
                self.info_label.config(text="No ramp/soak data yet - open this tab to fetch it from the controller")

            zone_names = _load_zone_names_from_logs(self.logs_dir)
            self._refresh_zone_selector(zone_names)

            zone_id = self._selected_zone_id()
            if zone_id:
                self._populate_for_zone(zone_id, force=False)
            self._update_live_status()
        except Exception:
            LOGGER.exception("RampSoakPanel.refresh failed")
