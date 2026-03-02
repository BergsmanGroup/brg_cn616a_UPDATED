"""
cn616a_service.py

Single-owner service for CN616A Modbus RTU:
- Owns the COM port (one process talks Modbus)
- Polls telemetry on a schedule (append JSONL + overwrite latest state JSON)
- Polls config on a slower schedule (append JSONL + overwrite latest state JSON)
- Reads ramp/soak tables on demand (append JSONL + overwrite latest state JSON)
- Accepts commands over localhost TCP as JSON Lines (one JSON object per line)

Folder layout (repo convention):
  repo_root/
    py/
      cn616a_service.py
      cn616a.py
      cn616a_cli.py
    logs/
      *.json
      *.jsonl
    cn616a_register_map.json

Outputs (in out_dir, default repo_root/logs):
  Telemetry:
    - cn616a_telemetry_state.json
    - cn616a_telemetry_log.jsonl
  Config:
    - cn616a_config_state.json
    - cn616a_config_log.jsonl
  Ramp/Soak:
    - cn616a_rampsoak_state.json
    - cn616a_rampsoak_log.jsonl
  Service config:
    - cn616a_service_config_state.json
    - cn616a_service_config_log.jsonl

Protocol (JSON Lines):
  Client sends one JSON object per line:
    {"id":"abcd1234","op":"set_sp_abs","zone":1,"value_c":80.0}
  Service replies with one JSON object per line:
    {"id":"abcd1234","ok":true}

Notes:
- State JSON is always a *complete latest snapshot* (atomic overwrite)
- Log JSONL is append-only (one JSON object per line)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty
from typing import Any, Dict, Optional, Sequence, Tuple, Union
from collections import deque

from cn616a import CN616A, CN616AError, SerialParams


# -----------------------------
# Time / IO helpers
# -----------------------------
def now_iso_local_ms() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def atomic_write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, obj: dict, *, flush_each_line: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, separators=(",", ":")) + "\n")
        if flush_each_line:
            f.flush()


def stable_hash(obj: Any) -> str:
    b = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(b).hexdigest()


# -----------------------------
# Command server (JSONL over TCP)
# -----------------------------
@dataclass
class CommandRequest:
    cmd: dict
    done: threading.Event = field(default_factory=threading.Event)
    result: Optional[dict] = None


def command_server(
    host: str,
    port: int,
    cmd_q: Queue,
    stop_evt: threading.Event,
    verbose: bool = False,
):
    """
    Robust JSONL TCP server:
    - Accepts client connections
    - Reads JSON objects delimited by newline
    - Enqueues them to service thread
    - Waits for completion and replies with JSON object per line
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(5)
    srv.settimeout(0.5)

    #if verbose:
    if True: #always print this info
        print(f"[service] command server listening on {host}:{port}")

    while not stop_evt.is_set():
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break

        #if verbose:
        if True: #always print this info
            print(f"[service] client connected: {addr}")

        conn.settimeout(5.0)
        with conn:
            buf = b""
            while not stop_evt.is_set():
                try:
                    chunk = conn.recv(4096)
                except (ConnectionAbortedError, ConnectionResetError, OSError):
                    # Normal on Windows if client closes or AV/firewall intervenes.
                    break
                except socket.timeout:
                    break

                if not chunk:
                    break

                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        cmd = json.loads(line.decode("utf-8"))
                    except Exception as e:
                        resp = {"ok": False, "error": f"Bad JSON: {e}"}
                        try:
                            conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                        except Exception:
                            pass
                        continue

                    req = CommandRequest(cmd=cmd)
                    cmd_q.put(req)

                    timeout_s = float(cmd.get("timeout_s", 5.0))
                    finished = req.done.wait(timeout=timeout_s)
                    if not finished:
                        resp = {"id": cmd.get("id"), "ok": False, "error": "Command timed out waiting for completion"}
                    else:
                        resp = req.result or {"id": cmd.get("id"), "ok": False, "error": "No result returned"}

                    try:
                        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                    except Exception:
                        break

    try:
        srv.close()
    except Exception:
        pass


