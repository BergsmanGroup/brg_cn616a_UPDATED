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
import logging
import math
from datetime import datetime

from .state_reader import format_timestamp, safe_get
from .service_client import ServiceClient
from .retention_estimator import RetentionEstimateWorker, estimate_log_storage as estimate_log_storage
from .config_values import parse_retention_seconds
from .entry_behavior import select_all_on_focus
from .state_values import get_field, get_numeric, get_zone, get_zones
from .snapshot_reader import StateSnapshot, StateSnapshotReader

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


def _load_zone_names_from_state(svc_state: Dict[str, Any]) -> Dict[str, str]:
    cfg = svc_state.get("config", {}) if isinstance(svc_state, dict) else {}
    return _normalize_zone_names(cfg.get("zone_names", {}))


class StatePanel(tk.Frame):
    """Base class for state display panels."""
    
    def __init__(
        self,
        parent,
        logs_dir: Path,
        auto_refresh_interval: float = 2.0,
        debug: bool = False,
        service_client: Optional[ServiceClient] = None,
        snapshot_reader: Optional[StateSnapshotReader] = None,
    ):
        super().__init__(parent)
        self.logs_dir = Path(logs_dir)
        self.auto_refresh_interval = auto_refresh_interval
        self.debug = debug
        self.service_client = service_client
        self.snapshot_reader = snapshot_reader or StateSnapshotReader(self.logs_dir)
        self._owns_snapshot_reader = snapshot_reader is None
        self.running = False
        self._after_id: Optional[str] = None

    def _snapshot(self, name: str) -> StateSnapshot:
        return self.snapshot_reader.snapshot(name)

    def _state(self, name: str) -> tuple[Dict[str, Any], StateSnapshot]:
        snapshot = self._snapshot(name)
        return (snapshot.data or {}, snapshot)

    @staticmethod
    def _snapshot_status(snapshot: StateSnapshot) -> str:
        if snapshot.error:
            return f"Snapshot unavailable: {snapshot.error}"
        if snapshot.age_s is None:
            return "Snapshot age unavailable"
        age = max(0, int(snapshot.age_s))
        freshness = "stale" if snapshot.stale else "current"
        return f"Snapshot {freshness} ({age}s old)"

    def _service_endpoint(self) -> tuple[str, int]:
        svc_snapshot = self._snapshot("service_config")
        if svc_snapshot.error and not svc_snapshot.error.startswith("Snapshot file not found:"):
            raise RuntimeError(f"Service configuration is not ready: {svc_snapshot.error}")
        svc_state = svc_snapshot.data or {}
        cfg = svc_state.get("config", {}) if isinstance(svc_state, dict) else {}
        host = str(cfg.get("last_tcp_host", "127.0.0.1") or "127.0.0.1")
        try:
            port = int(cfg.get("last_tcp_port", 8765) or 8765)
        except (TypeError, ValueError):
            port = 8765
        return host, port

    def _submit_service(
        self,
        messages: List[Dict[str, Any]],
        callback,
        *,
        priority: int = 0,
        coalesce_key: Optional[str] = None,
        endpoint: Optional[tuple[str, int]] = None,
    ) -> str:
        if self.service_client is None:
            raise RuntimeError("shared service client is not configured")
        host, port = endpoint or self._service_endpoint()
        return self.service_client.submit(
            host,
            port,
            messages,
            callback,
            priority=priority,
            coalesce_key=coalesce_key,
        )

    @staticmethod
    def _service_result_summary(result: Dict[str, Any]) -> tuple[str, bool]:
        labels = []
        all_succeeded = True
        needs_verification = False
        for outcome in result.get("outcomes", []):
            operation = str(outcome.get("op", "service operation"))
            state = outcome.get("state")
            if state == "confirmed_success":
                labels.append(f"{operation}: succeeded")
                continue
            all_succeeded = False
            if state == "confirmed_failure":
                labels.append(f"{operation}: rejected ({outcome.get('error', 'service rejected the command')})")
            elif state == "not_sent":
                labels.append(f"{operation}: not sent ({outcome.get('error', 'request was not sent')})")
            else:
                labels.append(f"{operation}: outcome unknown ({outcome.get('error', 'no confirmed response')})")
                needs_verification = True
        if not labels:
            return "Service returned no operation outcome", False
        message = "; ".join(labels)
        if needs_verification:
            message += ". Verify controller/service state before retrying."
        return message, all_succeeded
    
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

    def shutdown(self):
        if self._owns_snapshot_reader:
            self.snapshot_reader.shutdown()
    
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
    
    def __init__(
        self,
        parent,
        logs_dir: Path,
        debug: bool = False,
        service_client: Optional[ServiceClient] = None,
        snapshot_reader: Optional[StateSnapshotReader] = None,
    ):
        super().__init__(
            parent,
            logs_dir,
            debug=debug,
            service_client=service_client,
            snapshot_reader=snapshot_reader,
        )
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
        pv = get_numeric(zone_data, "pv_c", "pv", "PV", "process_value_c", "process_value")
        sp = get_numeric(zone_data, "sp_abs_c", "sp_abs", "sp_c", "sp", "SP_abs", "SP", "setpoint_c", "setpoint")
        out = get_numeric(zone_data, "out_pct", "output_pct", "output", "OUT")

        pv = f"{pv:.2f} C" if pv is not None else "N/A"
        sp = f"{sp:.2f} C" if sp is not None else "N/A"
        out = f"{out:.1f} %" if out is not None else "N/A"

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
        
        control_method = get_field(zone_data, "control_method", "mode", "control_mode") or "N/A"
        autotune_enable = get_field(zone_data, "autotune_enable", "autotune_enabled") or "N/A"
        control_mode = get_field(zone_data, "control_mode", "mode", "control_method") or "N/A"
        loop_status = get_field(zone_data, "loop_status", "status") or "N/A"
        
        # Convert ENABLE/DISABLE to On/Off and always add SP
        autotune_sp = get_numeric(zone_data, "autotune_sp_c", "autotune_sp", "sp_autotune_c", "sp_autotune")
        if autotune_sp is not None:
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
        segment_idx = get_field(zone_data, "current_segment_index", "segment_index", "ramp_soak_segment")
        segment_state = get_field(zone_data, "current_segment_state", "segment_state")
        ramp_remaining = get_field(zone_data, "ramp_soak_remaining", "ramp_soak_remaining_s", "remaining_s")
        segment_idx = "N/A" if segment_idx is None else segment_idx
        segment_state = "N/A" if segment_state is None else segment_state
        ramp_remaining = "N/A" if ramp_remaining is None else ramp_remaining
        
        if isinstance(ramp_remaining, float):
            ramp_remaining = f"{ramp_remaining:.1f}s"
        
        lines.append(f"  Segment: {segment_idx} ({segment_state})  |  Ramp/Soak Remaining: {ramp_remaining}")
        
        # === PID PARAMETERS ===
        lines.append("")  # Blank line separator
        lines.append("PID Parameters:")
        
        # Get PID gains directly from telemetry zone data (now part of telemetry polling)
        p_gain = get_numeric(zone_data, "p_gain", "kp", "proportional_gain")
        i_gain = get_numeric(zone_data, "i_gain", "ki", "integral_gain")
        d_gain = get_numeric(zone_data, "d_gain", "kd", "derivative_gain")
        
        # Format floats nicely
        p_gain = f"{p_gain:.4f}" if p_gain is not None else "N/A"
        i_gain = f"{i_gain:.4f}" if i_gain is not None else "N/A"
        d_gain = f"{d_gain:.4f}" if d_gain is not None else "N/A"
        
        lines.append(f"  P: {p_gain}  |  I: {i_gain}  |  D: {d_gain}")
        
        # Try to get deadband and cycle_time from config if available
        pid_params = zone_config.get("pid_parameters", {})
        deadband = safe_get(pid_params, "deadband", default=None)
        cycle_time = safe_get(pid_params, "cycle_time_s", default=None)
        
        if isinstance(deadband, (int, float)) and math.isfinite(float(deadband)):
            deadband = f"{deadband:.2f}°C"
        else:
            deadband = "N/A"
        if isinstance(cycle_time, (int, float)) and math.isfinite(float(cycle_time)):
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
            avg_error = get_numeric(analysis_data, "avg_abs_error_c", "mae_c", "mae")
            threshold = get_numeric(analysis_data, "threshold_c", "equilibrium_threshold_c")

            if avg_error is not None:
                avg_error = f"{avg_error:.3f}°C"
            else:
                avg_error = "N/A"
            if threshold is not None:
                threshold = f"{threshold:.2f}°C"
            else:
                threshold = "N/A"

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
            telem_state, telem_snapshot = self._state("telemetry")
            analysis_state, analysis_snapshot = self._state("analysis")
            config_state, _config_snapshot = self._state("config")
            service_state, _service_snapshot = self._state("service_config")
            zone_names = _load_zone_names_from_state(service_state)
            
            ts = telem_state.get("ts")
            port = telem_state.get("port")
            config = config_state.get("config", {})
            zones_config = config.get("zones", {})
            zones_data = get_zones(telem_state, "telemetry")
            analysis_data = get_zones(analysis_state, "analysis")
            zone_ids = sorted((str(zone_id) for zone_id in zones_data), key=lambda value: int(value) if value.isdigit() else 999)
            
            # Show the timestamp of whichever is more recent
            analysis_ts = analysis_state.get("ts")
            display_ts = ts if ts else analysis_ts
            
            self.info_label.config(
                text=(f"Last update: {format_timestamp(display_ts)} | Port: {port} | "
                      f"Telemetry {self._snapshot_status(telem_snapshot)} | "
                      f"Analysis {self._snapshot_status(analysis_snapshot)}")
            )
            
            if not zone_ids:
                for label in self.zone_numeric_labels.values():
                    label.config(text="PV: N/A    SP: N/A\nOutput: N/A")
                for label in self.zone_detail_labels.values():
                    label.config(text="No data available")
                return
            
            # Clear old labels if needed
            if set(self.zone_detail_labels.keys()) != set(zone_ids):
                for widget in self.zones_container.winfo_children():
                    widget.destroy()
                self.zone_numeric_labels.clear()
                self.zone_detail_labels.clear()
                self.zone_frames.clear()
            
            # Update or create zone displays
            for idx, zone_id in enumerate(zone_ids):
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
                zone_data = get_zone(zones_data, zone_id)
                zone_analysis = get_zone(analysis_data, zone_id)
                zone_config = get_zone(zones_config, zone_id)
                
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
    
    def __init__(
        self,
        parent,
        logs_dir: Path,
        debug: bool = False,
        on_viewer_config_changed=None,
        on_service_config_changed=None,
        service_client: Optional[ServiceClient] = None,
        snapshot_reader: Optional[StateSnapshotReader] = None,
    ):
        super().__init__(
            parent,
            logs_dir,
            debug=debug,
            service_client=service_client,
            snapshot_reader=snapshot_reader,
        )
        self.on_viewer_config_changed = on_viewer_config_changed
        self.on_service_config_changed = on_service_config_changed
        self._service_cfg_cache = {}
        self._form_updating = False
        self._form_dirty = False
        self._edit_revision = 0
        self._config_action_pending = False
        self._retention_revision = 0
        self._retention_specs = None
        self._retention_estimates = {}
        self.create_widgets()
        self._retention_worker = RetentionEstimateWorker(self.logs_dir)
    
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
        self.apply_settings_button = ttk.Button(
            self.service_actions_frame,
            text="Apply Service Settings",
            command=self._on_apply_settings_clicked,
        )
        self.apply_settings_button.pack(side=tk.RIGHT)

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
        self.connect_button = ttk.Button(connection_box, text="Connect", command=self._on_connect_clicked)
        self.connect_button.grid(row=conn_btn_row, column=0, pady=(6, 8), padx=8, sticky="e")
        self.disconnect_button = ttk.Button(connection_box, text="Disconnect", command=self._on_disconnect_clicked)
        self.disconnect_button.grid(row=conn_btn_row, column=1, pady=(6, 8), padx=(0, 8), sticky="w")
        self.refresh_connection_button = ttk.Button(
            connection_box,
            text="Refresh Connection",
            command=self._on_refresh_connection_clicked,
        )
        self.refresh_connection_button.grid(row=conn_btn_row, column=2, pady=(6, 8), padx=(0, 8), sticky="w")

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
        self.telemetry_retention = ttk.Entry(rotation_box, width=18)
        self.max_config_log_bytes = ttk.Entry(rotation_box, width=18)
        self.max_config_log_files = ttk.Entry(rotation_box, width=18)
        self.max_rampsoak_log_bytes = ttk.Entry(rotation_box, width=18)
        self.max_rampsoak_log_files = ttk.Entry(rotation_box, width=18)
        self.max_analysis_log_bytes = ttk.Entry(rotation_box, width=18)
        self.analysis_retention = ttk.Entry(rotation_box, width=18)
        self.retention_estimate_label = ttk.Label(rotation_box, text="", justify=tk.LEFT, wraplength=900)

        rotation_rows = [
            ("Service config max bytes", self.max_service_config_log_bytes, "Service config max files", self.max_service_config_log_files),
            ("Telemetry max file size", self.max_telemetry_log_bytes, "Telemetry retention duration", self.telemetry_retention),
            ("Config max bytes", self.max_config_log_bytes, "Config max files", self.max_config_log_files),
            ("Ramp/Soak max bytes", self.max_rampsoak_log_bytes, "Ramp/Soak max files", self.max_rampsoak_log_files),
            ("Analysis max file size", self.max_analysis_log_bytes, "Analysis retention duration", self.analysis_retention),
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
        self.retention_estimate_label.grid(
            row=len(rotation_rows), column=0, columnspan=4, sticky="w", padx=8, pady=(4, 8)
        )

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
            state, config_snapshot = self._state("config")
            ts = state.get("ts")
            config = state.get("config", {})
            
            self.info_label.config(
                text=f"Last update: {format_timestamp(ts)} | {self._snapshot_status(config_snapshot)}"
            )
            
            if not config:
                self.system_label.config(text="No config data available")
                self._poll_retention_estimates()
                return
            
            # System section
            system = config.get("system", {})
            system_text = self._format_system_config(system)
            self.system_label.config(text=system_text)
            
            # Zones section
            zones = config.get("zones", {})
            
            # Service configuration section (separate state file)
            svc_state, service_snapshot = self._state("service_config")
            svc_ts = svc_state.get("ts")
            svc_cfg = svc_state.get("config", {})
            zone_names = _normalize_zone_names(svc_cfg.get("zone_names", {})) if isinstance(svc_cfg, dict) else _normalize_zone_names({})
            self._update_zones_config(zones, zone_names)
            self.service_info_label.config(
                text=f"Last update: {format_timestamp(svc_ts)} | {self._snapshot_status(service_snapshot)}"
            )
            if not svc_cfg:
                self.service_status_label.config(text="No service config data available", foreground="gray")
            else:
                self._service_cfg_cache = dict(svc_cfg)
                if not self._form_dirty:
                    self._populate_service_form(svc_cfg)
            self._update_retention_estimate(svc_cfg if isinstance(svc_cfg, dict) else None)
            self._poll_retention_estimates()
        
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
            self._set_entry_text(self.telemetry_retention, self._format_duration(cfg.get("telemetry_retention_s", 86_400.0)))
            self._set_entry_text(self.max_config_log_bytes, cfg.get("max_config_log_bytes", 5_000_000))
            self._set_entry_text(self.max_config_log_files, cfg.get("max_config_log_files", 10))
            self._set_entry_text(self.max_rampsoak_log_bytes, cfg.get("max_rampsoak_log_bytes", 5_000_000))
            self._set_entry_text(self.max_rampsoak_log_files, cfg.get("max_rampsoak_log_files", 10))
            self._set_entry_text(self.max_analysis_log_bytes, cfg.get("max_analysis_log_bytes", 10_000_000))
            self._set_entry_text(self.analysis_retention, self._format_duration(cfg.get("analysis_retention_s", 86_400.0)))
            self._update_retention_estimate(cfg)

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
            number = float(text)
        except ValueError:
            return None
        return number if math.isfinite(number) else None

    def _safe_int(self, value: str) -> Optional[int]:
        text = str(value).strip()
        if text == "":
            return None
        try:
            return int(text)
        except ValueError:
            return None

    def _format_duration(self, seconds: Any) -> str:
        try:
            hours = float(seconds) / 3600.0
        except (TypeError, ValueError):
            hours = 24.0
        return f"{hours:g} h"

    def _update_retention_estimate(self, cfg: Optional[Dict[str, Any]] = None):
        cfg = cfg or self._service_cfg_cache
        zones = cfg.get("zones_list", [1, 2, 3, 4, 5, 6]) if cfg.get("zones_mode") == "list" else [1, 2, 3, 4, 5, 6]
        if not isinstance(zones, (list, tuple)):
            zones = [1, 2, 3, 4, 5, 6]
        zone_factor = max(1, len(zones)) / 6.0
        specs = {
            "telemetry": (
                "cn616a_telemetry_log.jsonl",
                parse_retention_seconds(self.telemetry_retention.get()),
                self._safe_float(self.polling_telemetry_hz.get()),
                self._safe_int(self.max_telemetry_log_bytes.get()),
                max(800, int(1600 * zone_factor)),
            ),
            "analysis": (
                "cn616a_analysis_log.jsonl",
                parse_retention_seconds(self.analysis_retention.get()),
                self._safe_float(self.polling_analysis_hz.get()),
                self._safe_int(self.max_analysis_log_bytes.get()),
                max(500, int(1000 * zone_factor)),
            ),
        }
        specs_changed = specs != self._retention_specs
        if specs_changed:
            self._retention_estimates = {}
        try:
            self._retention_revision = self._retention_worker.request(specs)
        except RuntimeError:
            LOGGER.exception("Retention estimate requested after worker shutdown")
            return
        self._retention_specs = specs
        if specs_changed or not self._retention_estimates:
            self.retention_estimate_label.config(text="Estimating retention from a bounded log sample…")

    def _poll_retention_estimates(self):
        for revision, _specs, estimates in self._retention_worker.poll_results():
            if revision == self._retention_revision:
                self._retention_estimates = estimates
                self._render_retention_estimate(estimates)

    def _render_retention_estimate(self, estimates: Dict[str, Any]):
        if any(estimates.get(key) is None for key in ("telemetry", "analysis")):
            self.retention_estimate_label.config(
                text="Estimated retention: enter positive durations, polling rates, and file sizes."
            )
            return

        descriptions = []
        for key in ("telemetry", "analysis"):
            estimate = estimates.get(key)
            if not isinstance(estimate, dict) or estimate.get("error"):
                descriptions.append(f"{key} estimate unavailable: {estimate.get('error', 'worker error') if isinstance(estimate, dict) else 'invalid inputs'}")
                continue
            status = estimate.get("sample_status", "unknown")
            confidence = "sampled" if estimate.get("file_count_confident") else f"uncertain: {status}"
            sample_errors = estimate.get("sample_errors") or []
            error_text = f"; {sample_errors[0][:160]}" if sample_errors else ""
            descriptions.append(
                f"{key} {estimate['file_count']} files, "
                f"~{self._format_bytes(estimate['estimated_bytes'])} data "
                f"({self._format_bytes(estimate['capacity_bytes'])} capacity; "
                f"{confidence}, {estimate['sampled_records']} records / "
                f"{estimate['sampled_bytes']} bytes sampled{error_text})"
            )
        self.retention_estimate_label.config(
            text=(
                "Estimated retention: " + "; ".join(descriptions) + ". "
                "Analysis is an upper-bound estimate because only changed results are logged."
            )
        )

    def shutdown(self):
        self._retention_worker.shutdown()
        super().shutdown()

    @staticmethod
    def _format_bytes(value: int) -> str:
        size = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024.0 or unit == "TB":
                return f"{size:.1f} {unit}"
            size /= 1024.0

    def _get_service_endpoint(self) -> tuple[str, int]:
        host = self.conn_tcp_host.get().strip() or "127.0.0.1"
        port = self._safe_int(self.conn_tcp_port.get())
        if port is None:
            port = int(self._service_cfg_cache.get("last_tcp_port", 8765) or 8765)
        return host, port

    def _set_service_status(self, message: str, ok: bool = True):
        self.service_status_label.config(text=message, foreground=("green" if ok else "red"))

    def _set_config_action_pending(self, pending: bool):
        self._config_action_pending = pending
        state = "disabled" if pending else "normal"
        for button in (
            self.apply_settings_button,
            self.connect_button,
            self.disconnect_button,
            self.refresh_connection_button,
        ):
            button.configure(state=state)

    def _on_form_edited(self, _event=None):
        if self._form_updating:
            return
        self._form_dirty = True
        self._edit_revision += 1
        self.service_status_label.config(text="Unsaved changes", foreground="orange")
        self._update_retention_estimate()

    def _queue_config_commands(
        self,
        messages: List[Dict[str, Any]],
        *,
        pending_text: str,
        success_text: str,
        notify_viewer: bool = False,
    ):
        if self._form_updating or self._config_action_pending:
            return
        self._set_config_action_pending(True)
        self._set_service_status(pending_text)
        edit_revision = self._edit_revision

        def on_result(result):
            self._set_config_action_pending(False)
            for outcome in result.get("outcomes", []):
                response = outcome.get("response", {})
                cfg = response.get("service_config") if isinstance(response, dict) else None
                if outcome.get("op") == "set_service_config" and outcome.get("state") == "confirmed_success" and isinstance(cfg, dict):
                    self._service_cfg_cache = dict(cfg)
                    if edit_revision == self._edit_revision:
                        self._populate_service_form(cfg)
                        self._form_dirty = False
                    else:
                        self._form_dirty = True
                    if notify_viewer and self.on_viewer_config_changed:
                        self.on_viewer_config_changed(self._extract_viewer_from_cfg(cfg))
                    if self.on_service_config_changed:
                        self.on_service_config_changed(cfg)

            summary, succeeded = self._service_result_summary(result)
            self._set_service_status(success_text if succeeded else summary, ok=succeeded)

        try:
            status = self._submit_service(
                messages,
                on_result,
                endpoint=self._get_service_endpoint(),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            self._set_config_action_pending(False)
            self._set_service_status(f"Unable to queue service request: {exc}", ok=False)
            return
        if status != "submitted":
            self._set_config_action_pending(False)
            self._set_service_status(f"Service request {status}; try again when the service is available", ok=False)

    def _apply_service_patch(self, patch: Dict[str, Any], *, notify_viewer: bool = False):
        if not patch:
            self._set_service_status("No settings to apply", ok=False)
            return
        self._queue_config_commands(
            [{"op": "set_service_config", "patch": patch}],
            pending_text="Applying service settings…",
            success_text="Settings applied",
            notify_viewer=notify_viewer,
        )

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
            ("telemetry_retention_s", self.telemetry_retention.get()),
            ("max_config_log_bytes", self.max_config_log_bytes.get()),
            ("max_config_log_files", self.max_config_log_files.get()),
            ("max_rampsoak_log_bytes", self.max_rampsoak_log_bytes.get()),
            ("max_rampsoak_log_files", self.max_rampsoak_log_files.get()),
            ("max_analysis_log_bytes", self.max_analysis_log_bytes.get()),
            ("analysis_retention_s", self.analysis_retention.get()),
        ]
        for key, value in pairs:
            if key.endswith("_retention_s"):
                seconds = parse_retention_seconds(value)
                if seconds is not None:
                    patch[key] = seconds
                continue
            if key.endswith("_files"):
                num = self._safe_int(value)
                if num is not None:
                    patch[key] = max(1, num)
            else:
                num = self._safe_int(value)
                if num is not None:
                    patch[key] = max(1, num)
        telemetry = self._retention_estimates.get("telemetry")
        if isinstance(telemetry, dict) and telemetry.get("file_count_confident"):
            patch["max_telemetry_log_files"] = telemetry["file_count"]
        analysis = self._retention_estimates.get("analysis")
        if isinstance(analysis, dict) and analysis.get("file_count_confident"):
            patch["max_analysis_log_files"] = analysis["file_count"]
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
        if self._config_action_pending:
            return
        combined_patch = {}
        combined_patch.update(self._build_polling_patch())
        combined_patch.update(self._build_log_rotation_patch())
        combined_patch.update(self._build_zones_patch())
        combined_patch.update(self._build_viewer_patch())
        self._apply_service_patch(combined_patch, notify_viewer=True)

    def _on_connect_clicked(self):
        if self._config_action_pending:
            return
        connection_patch = self._build_connection_patch()
        self._queue_config_commands(
            [
                {"op": "set_service_config", "patch": connection_patch},
                {"op": "connect_serial"},
            ],
            pending_text="Applying connection settings and connecting…",
            success_text="Connected",
        )

    def _on_disconnect_clicked(self):
        self._queue_config_commands(
            [{"op": "disconnect_serial"}],
            pending_text="Disconnecting…",
            success_text="Disconnected",
        )

    def _on_refresh_connection_clicked(self):
        if self._config_action_pending:
            return
        connection_patch = self._build_connection_patch()
        self._queue_config_commands(
            [
                {"op": "set_service_config", "patch": connection_patch},
                {"op": "refresh_connection"},
            ],
            pending_text="Applying connection settings and refreshing…",
            success_text="Connection refreshed",
        )

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

    def __init__(
        self,
        parent,
        logs_dir: Path,
        debug: bool = False,
        service_client: Optional[ServiceClient] = None,
        snapshot_reader: Optional[StateSnapshotReader] = None,
    ):
        super().__init__(
            parent,
            logs_dir,
            debug=debug,
            service_client=service_client,
            snapshot_reader=snapshot_reader,
        )
        self._latest_rampsoak_zones: Dict[str, Any] = {}
        self._zone_token_to_id: Dict[str, str] = {}
        self._segments_dirty = False
        self._num_segments_dirty = False
        self._control_mode_dirty = False
        self._form_updating = False
        self._command_pending = False
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
        self.apply_mode_button = ttk.Button(controls_card, text="Apply Mode", command=self._on_apply_control_mode)
        self.apply_mode_button.grid(
            row=0, column=2, sticky="w", padx=(6, 20), pady=6
        )

        ttk.Label(controls_card, text="Number of Segments (0-20):").grid(row=0, column=3, sticky="e", padx=(8, 6), pady=6)
        self.num_segments_var = tk.StringVar(value="")
        self.num_segments_entry = ttk.Entry(controls_card, textvariable=self.num_segments_var, width=6)
        self.num_segments_entry.grid(row=0, column=4, sticky="w", pady=6)
        self.num_segments_var.trace_add("write", self._on_num_segments_edited)
        self.num_segments_entry.bind("<FocusIn>", self._select_all_on_focus)
        self.apply_count_button = ttk.Button(controls_card, text="Apply Count", command=self._on_apply_num_segments)
        self.apply_count_button.grid(
            row=0, column=5, sticky="w", padx=(6, 8), pady=6
        )

        button_row = ttk.Frame(controls_card)
        button_row.grid(row=1, column=0, columnspan=6, sticky="w", padx=(8, 6), pady=(0, 8))
        self.profile_buttons = [
            ttk.Button(button_row, text="Start Profile (this zone)",
                       command=lambda: self._on_start_stop(True, all_zones=False)),
            ttk.Button(button_row, text="Stop Profile (this zone)",
                       command=lambda: self._on_start_stop(False, all_zones=False)),
            ttk.Button(button_row, text="Start All Zones",
                       command=lambda: self._on_start_stop(True, all_zones=True)),
            ttk.Button(button_row, text="Stop All Zones",
                       command=lambda: self._on_start_stop(False, all_zones=True)),
        ]
        for index, button in enumerate(self.profile_buttons):
            button.pack(side=tk.LEFT, padx=(0, 6 if index < len(self.profile_buttons) - 1 else 0))

        # ---- Segment table ----
        table_card = ttk.LabelFrame(self, text="Segments")
        table_card.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        table_toolbar = ttk.Frame(table_card)
        table_toolbar.pack(fill=tk.X, padx=8, pady=(6, 4))
        self.reload_button = ttk.Button(table_toolbar, text="Reload From Controller", command=self._on_reload_from_controller)
        self.reload_button.pack(side=tk.LEFT)
        self.save_segments_button = ttk.Button(table_toolbar, text="Save Segment Changes", command=self._on_save_segments)
        self.save_segments_button.pack(side=tk.LEFT, padx=(8, 0))
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
            sp_var.trace_add("write", self._on_segment_edited)
            sp_entry.bind("<FocusIn>", self._select_all_on_focus)

            slope_var = tk.StringVar(value="")
            slope_entry = ttk.Entry(row_frame, textvariable=slope_var, width=14)
            slope_entry.pack(side=tk.LEFT, padx=4)
            slope_var.trace_add("write", self._on_segment_edited)
            slope_entry.bind("<FocusIn>", self._select_all_on_focus)

            time_var = tk.StringVar(value="")
            time_entry = ttk.Entry(row_frame, textvariable=time_var, width=10)
            time_entry.pack(side=tk.LEFT, padx=4)
            time_var.trace_add("write", self._on_segment_edited)
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
        select_all_on_focus(event)

    def _safe_float(self, value: Any) -> Optional[float]:
        text = str(value).strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        return number if math.isfinite(number) else None

    def _format_num(self, value: Any) -> str:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
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

    def _selected_zone_id(self) -> Optional[str]:
        token = self.zone_var.get().strip()
        if not token:
            return None
        return self._zone_token_to_id.get(token)

    # -----------------------------
    # Zone selector + population
    # -----------------------------
    def _refresh_zone_selector(self, zone_names: Dict[str, str]):
        focused = self._safe_focus_get()
        edit_widgets = [self.num_segments_entry]
        edit_widgets.extend(
            widget
            for row in self.segment_rows
            for widget in (row["sp_entry"], row["slope_entry"], row["time_entry"])
        )
        if (
            self._command_pending
            or self._segments_dirty
            or self._num_segments_dirty
            or self._control_mode_dirty
            or focused in edit_widgets
            or focused is self.control_mode_combo
        ):
            return
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
        zone_data = get_zone(self._latest_rampsoak_zones, zone_id)
        if not isinstance(zone_data, dict):
            zone_data = {}
        focused = self._safe_focus_get()
        edit_widgets = [self.num_segments_entry]
        edit_widgets.extend(
            widget
            for row in self.segment_rows
            for widget in (row["sp_entry"], row["slope_entry"], row["time_entry"])
        )
        if not force and focused in edit_widgets:
            return

        self._form_updating = True
        try:
            if not self._num_segments_dirty or force:
                num_segments = get_numeric(zone_data, "num_segments", "segment_count")
                self.num_segments_var.set(str(int(num_segments)) if num_segments is not None else "")

            if not self._segments_dirty or force:
                segments = get_field(zone_data, "segments", "profile_segments") or {}
                for idx, row in enumerate(self.segment_rows):
                    seg = get_zone(segments, idx + 1) if isinstance(segments, dict) else {}
                    row["sp_var"].set(self._format_num(get_field(seg, "sp_c", "setpoint_c", "setpoint")))
                    row["slope_var"].set(
                        self._format_num(get_field(seg, "slope_c_per_min", "slope", "rate_c_per_min"))
                    )
                    row["time_var"].set(self._format_num(get_field(seg, "time_h", "time", "duration_h")))
                self._segments_dirty = False

            if force:
                self._num_segments_dirty = False
        finally:
            self._form_updating = False

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

        telem, snapshot = self._state("telemetry")
        zones = get_zones(telem, "telemetry")
        zdata = get_zone(zones, zone_id)

        seg_idx = get_field(zdata, "current_segment_index", "segment_index", "ramp_soak_segment")
        seg_state = get_field(zdata, "current_segment_state", "segment_state")
        control_mode = get_field(zdata, "control_mode", "control_method", "mode")
        loop_status = get_field(zdata, "loop_status", "status")
        seg_idx = "N/A" if seg_idx is None else seg_idx
        seg_state = "N/A" if seg_state is None else seg_state
        control_mode = "N/A" if control_mode is None else control_mode
        loop_status = "N/A" if loop_status is None else loop_status
        remaining = get_numeric(zdata, "ramp_soak_remaining", "ramp_soak_remaining_s", "remaining_s")
        remaining_txt = f"{remaining:.2f}\u00b0C" if remaining is not None else "N/A"

        self.live_status_label.config(
            text=(f"{self._snapshot_status(snapshot)} | Current Segment: {seg_idx} ({seg_state})    Control Mode: {control_mode}    "
                  f"Loop Status: {loop_status}    Ramp/Soak Remaining: {remaining_txt}")
        )

        if not self._control_mode_dirty and self._safe_focus_get() is not self.control_mode_combo:
            label = self._CONTROL_MODE_LABELS.get(str(control_mode).strip().upper())
            if label and label != self.control_mode_var.get():
                self.control_mode_var.set(label)

    # -----------------------------
    # Edit tracking
    # -----------------------------
    def _on_segment_edited(self, *_args):
        if self._form_updating:
            return
        self._segments_dirty = True
        self._set_table_status("Unsaved segment edits", ok=True)
        self._refresh_type_hints()

    def _on_num_segments_edited(self, *_args):
        if self._form_updating:
            return
        self._num_segments_dirty = True
        self._set_status("Unsaved segment-count edit", ok=True)

    def _on_control_mode_edited(self, _event=None):
        self._control_mode_dirty = True

    # -----------------------------
    # Actions
    # -----------------------------
    def _on_apply_control_mode(self):
        if self._command_pending:
            return
        zone_id = self._selected_zone_id()
        if not zone_id:
            self._set_status("Select a zone first", ok=False)
            return
        wire = self._CONTROL_MODE_WIRE.get(self.control_mode_var.get())
        if wire is None:
            self._set_status("Choose a control mode first", ok=False)
            return
        def completed(_result, summary, succeeded):
            if succeeded:
                self._control_mode_dirty = False
                self._set_status(f"Zone {zone_id} control mode updated", ok=True)
            else:
                self._set_status(summary, ok=False)

        self._queue_ramp_command(
            {"op": "set_control_mode", "zone": int(zone_id), "mode": wire},
            "Applying control mode…",
            completed,
        )

    def _on_apply_num_segments(self):
        if self._command_pending:
            return
        zone_id = self._selected_zone_id()
        if not zone_id:
            self._set_status("Select a zone first", ok=False)
            return
        count = self._safe_float(self.num_segments_var.get())
        if count is None or count != int(count) or not (0 <= count <= 20):
            self._set_status("Number of segments must be a whole number from 0 to 20", ok=False)
            return
        def completed(_result, summary, succeeded):
            if succeeded:
                self._num_segments_dirty = False
                self._apply_active_row_styling()
                self._set_status(f"Zone {zone_id} segment count set to {int(count)}", ok=True)
            else:
                self._set_status(summary, ok=False)

        self._queue_ramp_command(
            {"op": "set_num_segments", "zone": int(zone_id), "count": int(count)},
            "Applying segment count…",
            completed,
        )

    def _on_start_stop(self, enable: bool, *, all_zones: bool):
        if self._command_pending:
            return
        if all_zones:
            zones = sorted((int(z) for z in self._zone_token_to_id.values()), key=int) or list(range(1, 7))
        else:
            zone_id = self._selected_zone_id()
            if not zone_id:
                self._set_status("Select a zone first", ok=False)
                return
            zones = [int(zone_id)]
        def completed(_result, summary, succeeded):
            if succeeded:
                action = "started" if enable else "stopped"
                target = "all zones" if all_zones else f"zone {zones[0]}"
                self._set_status(f"Ramp/Soak {action} for {target}", ok=True)
            else:
                self._set_status(summary, ok=False)

        self._queue_ramp_command(
            {"op": "set_start_profile", "zones": zones, "enable": enable},
            "Updating Ramp/Soak profile…",
            completed,
        )

    def _on_reload_from_controller(self):
        if self._command_pending:
            return

        def completed(_result, summary, succeeded):
            if not succeeded:
                self._set_table_status(summary, ok=False)
                return
            state, _snapshot = self._state("rampsoak")
            self._latest_rampsoak_zones = get_zones(state, "rampsoak")
            zone_id = self._selected_zone_id()
            if zone_id:
                if not (self._segments_dirty or self._num_segments_dirty):
                    self._populate_for_zone(zone_id, force=True)
                else:
                    self._set_table_status("Reloaded; local edits retained", ok=True)
                    return
            self._set_table_status("Reloaded from controller", ok=True)

        self._queue_ramp_command(
            {"op": "read_rampsoak"},
            "Reloading Ramp/Soak data…",
            completed,
            table=True,
        )

    def _on_save_segments(self):
        if self._command_pending:
            return
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

        def completed(_result, summary, succeeded):
            if succeeded:
                self._segments_dirty = False
                self._set_table_status(f"Saved {len(segments)} segment(s) to zone {zone_id}", ok=True)
            else:
                self._set_table_status(summary, ok=False)

        self._queue_ramp_command(
            {"op": "set_rampsoak_segments", "zone": int(zone_id), "segments": segments},
            "Saving Ramp/Soak segments…",
            completed,
            table=True,
        )

    def _set_ramp_command_pending(self, pending: bool):
        self._command_pending = pending
        state = "disabled" if pending else "normal"
        for button in (
            self.apply_mode_button,
            self.apply_count_button,
            *self.profile_buttons,
            self.reload_button,
            self.save_segments_button,
        ):
            button.configure(state=state)
        self.zone_combo.configure(state="disabled" if pending else "readonly")
        for widget in (
            self.control_mode_combo,
            self.num_segments_entry,
            *(entry for row in self.segment_rows for entry in (row["sp_entry"], row["slope_entry"], row["time_entry"])),
        ):
            widget.configure(state="disabled" if pending else ("readonly" if widget is self.control_mode_combo else "normal"))

    def _queue_ramp_command(self, message: Dict[str, Any], pending_text: str, callback, *, table: bool = False):
        self._set_ramp_command_pending(True)
        status_setter = self._set_table_status if table else self._set_status
        status_setter(pending_text)

        def on_result(result):
            self._set_ramp_command_pending(False)
            summary, succeeded = self._service_result_summary(result)
            callback(result, summary, succeeded)

        try:
            status = self._submit_service([message], on_result)
        except (OSError, RuntimeError, ValueError) as exc:
            self._set_ramp_command_pending(False)
            status_setter(f"Unable to queue service request: {exc}", ok=False)
            return
        if status != "submitted":
            self._set_ramp_command_pending(False)
            status_setter(f"Service request {status}; no command was sent", ok=False)

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
        if self._command_pending:
            return
        try:
            focused = self._safe_focus_get()
            if focused == self.zone_combo:
                return

            state, ramp_snapshot = self._state("rampsoak")
            ts = state.get("ts") if isinstance(state, dict) else None
            self._latest_rampsoak_zones = get_zones(state, "rampsoak")
            zones = self._latest_rampsoak_zones

            if zones:
                self.info_label.config(
                    text=f"Last update: {format_timestamp(ts)} | {self._snapshot_status(ramp_snapshot)}"
                )
            else:
                self.info_label.config(
                    text=f"No ramp/soak data yet - open this tab to fetch it from the controller | "
                         f"{self._snapshot_status(ramp_snapshot)}"
                )

            service_state, _service_snapshot = self._state("service_config")
            zone_names = _load_zone_names_from_state(service_state)
            self._refresh_zone_selector(zone_names)

            zone_id = self._selected_zone_id()
            if zone_id:
                self._populate_for_zone(zone_id, force=False)
            self._update_live_status()
        except Exception:
            LOGGER.exception("RampSoakPanel.refresh failed")
