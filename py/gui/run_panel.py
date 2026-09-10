"""
run_panel.py

"Start Run" mode: in addition to the normal logs directory, mirror log JSONL
into a user-chosen directory for the duration of a run. Supports an optional
scheduled auto-stop time expressed in Pacific time (DST-aware via zoneinfo).

The schedule checkbox/datetime field are write-only controls driven by the
user; refresh() only ever updates the read-only status label from the
service's authoritative run status, so it can never clobber an in-progress
edit (the same class of bug fixed in command_panel.py).
"""

import json
import logging
import socket
import uuid
import tkinter as tk
from tkinter import ttk, filedialog
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from .display_panels import StatePanel
from .state_reader import get_service_config_state


LOGGER = logging.getLogger("cn616a.gui")

_DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def _pacific_tz():
    """Return the Pacific Time zone; requires the 'tzdata' package on Windows."""
    try:
        return ZoneInfo("America/Los_Angeles")
    except Exception:
        LOGGER.warning(
            "America/Los_Angeles tzdata unavailable (install the 'tzdata' package); "
            "falling back to the local machine timezone for run scheduling"
        )
        return datetime.now().astimezone().tzinfo


def _format_elapsed(seconds: float) -> str:
    total = int(max(0.0, seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _format_schedule_display(iso_str: Optional[str]) -> str:
    if not iso_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(iso_str).astimezone(_pacific_tz())
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return iso_str


class RunPanel(StatePanel):
    """Start/stop a logging run mirrored into a custom directory, with optional scheduled auto-stop."""

    def __init__(self, parent, logs_dir: Path, debug: bool = False):
        super().__init__(parent, logs_dir, debug=debug)
        self._create_widgets()

    def _create_widgets(self):
        card = ttk.LabelFrame(self, text="Start Run")
        card.pack(fill=tk.X, padx=10, pady=(8, 4))
        card.columnconfigure(1, weight=1)

        self.run_dir_var = tk.StringVar(value="")
        self.run_name_var = tk.StringVar(value="")
        self.schedule_enabled_var = tk.BooleanVar(value=False)
        self.schedule_dt_var = tk.StringVar(value="")

        ttk.Label(card, text="Run Directory:").grid(row=0, column=0, sticky="e", padx=(8, 6), pady=6)
        self.run_dir_entry = ttk.Entry(card, textvariable=self.run_dir_var, width=50)
        self.run_dir_entry.grid(row=0, column=1, sticky="we", pady=6)
        self.run_dir_entry.bind("<FocusIn>", self._select_all_on_focus)
        ttk.Button(card, text="Browse...", command=self._on_browse).grid(row=0, column=2, sticky="w", padx=(6, 8), pady=6)

        ttk.Label(card, text="Run Name (optional):").grid(row=1, column=0, sticky="e", padx=(8, 6), pady=4)
        self.run_name_entry = ttk.Entry(card, textvariable=self.run_name_var, width=30)
        self.run_name_entry.grid(row=1, column=1, sticky="w", pady=4)
        self.run_name_entry.bind("<FocusIn>", self._select_all_on_focus)

        button_row = ttk.Frame(card)
        button_row.grid(row=2, column=0, columnspan=3, sticky="w", padx=(8, 6), pady=(4, 8))
        self.start_button = tk.Button(
            button_row, text="Start Run", command=self._on_start_run,
            bg="#1f9d3a", fg="white", activebackground="#18822f", activeforeground="white",
            font=("Arial", 10, "bold"), padx=14, pady=6, relief=tk.RAISED, bd=2,
        )
        self.start_button.pack(side=tk.LEFT, padx=(0, 8))
        self.stop_button = tk.Button(
            button_row, text="Stop Run", command=self._on_stop_run,
            bg="#c0392b", fg="white", activebackground="#992d22", activeforeground="white",
            font=("Arial", 10, "bold"), padx=14, pady=6, relief=tk.RAISED, bd=2,
        )
        self.stop_button.pack(side=tk.LEFT)

        schedule_row = ttk.Frame(card)
        schedule_row.grid(row=3, column=0, columnspan=3, sticky="w", padx=(8, 6), pady=(0, 8))
        self.schedule_check = ttk.Checkbutton(
            schedule_row, text="Auto-stop at (Pacific time):",
            variable=self.schedule_enabled_var, command=self._on_schedule_toggle,
        )
        self.schedule_check.pack(side=tk.LEFT)
        self.schedule_dt_entry = ttk.Entry(schedule_row, textvariable=self.schedule_dt_var, width=20)
        self.schedule_dt_entry.pack(side=tk.LEFT, padx=(6, 4))
        self.schedule_dt_entry.bind("<FocusIn>", self._select_all_on_focus)
        self.schedule_dt_entry.bind("<Return>", self._on_schedule_datetime_committed)
        self.schedule_dt_entry.bind("<FocusOut>", self._on_schedule_datetime_committed)
        ttk.Label(schedule_row, text="YYYY-MM-DD HH:MM[:SS]", foreground="gray", font=("Arial", 8)).pack(side=tk.LEFT)

        self.status_label = ttk.Label(card, text="No run active", foreground="gray", font=("Arial", 9), justify=tk.LEFT)
        self.status_label.grid(row=4, column=0, columnspan=3, sticky="w", padx=(8, 6), pady=(0, 8))

    def _select_all_on_focus(self, event=None):
        widget = event.widget if event is not None else None
        if widget is None:
            return
        widget.select_range(0, tk.END)
        widget.icursor(tk.END)

    def _on_browse(self):
        chosen = filedialog.askdirectory(title="Choose run log directory")
        if chosen:
            self.run_dir_var.set(chosen)

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

    def _set_status(self, message: str, *, ok: bool = True):
        self.status_label.config(text=message, foreground=("black" if ok else "red"))

    def _parse_schedule_datetime(self) -> Optional[datetime]:
        text = self.schedule_dt_var.get().strip()
        if not text:
            return None
        for fmt in _DATETIME_FORMATS:
            try:
                naive = datetime.strptime(text, fmt)
            except ValueError:
                continue
            # Attaching ZoneInfo to a naive wall-clock time resolves the correct UTC
            # offset automatically, including across DST transitions.
            return naive.replace(tzinfo=_pacific_tz())
        return None

    def _on_start_run(self):
        run_dir = self.run_dir_var.get().strip()
        if not run_dir:
            self._set_status("Choose a run directory first", ok=False)
            return
        run_name = self.run_name_var.get().strip()
        try:
            resp = self._send_command("start_run", run_dir=run_dir, run_name=run_name or None)
            if resp.get("ok"):
                self._set_status(f"Run started: {run_dir}")
            else:
                self._set_status(f"Start failed: {resp.get('error', 'unknown error')}", ok=False)
        except Exception as e:
            self._set_status(f"Service unreachable: {e}", ok=False)

    def _on_stop_run(self):
        try:
            resp = self._send_command("stop_run")
            if resp.get("ok"):
                self._set_status("Run stopped" if resp.get("was_active") else "No run was active")
            else:
                self._set_status(f"Stop failed: {resp.get('error', 'unknown error')}", ok=False)
        except Exception as e:
            self._set_status(f"Service unreachable: {e}", ok=False)

    def _send_schedule(self, *, enabled: bool, stop_at: Optional[datetime]):
        try:
            resp = self._send_command(
                "set_run_schedule", enabled=enabled,
                stop_at_iso=stop_at.isoformat() if stop_at else None,
            )
            if resp.get("ok"):
                if enabled and stop_at is not None:
                    self._set_status(f"Auto-stop armed for {stop_at.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                else:
                    self._set_status("Auto-stop disabled")
            else:
                self._set_status(f"Schedule update failed: {resp.get('error', 'unknown error')}", ok=False)
        except Exception as e:
            self._set_status(f"Service unreachable: {e}", ok=False)

    def _on_schedule_toggle(self):
        # Checkbutton's variable is already updated by the time this command callback fires.
        enabled = bool(self.schedule_enabled_var.get())
        if not enabled:
            self._send_schedule(enabled=False, stop_at=None)
            return
        stop_at = self._parse_schedule_datetime()
        if stop_at is None:
            self._set_status("Enter a valid schedule time (YYYY-MM-DD HH:MM) before enabling auto-stop", ok=False)
            self.schedule_enabled_var.set(False)
            return
        self._send_schedule(enabled=True, stop_at=stop_at)

    def _on_schedule_datetime_committed(self, _event=None):
        # Only push edits while the checkbox is armed; editing the field while it's
        # unchecked just updates local text with no effect until re-enabled.
        if not self.schedule_enabled_var.get():
            return
        stop_at = self._parse_schedule_datetime()
        if stop_at is None:
            self._set_status("Invalid schedule time; auto-stop not updated", ok=False)
            return
        self._send_schedule(enabled=True, stop_at=stop_at)

    def refresh(self):
        """Poll authoritative run status from the service. Never writes back into
        the editable run/schedule controls - only updates the read-only status line."""
        try:
            resp = self._send_command("get_run_status")
        except Exception as e:
            self.status_label.config(text=f"Service unreachable: {e}", foreground="red")
            return

        try:
            active = bool(resp.get("run_active"))
            if active:
                elapsed = resp.get("run_elapsed_s")
                elapsed_txt = _format_elapsed(elapsed) if isinstance(elapsed, (int, float)) else "?"
                line1 = f"Run ACTIVE - dir: {resp.get('run_dir') or '?'} - elapsed: {elapsed_txt}"
            else:
                line1 = "No run active"

            if resp.get("run_schedule_enabled"):
                line2 = f"Auto-stop: armed for {_format_schedule_display(resp.get('run_schedule_stop_at'))}"
            else:
                line2 = "Auto-stop: disabled"

            self.status_label.config(text=f"{line1}\n{line2}", foreground="black")
        except Exception:
            LOGGER.exception("RunPanel.refresh failed to render status")