# -----------------------------
# Service config model
# -----------------------------
@dataclass
class ServiceConfig:
    # Polling cadence (Hz). Set to 0 to disable a poller.
    telemetry_hz: float = 2.0
    config_hz: float = 0.2
    rampsoak_hz: float = 0.0  # usually on-demand only; keep 0 unless you want periodic snapshots
    analysis_hz: float = 1.0
    gui_refresh_hz: float = 2.0
    equilibrium_window_s: float = 30.0
    equilibrium_threshold_c: float = 0.25

    # Zone selection
    zones_mode: str = "auto"  # "auto" or "list"
    zones_list: Sequence[int] = field(default_factory=lambda: [1, 2, 3, 4, 5, 6])

    # Logging
    flush_each_line: bool = True

    # Viewer settings for GUI (history, colors, line width)
    viewer_history_hours: float = 1.0
    viewer_line_width: float = 2.5
    viewer_pv_color: str = "blue"
    viewer_sp_color: str = "red"
    viewer_sp_autotune_color: str = "purple"

    # Last successful connection info
    last_serial_port: str = ""
    last_serial_params: dict = field(default_factory=lambda: {
        "baudrate": 115200,
        "parity": "N",
        "stopbits": 1,
        "bytesize": 8,
        "timeout": 1.0,
    })
    last_tcp_host: str = ""
    last_tcp_port: int = 0

    def effective_zones(self) -> list[int]:
        if self.zones_mode == "auto":
            return [1, 2, 3, 4, 5, 6]
        return sorted({int(z) for z in self.zones_list if 1 <= int(z) <= 6})

    def to_dict(self) -> dict:
        return {
            "telemetry_hz": self.telemetry_hz,
            "config_hz": self.config_hz,
            "rampsoak_hz": self.rampsoak_hz,
            "zones_mode": self.zones_mode,
            "zones_list": list(self.zones_list),
            "flush_each_line": self.flush_each_line,
            "analysis_hz": self.analysis_hz,
            "gui_refresh_hz": self.gui_refresh_hz,
            "equilibrium_window_s": self.equilibrium_window_s,
            "equilibrium_threshold_c": self.equilibrium_threshold_c,
            "last_serial_port": self.last_serial_port,
            "last_serial_params": self.last_serial_params,
            "last_tcp_host": self.last_tcp_host,
            "last_tcp_port": self.last_tcp_port,
            # viewer preferences
            "viewer_history_hours": self.viewer_history_hours,
            "viewer_line_width": self.viewer_line_width,
            "viewer_pv_color": self.viewer_pv_color,
            "viewer_sp_color": self.viewer_sp_color,
            "viewer_sp_autotune_color": self.viewer_sp_autotune_color,
        }

    @staticmethod
    def from_dict(d: dict) -> "ServiceConfig":
        cfg = ServiceConfig()
        for k in ["telemetry_hz","config_hz","rampsoak_hz","analysis_hz","gui_refresh_hz",
          "equilibrium_window_s","equilibrium_threshold_c",
          "zones_mode","zones_list","flush_each_line",
          "last_serial_port","last_serial_params","last_tcp_host","last_tcp_port",
          "viewer_history_hours","viewer_line_width",
          "viewer_pv_color","viewer_sp_color","viewer_sp_autotune_color"]:
            if k in d:
                setattr(cfg, k, d[k])
        # normalize
        cfg.telemetry_hz = float(cfg.telemetry_hz)
        cfg.config_hz = float(cfg.config_hz)
        cfg.rampsoak_hz = float(cfg.rampsoak_hz)
        cfg.zones_mode = str(cfg.zones_mode)
        cfg.zones_list = list(cfg.zones_list) if isinstance(cfg.zones_list, (list, tuple)) else [1, 2, 3, 4, 5, 6]
        cfg.flush_each_line = bool(cfg.flush_each_line)
        cfg.analysis_hz = float(cfg.analysis_hz)
        cfg.gui_refresh_hz = float(cfg.gui_refresh_hz)
        cfg.equilibrium_window_s = float(cfg.equilibrium_window_s)
        cfg.equilibrium_threshold_c = float(cfg.equilibrium_threshold_c)
        # ensure serial_params keys
        if not isinstance(cfg.last_serial_params, dict):
            cfg.last_serial_params = {}
        return cfg


