"""
command_panel.py

Writable command controls for CN616A telemetry tab.
Separated from read-only telemetry display to keep concerns isolated.
"""

import json
import logging
import socket
import traceback
import uuid
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any, Dict, Optional

from .display_panels import StatePanel
from .state_reader import get_service_config_state, get_telemetry_state, safe_get


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


class CommandPanel(StatePanel):
    """Write command controls for SP/mode/autotune in the telemetry tab."""

    def __init__(self, parent, logs_dir: Path, debug: bool = False):
        super().__init__(parent, logs_dir, debug=debug)
        self._latest_zones_data: Dict[str, Dict[str, Any]] = {}
        self._zone_token_to_id: Dict[str, str] = {}
        self._form_dirty = False
        self._create_widgets()

    def _create_widgets(self):
        self.command_card = ttk.LabelFrame(self, text="Zone Commands")
        self.command_card.pack(fill=tk.X, padx=10, pady=(8, 4))
        self.command_card.columnconfigure(4, weight=1)

        self.unit_var = tk.StringVar(value="1")
        self.zone_var = tk.StringVar(value="")
        self.sp_abs_var = tk.StringVar(value="")
        self.mode_var = tk.StringVar(value="PID")
        self.sp_autotune_var = tk.StringVar(value="")

        ttk.Label(self.command_card, text="Unit:").grid(row=0, column=0, sticky="e", padx=(8, 6), pady=6)
        self.unit_combo = ttk.Combobox(
            self.command_card,
            textvariable=self.unit_var,
            state="readonly",
            width=10,
            values=[str(i) for i in range(1, 9)],
        )
        self.unit_combo.grid(row=0, column=1, sticky="w", pady=6)

        ttk.Label(self.command_card, text="Zone:").grid(row=0, column=2, sticky="e", padx=(12, 6), pady=6)
        self.zone_combo = ttk.Combobox(
            self.command_card,
            textvariable=self.zone_var,
            state="readonly",
            width=22,
            values=[],
        )
        self.zone_combo.grid(row=0, column=3, sticky="w", pady=6)
        self.zone_combo.bind("<<ComboboxSelected>>", self._on_zone_selected)

        ttk.Label(self.command_card, text="SP Abs (C):").grid(row=1, column=0, sticky="e", padx=(8, 6), pady=4)
        self.sp_abs_entry = ttk.Entry(self.command_card, textvariable=self.sp_abs_var, width=14)
        self.sp_abs_entry.grid(row=1, column=1, sticky="w", pady=4)
        self.sp_abs_entry.bind("<KeyRelease>", self._on_form_edited)

        ttk.Label(self.command_card, text="Mode:").grid(row=1, column=2, sticky="e", padx=(12, 6), pady=4)
        self.mode_combo = ttk.Combobox(
            self.command_card,
            textvariable=self.mode_var,
            state="readonly",
            width=20,
            values=["PID", "ON/OFF"],
        )
        self.mode_combo.grid(row=1, column=3, sticky="w", pady=4)
        self.mode_combo.bind("<<ComboboxSelected>>", self._on_form_edited)

        ttk.Label(self.command_card, text="SP Autotune (C):").grid(row=2, column=0, sticky="e", padx=(8, 6), pady=4)
        self.sp_autotune_entry = ttk.Entry(self.command_card, textvariable=self.sp_autotune_var, width=14)
        self.sp_autotune_entry.grid(row=2, column=1, sticky="w", pady=4)
        self.sp_autotune_entry.bind("<KeyRelease>", self._on_form_edited)

        self.apply_button = tk.Button(
            self.command_card,
            text="Send Zone Updates",
            command=self._on_send_zone_updates,
            bg="#1f9d3a",
            fg="white",
            activebackground="#18822f",
            activeforeground="white",
            font=("Arial", 11, "bold"),
            padx=16,
            pady=8,
            relief=tk.RAISED,
            bd=2,
        )
        self.apply_button.grid(row=2, column=4, sticky="e", padx=(28, 12), pady=4)

        self.start_autotune_button = ttk.Button(self.command_card, text="Start Autotune", command=self._on_start_autotune)
        self.start_autotune_button.grid(row=3, column=2, sticky="e", padx=(12, 6), pady=(4, 8))

        self.stop_autotune_button = ttk.Button(self.command_card, text="Stop Autotune", command=self._on_stop_autotune)
        self.stop_autotune_button.grid(row=3, column=3, sticky="w", pady=(4, 8))

        self.status_label = ttk.Label(self.command_card, text="", foreground="gray", font=("Arial", 9))
        self.status_label.grid(row=3, column=0, columnspan=2, sticky="w", padx=(8, 6), pady=(4, 8))

    def _set_status(self, message: str, *, ok: bool = True):
        self.status_label.config(text=message, foreground=("green" if ok else "red"))

    def _on_form_edited(self, _event=None):
        self._form_dirty = True
        self._set_status("Unsaved command edits", ok=True)

    def _safe_float(self, value: str) -> Optional[float]:
        text = str(value).strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _safe_focus_get(self):
        try:
            return self.focus_get()
        except Exception:
            return None

    def on_tab_selected(self):
        """Called by main GUI when telemetry tab is shown again."""
        try:
            self.refresh()
        except Exception:
            LOGGER.exception("CommandPanel.on_tab_selected failed")

    def _selected_zone_id(self) -> Optional[str]:
        token = self.zone_var.get().strip()
        if not token:
            return None
        return self._zone_token_to_id.get(token)

    def _mode_to_wire_value(self, mode_label: str) -> str:
        if str(mode_label).strip().upper() == "ON/OFF":
            return "ON_OFF_CONTROL"
        return "PID_CONTROL"

    def _mode_label_from_zone(self, zone_data: Dict[str, Any]) -> str:
        value = str(safe_get(zone_data, "control_method", default="") or "").upper()
        if "ON_OFF" in value:
            return "ON/OFF"
        return "PID"

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
        msg = {
            "id": uuid.uuid4().hex[:8],
            "op": op,
            "unit": int(self.unit_var.get() or 1),
        }
        msg.update(fields)

        data = (json.dumps(msg) + "\n").encode("utf-8")
        with socket.create_connection((host, port), timeout=2.0) as s:
            s.sendall(data)
            s.settimeout(2.0)
            resp = s.recv(65536).decode("utf-8", errors="ignore").strip()
        return json.loads(resp) if resp else {"ok": False, "error": "empty response"}

    def _populate_fields_for_zone(self, zone_id: str, *, force: bool = False):
        zone_data = self._latest_zones_data.get(str(zone_id), {})
        if not isinstance(zone_data, dict):
            return
        if self._form_dirty and not force:
            return

        sp_abs = safe_get(zone_data, "sp_abs", default=None)
        autotune_sp = safe_get(zone_data, "autotune_sp", default=None)
        if isinstance(sp_abs, (int, float)):
            self.sp_abs_var.set(f"{float(sp_abs):.3f}".rstrip("0").rstrip("."))
        else:
            self.sp_abs_var.set("")
        if isinstance(autotune_sp, (int, float)):
            self.sp_autotune_var.set(f"{float(autotune_sp):.3f}".rstrip("0").rstrip("."))
        else:
            self.sp_autotune_var.set("")
        self.mode_var.set(self._mode_label_from_zone(zone_data))
        self._form_dirty = False
        self._sync_autotune_buttons(zone_data)

    def _sync_autotune_buttons(self, zone_data: Optional[Dict[str, Any]] = None):
        if zone_data is None:
            zone_id = self._selected_zone_id()
            zone_data = self._latest_zones_data.get(zone_id or "", {})

        autotune_state = str(safe_get(zone_data or {}, "autotune_enable", default="") or "").upper()
        if autotune_state == "ENABLE":
            self.start_autotune_button.state(["disabled"])
            self.stop_autotune_button.state(["!disabled"])
        elif autotune_state == "DISABLE":
            self.start_autotune_button.state(["!disabled"])
            self.stop_autotune_button.state(["disabled"])
        else:
            self.start_autotune_button.state(["!disabled"])
            self.stop_autotune_button.state(["!disabled"])

    def _refresh_zone_selector(self, zone_names: Dict[str, str]):
        sorted_zone_ids = sorted(
            [str(z) for z in self._latest_zones_data.keys()],
            key=lambda x: int(x) if str(x).isdigit() else 999,
        )

        tokens = []
        mapping = {}
        for zone_id in sorted_zone_ids:
            token = f"{zone_id} - {zone_names.get(zone_id, f'Zone {zone_id}') }"
            tokens.append(token)
            mapping[token] = zone_id

        self._zone_token_to_id = mapping
        self.zone_combo.configure(values=tokens)

        if not tokens:
            self.zone_var.set("")
            self.apply_button.configure(state=tk.DISABLED)
            self.start_autotune_button.state(["disabled"])
            self.stop_autotune_button.state(["disabled"])
            return

        if self.zone_var.get() not in mapping:
            self.zone_var.set(tokens[0])
            self._populate_fields_for_zone(mapping[tokens[0]], force=True)

        self.apply_button.configure(state=tk.NORMAL)
        self._sync_autotune_buttons()

    def _on_zone_selected(self, _event=None):
        zone_id = self._selected_zone_id()
        if zone_id:
            # Explicitly drop unsent edits when switching zones.
            self._form_dirty = False
            self._populate_fields_for_zone(zone_id, force=True)
            self._set_status("Zone changed, unsent edits discarded", ok=True)

    def _on_send_zone_updates(self):
        zone_id = self._selected_zone_id()
        if not zone_id:
            self._set_status("Select a zone first", ok=False)
            return

        sp_abs = self._safe_float(self.sp_abs_var.get())
        autotune_sp = self._safe_float(self.sp_autotune_var.get())
        mode = self._mode_to_wire_value(self.mode_var.get())
        if sp_abs is None or autotune_sp is None:
            self._set_status("SP Abs and SP Autotune must be numeric", ok=False)
            return

        ops = [
            ("set_sp_abs", {"zone": int(zone_id), "value_c": sp_abs}),
            ("set_control_method", {"zone": int(zone_id), "method": mode}),
            ("set_autotune_setpoint", {"zone": int(zone_id), "value_c": autotune_sp}),
        ]
        try:
            for op, payload in ops:
                resp = self._send_command(op, **payload)
                if not resp.get("ok"):
                    self._set_status(f"{op} failed: {resp.get('error', 'unknown error')}", ok=False)
                    return
            self._form_dirty = False
            self._set_status(f"Zone {zone_id} updates sent", ok=True)
        except Exception as e:
            self._set_status(f"Service unreachable: {e}", ok=False)

    def _on_start_autotune(self):
        zone_id = self._selected_zone_id()
        if not zone_id:
            self._set_status("Select a zone first", ok=False)
            return
        try:
            resp = self._send_command("start_autotune", zones=[int(zone_id)])
            if resp.get("ok"):
                self._set_status(f"Zone {zone_id} autotune started", ok=True)
                self.start_autotune_button.state(["disabled"])
                self.stop_autotune_button.state(["!disabled"])
            else:
                self._set_status(f"Start failed: {resp.get('error', 'unknown error')}", ok=False)
        except Exception as e:
            self._set_status(f"Service unreachable: {e}", ok=False)

    def _on_stop_autotune(self):
        zone_id = self._selected_zone_id()
        if not zone_id:
            self._set_status("Select a zone first", ok=False)
            return
        try:
            resp = self._send_command("stop_autotune", zone=int(zone_id))
            if resp.get("ok"):
                self._set_status(f"Zone {zone_id} autotune stopped", ok=True)
                self.start_autotune_button.state(["!disabled"])
                self.stop_autotune_button.state(["disabled"])
            else:
                self._set_status(f"Stop failed: {resp.get('error', 'unknown error')}", ok=False)
        except Exception as e:
            self._set_status(f"Service unreachable: {e}", ok=False)

    def refresh(self):
        try:
            focused = self._safe_focus_get()
            if focused in (self.zone_combo, self.unit_combo):
                # Do not mutate combobox values while user is interacting.
                return

            telem_state = get_telemetry_state(self.logs_dir)
            zone_names = _load_zone_names_from_logs(self.logs_dir)
            telemetry = telem_state.get("telemetry", {}) if isinstance(telem_state, dict) else {}
            zones_data = telemetry.get("zones", {}) if isinstance(telemetry, dict) else {}
            self._latest_zones_data = zones_data if isinstance(zones_data, dict) else {}

            try:
                unit_value = str(telem_state.get("unit", "") or "")
                if unit_value:
                    self.unit_var.set(unit_value)
            except Exception:
                pass

            self._refresh_zone_selector(zone_names)
            selected_zone = self._selected_zone_id()
            if selected_zone:
                self._populate_fields_for_zone(selected_zone, force=False)
        except Exception:
            LOGGER.exception("CommandPanel.refresh failed")
            if self.debug:
                print(traceback.format_exc())