def hz_to_period_s(hz: float) -> float:
    hz = float(hz)
    if hz <= 0:
        return 1e9
    return 1.0 / hz


# -----------------------------
# CN616AService
# -----------------------------
class CN616AService:
    def __init__(
        self,
        *,
        port: str,
        unit: int,
        map_path: str,
        out_dir: Path,
        tcp_host: str,
        tcp_port: int,
        cfg: ServiceConfig,
        verbose: bool = False,
    ):
        self.port = port
        self.unit = int(unit)
        self.map_path = str(map_path)
        self.out_dir = Path(out_dir)
        self.tcp_host = tcp_host
        self.tcp_port = int(tcp_port)
        self.verbose = bool(verbose)

        self.cfg = cfg
        self.zones_enabled = self.cfg.effective_zones()
        self._err_buf = {z: deque() for z in range(1, 7)}  # stores (t_monotonic, abs_err_c, pv, sp)
        self._last_sp_abs = {z: None for z in range(1, 7)}  # SP cache (degC)

        # IO paths
        self.telemetry_state_path = self.out_dir / "cn616a_telemetry_state.json"
        self.telemetry_log_path = self.out_dir / "cn616a_telemetry_log.jsonl"

        self.config_state_path = self.out_dir / "cn616a_config_state.json"
        self.config_log_path = self.out_dir / "cn616a_config_log.jsonl"

        self.rampsoak_state_path = self.out_dir / "cn616a_rampsoak_state.json"
        self.rampsoak_log_path = self.out_dir / "cn616a_rampsoak_log.jsonl"

        self.svc_cfg_state_path = self.out_dir / "cn616a_service_config_state.json"
        self.svc_cfg_log_path = self.out_dir / "cn616a_service_config_log.jsonl"

        self.analysis_state_path = self.out_dir / "cn616a_analysis_state.json"
        self.analysis_log_path   = self.out_dir / "cn616a_analysis_log.jsonl"
        self._last_analysis_hash: Optional[str] = None

        # state hashes (to avoid noisy config logs)
        self._last_config_hash: Optional[str] = None
        self._last_rampsoak_hash: Optional[str] = None

        # status / errors
        self._connected = False
        self._last_err: Optional[str] = None
        self._last_telemetry_ts: Optional[str] = None
        self._last_cycle_ms: Optional[int] = None

        # command infra
        self.cmd_q: Queue = Queue()
        self.stop_evt = threading.Event()
        self._cmd_thread = threading.Thread(
            target=command_server,
            args=(self.tcp_host, self.tcp_port, self.cmd_q, self.stop_evt, self.verbose),
            daemon=True,
        )

        # driver
        self.ctl = CN616A(port=self.port, unit=self.unit, register_map_path=self.map_path)

        # write initial service config snapshot + log
        self._log_service_config(event="startup", patch={})

    def _log_service_config(self, *, event: str, patch: dict, client: Optional[Tuple[str, int]] = None) -> None:
        snap = {
            "ts": now_iso_local_ms(),
            "event": event,
            "client": {"host": client[0], "port": client[1]} if client else None,
            "config": self.cfg.to_dict(),
        }
        atomic_write_json(self.svc_cfg_state_path, snap)
        append_jsonl(self.svc_cfg_log_path, snap, flush_each_line=self.cfg.flush_each_line)

    def _apply_connection_settings_to_ctl(self) -> None:
        """Apply configured serial connection settings to driver before connect/reconnect."""
        configured_port = str(self.cfg.last_serial_port or self.port)
        self.port = configured_port

        params_raw = self.cfg.last_serial_params if isinstance(self.cfg.last_serial_params, dict) else {}
        try:
            serial_params = SerialParams(
                baudrate=int(params_raw.get("baudrate", 115200)),
                parity=str(params_raw.get("parity", "N")),
                stopbits=int(params_raw.get("stopbits", 1)),
                bytesize=int(params_raw.get("bytesize", 8)),
                timeout=float(params_raw.get("timeout", 1.0)),
            )
        except Exception:
            serial_params = SerialParams()

        self.ctl.port = configured_port
        self.ctl.serial = serial_params

    def connect(self) -> None:
        self._apply_connection_settings_to_ctl()
        #if self.verbose:
        if True: #always print this info
            print(f"[service] connecting to CN616A on {self.port} (unit={self.unit})...")
        self.ctl.connect()
        self._connected = True
        # update service config with successful connection info
        try:
            self.cfg.last_serial_port = self.port
            params = self.ctl.serial
            # copy only relevant attributes
            self.cfg.last_serial_params = {
                "baudrate": params.baudrate,
                "parity": params.parity,
                "stopbits": params.stopbits,
                "bytesize": params.bytesize,
                "timeout": params.timeout,
            }
            self.cfg.last_tcp_host = self.tcp_host
            self.cfg.last_tcp_port = self.tcp_port
            # log the updated config snapshot
            self._log_service_config(event="connect", patch={})
        except Exception:
            pass
        #if self.verbose:
        if True: #always print this info
            print("[service] connected")

    def close(self) -> None:
        try:
            self.ctl.close()
        except Exception:
            pass
        self._connected = False

    def restart_serial(self) -> None:
        self.close()
        time.sleep(0.2)
        self.ctl = CN616A(port=self.port, unit=self.unit, register_map_path=self.map_path)
        self._apply_connection_settings_to_ctl()
        self.connect()

    def reload_register_map(self) -> None:
        # Rebuild ctl with same serial params, new map load
        was_connected = self._connected
        if was_connected:
            self.close()
            time.sleep(0.2)
        self.ctl = CN616A(port=self.port, unit=self.unit, register_map_path=self.map_path)
        self._apply_connection_settings_to_ctl()
        if was_connected:
            self.connect()

    # -----------------------------
    # Pollers
    # -----------------------------
    def poll_telemetry(self) -> Dict[str, Any]:
        """
        Poll telemetry from controller, write:
        - cn616a_telemetry_state.json (snapshot)
        - cn616a_telemetry_log.jsonl (append)

        Updates rolling buffers for poll_analysis():
        self._err_buf[z] stores: (t_monotonic, abs_err_c, pv_c, sp_c)

        Robust to different telemetry dict shapes:
        A) {"zones": {"1": {...}, ...}}
        B) {"1": {...}, "2": {...}}   (or int keys)
        C) {"z1": {...}, ...}

        SP is taken from telemetry if present; otherwise uses cached SP from poll_config().
        """
        zones = self.zones_enabled

        data = self.ctl.read_telemetry(zones)

        state_obj = {
            "ts": now_iso_local_ms(),
            "unit": self.unit,
            "port": self.port,
            "zones": zones,
            "telemetry": data,
        }
        atomic_write_json(self.telemetry_state_path, state_obj)
        append_jsonl(self.telemetry_log_path, state_obj, flush_each_line=self.cfg.flush_each_line)

        def get_zone_block(container: Any, z: int) -> dict:
            if not isinstance(container, dict):
                return {}
            for k in (str(z), z, f"z{z}", f"Z{z}", f"zone{z}", f"ZONE{z}"):
                if k in container and isinstance(container[k], dict):
                    return container[k] or {}
            return {}

        # Find zone telemetry dict
        zones_tel = {}
        if isinstance(data, dict):
            if "zones" in data and isinstance(data["zones"], dict):
                zones_tel = data["zones"]
            else:
                zones_tel = data

        t = time.monotonic()

        # Optional: PV cache for debugging/alternate analysis later
        if not hasattr(self, "_last_pv"):
            self._last_pv = {z: None for z in range(1, 7)}

        for z in zones:
            zd = get_zone_block(zones_tel, z)

            # PV keys
            pv = None
            for k in ("pv_c", "pv", "PV", "process_value_c", "process_value"):
                if k in zd and zd[k] is not None:
                    pv = zd[k]
                    break

            # SP keys (telemetry may not include SP)
            sp = None
            for k in ("sp_abs_c", "sp_abs", "sp_c", "sp", "SP_abs", "SP", "setpoint_c", "setpoint"):
                if k in zd and zd[k] is not None:
                    sp = zd[k]
                    break

            if pv is None:
                continue

            try:
                pv_f = float(pv)
            except Exception:
                continue

            self._last_pv[z] = pv_f

            # Fallback SP to cached value from config poll
            if sp is None:
                sp = self._last_sp_abs.get(z, None)

            if sp is None:
                # still can't compute error without SP
                continue

            try:
                sp_f = float(sp)
            except Exception:
                continue

            abs_err = abs(pv_f - sp_f)
            self._err_buf[z].append((t, abs_err, pv_f, sp_f))

        # Prune old buffer entries
        keep_s = max(5.0, float(self.cfg.equilibrium_window_s) * 2.0)
        cutoff = t - keep_s
        for z in zones:
            dq = self._err_buf.get(z)
            if not dq:
                continue
            while dq and dq[0][0] < cutoff:
                dq.popleft()

        self._last_telemetry_ts = state_obj["ts"]
        return state_obj

    def poll_config(self) -> Optional[Dict[str, Any]]:
        """
        Poll configuration from controller, write:
        - cn616a_config_state.json (snapshot)
        - cn616a_config_log.jsonl (append only when config changes)

        Also updates SP cache for analysis:
        self._last_sp_abs[z] = latest setpoint (degC) for each zone.

        Robust to different config dict shapes:
        A) {"zones": {"1": {...}, ...}}
        B) {"1": {...}, "2": {...}}   (or int keys)
        C) {"z1": {...}, "z2": {...}}
        """
        zones = self.zones_enabled

        data = self.ctl.read_config(zones)

        def get_zone_block(container: Any, z: int) -> dict:
            if not isinstance(container, dict):
                return {}
            # Try common key forms
            for k in (str(z), z, f"z{z}", f"Z{z}", f"zone{z}", f"ZONE{z}"):
                if k in container and isinstance(container[k], dict):
                    return container[k] or {}
            return {}

        # Identify where zone dicts live
        zones_cfg = {}
        if isinstance(data, dict):
            if "zones" in data and isinstance(data["zones"], dict):
                zones_cfg = data["zones"]
            else:
                # If top-level looks like it contains zone blocks, use it as zones_cfg
                # (covers {"1": {...}} or {"z1": {...}})
                zones_cfg = data

        # Update SP cache
        for z in zones:
            zd = get_zone_block(zones_cfg, z)

            sp = None
            # Try likely SP keys
            for k in ("sp_abs_c", "sp_abs", "sp_c", "sp", "setpoint_c", "setpoint", "SP_abs", "SP"):
                if k in zd and zd[k] is not None:
                    sp = zd[k]
                    break

            if sp is not None:
                try:
                    self._last_sp_abs[z] = float(sp)
                except Exception:
                    pass  # keep old cached value

        state_obj = {
            "ts": now_iso_local_ms(),
            "unit": self.unit,
            "port": self.port,
            "zones": zones,
            "config": data,
        }

        h = stable_hash(state_obj["config"])
        changed = (self._last_config_hash != h)
        self._last_config_hash = h

        atomic_write_json(self.config_state_path, state_obj)

        if changed:
            append_jsonl(self.config_log_path, state_obj, flush_each_line=self.cfg.flush_each_line)
            return state_obj

        return None

    def poll_rampsoak(self) -> Optional[Dict[str, Any]]:
        zones = self.zones_enabled
        data = self.ctl.read_rampsoak_all(zones)

        state_obj = {
            "ts": now_iso_local_ms(),
            "unit": self.unit,
            "port": self.port,
            "zones": zones,
            "rampsoak": data,
        }
        h = stable_hash(state_obj["rampsoak"])
        changed = (self._last_rampsoak_hash != h)
        self._last_rampsoak_hash = h

        atomic_write_json(self.rampsoak_state_path, state_obj)
        if changed:
            append_jsonl(self.rampsoak_log_path, state_obj, flush_each_line=self.cfg.flush_each_line)
        return state_obj if changed else None

    def poll_analysis(self) -> Dict[str, Any]:
        now_m = time.monotonic()
        window = float(self.cfg.equilibrium_window_s)
        thr = float(self.cfg.equilibrium_threshold_c)

        out = {}
        for z in self.zones_enabled:
            dq = self._err_buf[z]
            # samples within window
            recent = [item for item in dq if item[0] >= now_m - window]
            if not recent:
                out[str(z)] = {
                    "avg_abs_error_c": None,
                    "in_equilibrium": False,
                    "n_points": 0,
                    "threshold_c": thr,
                    "window_s": window,
                    "last_pv_c": None,
                    "last_sp_c": None,
                    "last_sp_c": self._last_sp_abs.get(z, None),  # <--- THIS
                    "last_pv_c": getattr(self, "_last_pv", {}).get(z, None),

                }
                continue

            avg_err = sum(x[1] for x in recent) / len(recent)
            last_pv = recent[-1][2]
            last_sp = recent[-1][3]
            out[str(z)] = {
                "avg_abs_error_c": avg_err,
                "in_equilibrium": avg_err <= thr,
                "n_points": len(recent),
                "threshold_c": thr,
                "window_s": window,
                "last_pv_c": last_pv,
                "last_sp_c": last_sp,
            }

        state_obj = {
            "ts": now_iso_local_ms(),
            "unit": self.unit,
            "port": self.port,
            "zones": self.zones_enabled,
            "analysis": out,
        }

        atomic_write_json(self.analysis_state_path, state_obj)

        # Optional: only log when the analysis meaningfully changes
        h = stable_hash(state_obj["analysis"])
        if h != self._last_analysis_hash:
            self._last_analysis_hash = h
            append_jsonl(self.analysis_log_path, state_obj, flush_each_line=self.cfg.flush_each_line)

        return state_obj

    # -----------------------------
    # Command execution
    # -----------------------------
    def handle_command(self, cmd: dict) -> dict:
        cid = cmd.get("id")
        op = cmd.get("op")

        try:
            if op == "ping":
                return {"id": cid, "ok": True, "pong": True}

            # ---- service config ops (GUI panel) ----
            if op == "get_service_config":
                return {"id": cid, "ok": True, "service_config": self.cfg.to_dict(), "zones_enabled": self.zones_enabled}

            if op == "set_service_config":
                patch = cmd.get("patch", {})
                if not isinstance(patch, dict):
                    return {"id": cid, "ok": False, "error": "patch must be a JSON object"}

                new_cfg = ServiceConfig.from_dict({**self.cfg.to_dict(), **patch})
                self.cfg = new_cfg
                self.zones_enabled = self.cfg.effective_zones()
                self._log_service_config(event="set_service_config", patch=patch)
                return {"id": cid, "ok": True, "service_config": self.cfg.to_dict(), "zones_enabled": self.zones_enabled}

            if op == "connect_serial":
                if not self._connected:
                    self.connect()
                return {"id": cid, "ok": True, "connected": self._connected}

            if op == "disconnect_serial":
                if self._connected:
                    self.close()
                self._log_service_config(event="disconnect", patch={})
                return {"id": cid, "ok": True, "connected": self._connected}

            if op == "refresh_connection":
                self.restart_serial()
                return {"id": cid, "ok": True, "connected": self._connected}

            if op == "get_status":
                return {
                    "id": cid,
                    "ok": True,
                    "connected": self._connected,
                    "last_telemetry_ts": self._last_telemetry_ts,
                    "last_error": self._last_err,
                    "cycle_ms": self._last_cycle_ms,
                    "zones_enabled": self.zones_enabled,
                    "service_config": self.cfg.to_dict(),
                }

            if op == "reload_register_map":
                self.reload_register_map()
                return {"id": cid, "ok": True}

            if op == "restart_serial":
                self.restart_serial()
                return {"id": cid, "ok": True}

            if op == "shutdown":
                self.stop_evt.set()
                return {"id": cid, "ok": True, "shutting_down": True}

            # ---- existing controller ops ----
            if op == "set_sp_abs":
                zone = int(cmd["zone"])
                value_c = float(cmd["value_c"])
                self.ctl.set_sp_abs(zone, value_c)
                return {"id": cid, "ok": True}

            if op == "set_control_method":
                zone = int(cmd["zone"])
                method = cmd["method"]
                self.ctl.set_control_method(zone, method)
                return {"id": cid, "ok": True}

            if op == "set_control_mode":
                zone = int(cmd["zone"])
                mode = cmd["mode"]
                self.ctl.set_control_mode(zone, mode)
                return {"id": cid, "ok": True}

            if op == "set_autotune_setpoint":
                zones = cmd.get("zones", cmd.get("zone"))
                setpoints = cmd.get("setpoints", cmd.get("value_c"))
                self.ctl.set_autotune_setpoint(zones, setpoints)
                return {"id": cid, "ok": True}

            if op == "start_autotune":
                zones = cmd.get("zones", cmd.get("zone"))
                self.ctl.start_autotune(zones)
                return {"id": cid, "ok": True}

            if op == "stop_autotune":
                zone = int(cmd["zone"])
                self.ctl.stop_autotune(zone)
                return {"id": cid, "ok": True}

            if op == "read_config":
                changed = self.poll_config()
                return {"id": cid, "ok": True, "changed": bool(changed), "zones_enabled": self.zones_enabled}

            if op == "read_rampsoak":
                changed = self.poll_rampsoak()
                return {"id": cid, "ok": True, "changed": bool(changed), "zones_enabled": self.zones_enabled}

            return {"id": cid, "ok": False, "error": f"Unknown op: {op}"}

        except Exception as e:
            self._last_err = f"{type(e).__name__}: {e}"
            return {"id": cid, "ok": False, "error": self._last_err}

    # -----------------------------
    # Main loop
    # -----------------------------
    def run(self, *, poll_s: float, verbose: bool = False) -> None:
        """
        Main service loop:
        1) Drain TCP commands
        2) Telemetry poll (cadence limited by min(--poll, 1/telemetry_hz))
        3) Config poll (1/config_hz)
        4) Ramp/soak poll (1/rampsoak_hz) if enabled
        5) Analysis poll (1/analysis_hz) if enabled

        NOTE: Telemetry is the only loop explicitly capped by --poll (poll_s).
            The others are driven purely by their *_hz config values.
        """
        self.verbose = self.verbose or verbose

        if self.verbose:
            print(f"[service] out_dir: {self.out_dir}")
            print(f"[service] zones_enabled: {self.zones_enabled}")
            print(f"[service] register_map: {self.map_path}")

        # Start command server + connect serial
        self._cmd_thread.start()
        self.connect()
        # Seed SP cache immediately so analysis can start right away
        try:
            self.poll_config()
        except Exception as e:
            self._last_err = f"seed poll_config failed: {type(e).__name__}: {e}"

        next_tel = time.monotonic()
        next_cfg = time.monotonic()
        next_rs = time.monotonic()
        next_an = time.monotonic()

        while not self.stop_evt.is_set():
            cycle_t0 = time.monotonic()

            # 1) Drain queued commands (fast, non-blocking)
            while True:
                try:
                    req: CommandRequest = self.cmd_q.get_nowait()
                except Empty:
                    break

                resp = self.handle_command(req.cmd)
                req.result = resp
                req.done.set()

            now = time.monotonic()

            # 2) Telemetry poll
            # Cap telemetry loop to not run faster than --poll, even if telemetry_hz is high.
            tel_period = min(float(poll_s), hz_to_period_s(self.cfg.telemetry_hz))
            if now >= next_tel:
                ok = True
                err = None
                z1_pv = None
                try:
                    state_obj = self.poll_telemetry()
                    # Convenience for verbose prints
                    z1 = (state_obj.get("telemetry", {}) or {}).get("zones", {}).get("1", {}) or {}
                    z1_pv = z1.get("pv_c", None)
                except Exception as e:
                    ok = False
                    err = f"{type(e).__name__}: {e}"
                    self._last_err = err

                next_tel = now + tel_period
                cycle_ms = int((time.monotonic() - cycle_t0) * 1000)
                self._last_cycle_ms = cycle_ms

                if self.verbose:
                    print(f"[service] telemetry ok={ok} cycle_ms={cycle_ms} z1_pv={z1_pv} err={err}")

            now = time.monotonic()

            # 3) Config poll (slower)
            cfg_period = hz_to_period_s(self.cfg.config_hz)
            if now >= next_cfg:
                try:
                    self.poll_config()
                except Exception as e:
                    self._last_err = f"{type(e).__name__}: {e}"
                next_cfg = now + cfg_period

            now = time.monotonic()

            # 4) Ramp/Soak poll (usually disabled; on-demand otherwise)
            rs_period = hz_to_period_s(self.cfg.rampsoak_hz)
            if now >= next_rs:
                if float(self.cfg.rampsoak_hz) > 0:
                    try:
                        self.poll_rampsoak()
                    except Exception as e:
                        self._last_err = f"{type(e).__name__}: {e}"
                next_rs = now + rs_period

            now = time.monotonic()

            # 5) Analysis poll (equilibrium metrics)
            an_period = hz_to_period_s(self.cfg.analysis_hz)
            if now >= next_an:
                if float(self.cfg.analysis_hz) > 0:
                    try:
                        self.poll_analysis()
                    except Exception as e:
                        self._last_err = f"{type(e).__name__}: {e}"
                next_an = now + an_period

            # Avoid busy loop
            time.sleep(0.005)

        # Shutdown
        self.close()


# -----------------------------
# CLI entry
# -----------------------------
def infer_repo_root_from_this_file() -> Path:
    # This file should live in repo_root/py/cn616a_service.py
    here = Path(__file__).resolve()
    return here.parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True, help="Serial port (e.g. COM4)")
    ap.add_argument("--unit", type=int, default=1, help="Modbus unit/slave id (default 1)")
    ap.add_argument("--map", dest="map_path", default="cn616a_register_map.json", help="Path to register map JSON")
    ap.add_argument("--out-dir", default="", help="Output directory for logs (default repo_root/logs)")
    ap.add_argument("--host", default="127.0.0.1", help="TCP host (default 127.0.0.1)")
    ap.add_argument("--tcp-port", type=int, default=8765, help="TCP port (default 8765)")

    # legacy simple cadence knob (caps max loop cadence)
    ap.add_argument("--poll", type=float, default=0.5, help="Max telemetry period in seconds (default 0.5)")

    # service-config defaults at startup
    ap.add_argument("--telemetry-hz", type=float, default=2.0)
    ap.add_argument("--config-hz", type=float, default=0.2)
    ap.add_argument("--rampsoak-hz", type=float, default=0.0)
    ap.add_argument("--zones", default="auto", help="auto or comma list e.g. 1,2,3")
    ap.add_argument("--flush-each-line", action="store_true", help="Flush JSONL after each write")

    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    repo_root = infer_repo_root_from_this_file()
    out_dir = Path(args.out_dir).resolve() if args.out_dir else (repo_root / "logs")

    # Map path: accept absolute; else interpret relative to repo_root
    map_path = Path(args.map_path)
    if not map_path.is_absolute():
        map_path = (repo_root / map_path).resolve()

    # Zones
    zones_mode = "auto"
    zones_list: list[int] = [1, 2, 3, 4, 5, 6]
    if str(args.zones).strip().lower() != "auto":
        zones_mode = "list"
        zones_list = [int(x.strip()) for x in str(args.zones).split(",") if x.strip()]

    cfg = ServiceConfig(
        telemetry_hz=float(args.telemetry_hz),
        config_hz=float(args.config_hz),
        rampsoak_hz=float(args.rampsoak_hz),
        zones_mode=zones_mode,
        zones_list=zones_list,
        flush_each_line=bool(args.flush_each_line),
    )

    svc = CN616AService(
        port=str(args.port),
        unit=int(args.unit),
        map_path=str(map_path),
        out_dir=out_dir,
        tcp_host=str(args.host),
        tcp_port=int(args.tcp_port),
        cfg=cfg,
        verbose=bool(args.verbose),
    )

    try:
        svc.run(poll_s=float(args.poll), verbose=bool(args.verbose))
    except KeyboardInterrupt:
        if args.verbose:
            print("[service] stopping (KeyboardInterrupt)")
        svc.stop_evt.set()
        try:
            time.sleep(0.1)
        except Exception:
            pass
        svc.close()


if __name__ == "__main__":
    main()