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
import math
import os
import select
import socket
import sys
import threading
import time
import logging
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue, Empty, Full
from typing import Any, Dict, Optional, Sequence, Tuple, Union
from collections import deque

from cn616a import CN616A, CN616AError, SerialParams


LOGGER = logging.getLogger("cn616a.service")


def _setup_service_logging(logs_dir: Path, verbose: bool = False) -> logging.Logger:
    """Configure persistent runtime logging for the service process."""
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("cn616a.service")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    has_file_handler = any(
        isinstance(h, RotatingFileHandler)
        and getattr(h, "baseFilename", "").endswith("cn616a_service_error.log")
        for h in logger.handlers
    )
    if not has_file_handler:
        handler = RotatingFileHandler(
            logs_dir / "cn616a_service_error.log",
            maxBytes=1_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        logger.addHandler(handler)

    return logger


def _install_exception_hooks(logger: logging.Logger) -> None:
    """Capture uncaught process/thread exceptions in service log."""
    def _uncaught(exc_type, exc_value, exc_tb):
        logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))

    def _thread_uncaught(args):
        logger.critical(
            "Unhandled thread exception in %s",
            getattr(args.thread, "name", "<unknown>"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _uncaught
    threading.excepthook = _thread_uncaught


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


def _prune_rotated_logs(path: Path, max_files: int) -> None:
    keep_archives = max(0, int(max_files) - 1)
    archives = sorted(
        path.parent.glob(f"{path.stem}_*{path.suffix}"),
        key=lambda p: (p.stat().st_mtime_ns, p.name),
    )
    while len(archives) > keep_archives:
        old = archives.pop(0)
        try:
            old.unlink()
        except Exception:
            pass


def append_jsonl_rotated(
    path: Path,
    obj: dict,
    *,
    max_bytes: int,
    max_files: int,
    flush_each_line: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    max_bytes = max(1, int(max_bytes))
    max_files = max(1, int(max_files))
    payload = json.dumps(obj, separators=(",", ":")) + "\n"
    payload_size = len(payload.encode("utf-8"))

    if path.exists():
        try:
            current_size = path.stat().st_size
        except OSError:
            current_size = 0

        if current_size + payload_size > max_bytes:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            archived_path = path.parent / f"{path.stem}_{ts}{path.suffix}"
            counter = 0
            while archived_path.exists() and counter < 100:
                counter += 1
                archived_path = path.parent / f"{path.stem}_{ts}_{counter}{path.suffix}"
            try:
                path.rename(archived_path)
            except OSError:
                pass
            else:
                _prune_rotated_logs(path, max_files)

    with path.open("a", encoding="utf-8") as f:
        f.write(payload)
        if flush_each_line:
            f.flush()

    _prune_rotated_logs(path, max_files)


def stable_hash(obj: Any) -> str:
    b = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(b).hexdigest()


def load_persisted_service_config(out_dir: Path) -> Optional["ServiceConfig"]:
    """Load last service config snapshot if present."""
    state_path = Path(out_dir) / "cn616a_service_config_state.json"
    if not state_path.exists():
        return None

    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        cfg_obj = raw.get("config", {}) if isinstance(raw, dict) else {}
        if isinstance(cfg_obj, dict):
            return ServiceConfig.from_dict(cfg_obj)
    except Exception:
        pass
    return None


# -----------------------------
# Command server (JSONL over TCP)
# -----------------------------
MAX_COMMAND_BYTES = 64 * 1024
MAX_ACTIVE_CLIENTS = 8
MAX_PENDING_COMMANDS = 64
DEFAULT_COMMAND_TIMEOUT_S = 5.0
MAX_COMMAND_TIMEOUT_S = 30.0


@dataclass
class CommandRequest:
    cmd: dict
    queued_at: float = field(default_factory=time.monotonic)
    done: threading.Event = field(default_factory=threading.Event)
    result: Optional[dict] = None
    state: str = "queued"
    started_at: Optional[float] = None
    controller_io_ms: float = 0.0
    state_lock: threading.Lock = field(default_factory=threading.Lock)

    def mark_started(self) -> bool:
        with self.state_lock:
            if self.state != "queued":
                return False
            self.state = "running"
            self.started_at = time.monotonic()
            return True

    def cancel_if_queued(self) -> bool:
        with self.state_lock:
            if self.state != "queued":
                return False
            self.state = "cancelled"
            self.done.set()
            return True

    def finish(self, result: dict) -> None:
        with self.state_lock:
            self.result = result
            self.state = "completed"
            self.done.set()

    def timeout_response(self) -> dict:
        with self.state_lock:
            cid = self.cmd.get("id")
            if self.state == "queued":
                self.state = "cancelled"
                self.done.set()
            if self.state == "cancelled":
                return {
                    "id": cid,
                    "ok": False,
                    "state": "not_executed",
                    "error": "Command timed out in queue and was not executed",
                }
            if self.state == "completed" and self.result is not None:
                return self.result
            return {
                "id": cid,
                "ok": False,
                "state": "unknown",
                "error": "Command timed out after execution started; outcome may be unknown",
            }


def command_server(
    host: str,
    port: int,
    cmd_q: Queue,
    stop_evt: threading.Event,
    verbose: bool = False,
):
    """
    Robust JSONL TCP server:
    - Handles a bounded number of client connections concurrently.
    - Reads size-bounded JSON objects delimited by newline.
    - Enqueues commands for serialized execution by the service thread.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(MAX_ACTIVE_CLIENTS)
    srv.settimeout(0.5)
    client_slots = threading.BoundedSemaphore(MAX_ACTIVE_CLIENTS)

    print(f"[service] command server listening on {host}:{port}")
    LOGGER.info("Command server listening on %s:%s", host, port)

    while not stop_evt.is_set():
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break

        if not client_slots.acquire(blocking=False):
            LOGGER.warning("Rejecting TCP client %s: active client limit reached", addr)
            try:
                conn.sendall(
                    (json.dumps({"ok": False, "state": "rejected", "error": "Too many active clients"}) + "\n").encode("utf-8")
                )
            except OSError:
                pass
            conn.close()
            continue

        print(f"[service] client connected: {addr}")
        LOGGER.info("Client connected: %s", addr)
        threading.Thread(
            target=_handle_command_client,
            args=(conn, addr, cmd_q, stop_evt, client_slots),
            name=f"cn616a-client-{addr[0]}:{addr[1]}",
            daemon=True,
        ).start()

    srv.close()


def _client_disconnected(conn: socket.socket) -> bool:
    try:
        readable, _, _ = select.select([conn], [], [], 0)
        return bool(readable and conn.recv(1, socket.MSG_PEEK) == b"")
    except (OSError, ValueError):
        return True


def _send_json_line(conn: socket.socket, response: dict) -> bool:
    try:
        conn.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))
        return True
    except OSError:
        return False


def _handle_command_client(
    conn: socket.socket,
    addr: Tuple[str, int],
    cmd_q: Queue,
    stop_evt: threading.Event,
    client_slots: threading.BoundedSemaphore,
) -> None:
    try:
        conn.settimeout(5.0)
        with conn:
            buf = b""
            while not stop_evt.is_set():
                try:
                    chunk = conn.recv(4096)
                except (ConnectionAbortedError, ConnectionResetError, OSError):
                    break
                except socket.timeout:
                    break

                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_COMMAND_BYTES and b"\n" not in buf:
                    _send_json_line(conn, {"ok": False, "state": "rejected", "error": "Request exceeded the size limit"})
                    break

                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    line = line.strip()
                    if not line:
                        continue
                    if len(line) > MAX_COMMAND_BYTES:
                        if not _send_json_line(conn, {"ok": False, "state": "rejected", "error": "Request exceeded the size limit"}):
                            return
                        continue

                    try:
                        cmd = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        LOGGER.warning("Bad JSON from client %s: %s", addr, exc)
                        if not _send_json_line(conn, {"ok": False, "error": f"Bad JSON: {exc}"}):
                            return
                        continue
                    if not isinstance(cmd, dict):
                        if not _send_json_line(conn, {"ok": False, "error": "Request must be a JSON object"}):
                            return
                        continue

                    cid = cmd.get("id")
                    try:
                        timeout_s = float(cmd.get("timeout_s", DEFAULT_COMMAND_TIMEOUT_S))
                    except (TypeError, ValueError):
                        timeout_s = math.nan
                    if not math.isfinite(timeout_s) or timeout_s <= 0 or timeout_s > MAX_COMMAND_TIMEOUT_S:
                        if not _send_json_line(conn, {
                            "id": cid,
                            "ok": False,
                            "state": "rejected",
                            "error": f"timeout_s must be greater than 0 and no more than {MAX_COMMAND_TIMEOUT_S:g}",
                        }):
                            return
                        continue

                    req = CommandRequest(cmd=cmd)
                    try:
                        cmd_q.put_nowait(req)
                    except Full:
                        LOGGER.warning("Rejecting command id=%r because the bounded queue is full", cid)
                        if not _send_json_line(conn, {
                            "id": cid,
                            "ok": False,
                            "state": "rejected",
                            "error": "Command queue is full; request was not accepted",
                        }):
                            return
                        continue

                    deadline = time.monotonic() + timeout_s
                    response = None
                    while response is None:
                        remaining = deadline - time.monotonic()
                        if req.done.wait(timeout=min(0.05, max(0.0, remaining))):
                            response = req.result
                            if response is None:
                                response = req.timeout_response()
                            break
                        if _client_disconnected(conn):
                            if req.cancel_if_queued():
                                LOGGER.info("Command id=%r disconnected before execution; cancelled", cid)
                            else:
                                LOGGER.warning("Command id=%r client disconnected after execution started; outcome may be unknown", cid)
                            return
                        if stop_evt.is_set() and req.cancel_if_queued():
                            response = {
                                "id": cid,
                                "ok": False,
                                "state": "not_executed",
                                "error": "Service is shutting down; command was not executed",
                            }
                            LOGGER.warning("Command id=%r cancelled during service shutdown", cid)
                        elif remaining <= 0:
                            response = req.timeout_response()
                            LOGGER.warning(
                                "Command id=%r timed out with state=%s",
                                cid,
                                response.get("state", "completed"),
                            )

                    if response is None:
                        response = {"id": cid, "ok": False, "error": "No result returned"}
                    if not _send_json_line(conn, response):
                        break
    finally:
        client_slots.release()


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
    zone_names: dict = field(default_factory=lambda: {str(z): f"Zone {z}" for z in range(1, 7)})

    # Logging
    flush_each_line: bool = True
    max_service_config_log_bytes: int = 2_000_000
    max_service_config_log_files: int = 10
    max_telemetry_log_bytes: int = 10_000_000
    max_telemetry_log_files: int = 10
    telemetry_retention_s: float = 86_400.0
    max_config_log_bytes: int = 5_000_000
    max_config_log_files: int = 10
    max_rampsoak_log_bytes: int = 5_000_000
    max_rampsoak_log_files: int = 10
    max_analysis_log_bytes: int = 10_000_000
    max_analysis_log_files: int = 10
    analysis_retention_s: float = 86_400.0

    # Viewer settings for GUI (history, colors, line width)
    viewer_history_hours: float = 1.0
    viewer_line_width: float = 2.5
    viewer_pv_color: str = "blue"
    viewer_sp_color: str = "red"
    viewer_sp_autotune_color: str = "purple"
    viewer_show_sp_abs: bool = True
    viewer_show_sp_autotune: bool = True
    viewer_show_mae: bool = True

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
            "zone_names": dict(self.zone_names),
            "flush_each_line": self.flush_each_line,
            "max_service_config_log_bytes": self.max_service_config_log_bytes,
            "max_service_config_log_files": self.max_service_config_log_files,
            "max_telemetry_log_bytes": self.max_telemetry_log_bytes,
            "max_telemetry_log_files": self.max_telemetry_log_files,
            "telemetry_retention_s": self.telemetry_retention_s,
            "max_config_log_bytes": self.max_config_log_bytes,
            "max_config_log_files": self.max_config_log_files,
            "max_rampsoak_log_bytes": self.max_rampsoak_log_bytes,
            "max_rampsoak_log_files": self.max_rampsoak_log_files,
            "max_analysis_log_bytes": self.max_analysis_log_bytes,
            "max_analysis_log_files": self.max_analysis_log_files,
            "analysis_retention_s": self.analysis_retention_s,
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
            "viewer_show_sp_abs": self.viewer_show_sp_abs,
            "viewer_show_sp_autotune": self.viewer_show_sp_autotune,
            "viewer_show_mae": self.viewer_show_mae,
            "viewer": {
                "history_hours": self.viewer_history_hours,
                "line_width": self.viewer_line_width,
                "pv_color": self.viewer_pv_color,
                "sp_color": self.viewer_sp_color,
                "sp_autotune_color": self.viewer_sp_autotune_color,
                "show_sp_abs": self.viewer_show_sp_abs,
                "show_sp_autotune": self.viewer_show_sp_autotune,
                "show_mae": self.viewer_show_mae,
            },
        }

    @staticmethod
    def from_dict(d: dict) -> "ServiceConfig":
        if not isinstance(d, dict):
            d = {}

        # Accept nested viewer payload and map to flat persisted keys.
        source = dict(d)
        viewer = source.get("viewer")
        if isinstance(viewer, dict):
            source.setdefault("viewer_history_hours", viewer.get("history_hours"))
            source.setdefault("viewer_line_width", viewer.get("line_width"))
            source.setdefault("viewer_pv_color", viewer.get("pv_color"))
            source.setdefault("viewer_sp_color", viewer.get("sp_color"))
            source.setdefault("viewer_sp_autotune_color", viewer.get("sp_autotune_color"))
            source.setdefault("viewer_show_sp_abs", viewer.get("show_sp_abs"))
            source.setdefault("viewer_show_sp_autotune", viewer.get("show_sp_autotune"))
            source.setdefault("viewer_show_mae", viewer.get("show_mae"))

        cfg = ServiceConfig()
        for k in ["telemetry_hz", "config_hz", "rampsoak_hz", "analysis_hz", "gui_refresh_hz",
                  "equilibrium_window_s", "equilibrium_threshold_c",
                  "zones_mode", "zones_list", "zone_names", "flush_each_line",
                  "max_service_config_log_bytes", "max_service_config_log_files",
                  "max_telemetry_log_bytes", "max_telemetry_log_files",
                  "telemetry_retention_s",
                  "max_config_log_bytes", "max_config_log_files",
                  "max_rampsoak_log_bytes", "max_rampsoak_log_files",
                  "max_analysis_log_bytes", "max_analysis_log_files",
                  "analysis_retention_s",
                  "last_serial_port", "last_serial_params", "last_tcp_host", "last_tcp_port",
                  "viewer_history_hours", "viewer_line_width",
                  "viewer_pv_color", "viewer_sp_color", "viewer_sp_autotune_color",
                  "viewer_show_sp_abs", "viewer_show_sp_autotune", "viewer_show_mae"]:
            if k in source:
                setattr(cfg, k, source[k])
        # normalize
        cfg.telemetry_hz = float(cfg.telemetry_hz)
        cfg.config_hz = float(cfg.config_hz)
        cfg.rampsoak_hz = float(cfg.rampsoak_hz)
        cfg.zones_mode = str(cfg.zones_mode)
        cfg.zones_list = list(cfg.zones_list) if isinstance(cfg.zones_list, (list, tuple)) else [1, 2, 3, 4, 5, 6]
        if isinstance(cfg.zone_names, dict):
            normalized = {}
            for z in range(1, 7):
                raw = cfg.zone_names.get(str(z), cfg.zone_names.get(z, f"Zone {z}"))
                name = str(raw).strip() if raw is not None else ""
                normalized[str(z)] = name or f"Zone {z}"
            cfg.zone_names = normalized
        elif isinstance(cfg.zone_names, (list, tuple)):
            normalized = {str(z): f"Zone {z}" for z in range(1, 7)}
            for idx, raw in enumerate(cfg.zone_names[:6], start=1):
                name = str(raw).strip() if raw is not None else ""
                normalized[str(idx)] = name or f"Zone {idx}"
            cfg.zone_names = normalized
        else:
            cfg.zone_names = {str(z): f"Zone {z}" for z in range(1, 7)}
        cfg.flush_each_line = bool(cfg.flush_each_line)
        cfg.analysis_hz = float(cfg.analysis_hz)
        cfg.gui_refresh_hz = float(cfg.gui_refresh_hz)
        cfg.equilibrium_window_s = float(cfg.equilibrium_window_s)
        cfg.equilibrium_threshold_c = float(cfg.equilibrium_threshold_c)
        cfg.viewer_show_sp_abs = bool(cfg.viewer_show_sp_abs)
        cfg.viewer_show_sp_autotune = bool(cfg.viewer_show_sp_autotune)
        cfg.viewer_show_mae = bool(cfg.viewer_show_mae)
        cfg.max_service_config_log_bytes = int(cfg.max_service_config_log_bytes)
        cfg.max_service_config_log_files = max(1, int(cfg.max_service_config_log_files))
        cfg.max_telemetry_log_bytes = int(cfg.max_telemetry_log_bytes)
        cfg.max_telemetry_log_files = max(1, int(cfg.max_telemetry_log_files))
        cfg.telemetry_retention_s = max(0.0, float(cfg.telemetry_retention_s))
        cfg.max_config_log_bytes = int(cfg.max_config_log_bytes)
        cfg.max_config_log_files = max(1, int(cfg.max_config_log_files))
        cfg.max_rampsoak_log_bytes = int(cfg.max_rampsoak_log_bytes)
        cfg.max_rampsoak_log_files = max(1, int(cfg.max_rampsoak_log_files))
        cfg.max_analysis_log_bytes = int(cfg.max_analysis_log_bytes)
        cfg.max_analysis_log_files = max(1, int(cfg.max_analysis_log_files))
        cfg.analysis_retention_s = max(0.0, float(cfg.analysis_retention_s))
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
        self.active_run_state_path = self.out_dir / "cn616a_active_run_state.json"
        self._last_analysis_hash: Optional[str] = None

        # "Start Run" mode: mirrors log JSONL into a user-chosen directory for the duration of a run.
        self._run_active: bool = False
        self._run_dir: Optional[Path] = None
        self._run_name: str = ""
        self._run_started_ts: Optional[str] = None
        self._run_started_monotonic: Optional[float] = None
        self._run_last_data_ts: Optional[str] = None
        self._run_recovered: bool = False
        self._run_interruption_s: Optional[float] = None
        self._run_interruption_started_ts: Optional[str] = None
        self._run_recovered_at_ts: Optional[str] = None
        # Scheduled auto-stop is a plain value compared every loop iteration (no timer/thread to
        # cancel), so toggling it or editing the target time mid-run is always safe.
        self._run_schedule_enabled: bool = False
        self._run_schedule_stop_at: Optional[datetime] = None

        # state hashes (to avoid noisy config logs)
        self._last_config_hash: Optional[str] = None
        self._last_rampsoak_hash: Optional[str] = None

        # status / errors
        self._connected = False
        self._last_err: Optional[str] = None
        self._last_telemetry_ts: Optional[str] = None
        self._last_cycle_ms: Optional[int] = None

        # command infra
        self.cmd_q: Queue = Queue(maxsize=MAX_PENDING_COMMANDS)
        self._active_command: Optional[CommandRequest] = None
        self._last_controller_io_ms: Optional[float] = None
        self._controller_io_count = 0
        self.stop_evt = threading.Event()
        self._cmd_thread = threading.Thread(
            target=command_server,
            args=(self.tcp_host, self.tcp_port, self.cmd_q, self.stop_evt, self.verbose),
            daemon=True,
        )

        # driver
        self.ctl = CN616A(port=self.port, unit=self.unit, register_map_path=self.map_path)

        # write initial service config snapshot + log
        self._restore_active_run()
        self._log_service_config(event="startup", patch={})

    def _log_service_config(self, *, event: str, patch: dict, client: Optional[Tuple[str, int]] = None) -> None:
        snap = {
            "ts": now_iso_local_ms(),
            "event": event,
            "client": {"host": client[0], "port": client[1]} if client else None,
            "config": self.cfg.to_dict(),
        }
        atomic_write_json(self.svc_cfg_state_path, snap)
        append_jsonl_rotated(
            self.svc_cfg_log_path,
            snap,
            max_bytes=self.cfg.max_service_config_log_bytes,
            max_files=self.cfg.max_service_config_log_files,
            flush_each_line=self.cfg.flush_each_line,
        )

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

    def close(self, *, service_shutdown: bool = False) -> None:
        if service_shutdown and self._run_active:
            self._persist_active_run(service_stopped_ts=now_iso_local_ms())
        try:
            self.ctl.close()
        except Exception:
            pass
        self._connected = False

    def _controller_call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Call one driver operation and measure its serialized execution time."""
        started = time.monotonic()
        try:
            return getattr(self.ctl, method_name)(*args, **kwargs)
        finally:
            duration_ms = (time.monotonic() - started) * 1000.0
            self._last_controller_io_ms = duration_ms
            self._controller_io_count += 1
            if self._active_command is not None:
                self._active_command.controller_io_ms += duration_ms
            LOGGER.debug("Controller operation %s completed in %.3f ms", method_name, duration_ms)

    def _process_pending_commands(self, *, limit: int = 1) -> None:
        """Run a bounded FIFO batch at safe boundaries between controller operations."""
        for _ in range(max(0, int(limit))):
            try:
                req: CommandRequest = self.cmd_q.get_nowait()
            except Empty:
                return
            if not req.mark_started():
                continue

            started = req.started_at or time.monotonic()
            queue_wait_ms = max(0.0, (started - req.queued_at) * 1000.0)
            self._active_command = req
            try:
                response = self.handle_command(req.cmd)
            except Exception as exc:
                LOGGER.exception("Unhandled command failure id=%r", req.cmd.get("id"))
                response = {
                    "id": req.cmd.get("id"),
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            finally:
                self._active_command = None

            end_to_end_ms = max(0.0, (time.monotonic() - req.queued_at) * 1000.0)
            response["timing"] = {
                "queue_wait_ms": round(queue_wait_ms, 3),
                "controller_io_ms": round(req.controller_io_ms, 3),
                "end_to_end_ms": round(end_to_end_ms, 3),
            }
            req.finish(response)
            LOGGER.info(
                "Command completed id=%r op=%r state=%s queue_wait_ms=%.3f "
                "controller_io_ms=%.3f end_to_end_ms=%.3f",
                req.cmd.get("id"),
                req.cmd.get("op"),
                "success" if response.get("ok") is True else "failure",
                queue_wait_ms,
                req.controller_io_ms,
                end_to_end_ms,
            )

    def _check_scheduled_run_stop(self) -> None:
        if (
            self._run_active
            and self._run_schedule_enabled
            and self._run_schedule_stop_at is not None
            and datetime.now(timezone.utc) >= self._run_schedule_stop_at
        ):
            self._do_stop_run(reason="scheduled")

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

        data = self._controller_call("read_telemetry", zones)

        state_obj = {
            "ts": now_iso_local_ms(),
            "unit": self.unit,
            "port": self.port,
            "zones": zones,
            "telemetry": data,
        }
        atomic_write_json(self.telemetry_state_path, state_obj)
        append_jsonl_rotated(
            self.telemetry_log_path,
            state_obj,
            max_bytes=self.cfg.max_telemetry_log_bytes,
            max_files=self.cfg.max_telemetry_log_files,
            flush_each_line=self.cfg.flush_each_line,
        )
        self._append_run_copy("cn616a_telemetry_log.jsonl", state_obj)

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

        data = self._controller_call("read_config", zones)

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
            append_jsonl_rotated(
                self.config_log_path,
                state_obj,
                max_bytes=self.cfg.max_config_log_bytes,
                max_files=self.cfg.max_config_log_files,
                flush_each_line=self.cfg.flush_each_line,
            )
            self._append_run_copy("cn616a_config_log.jsonl", state_obj)
            return state_obj

        return None

    def poll_rampsoak(self) -> Optional[Dict[str, Any]]:
        """
        Poll ramp/soak profiles from controller, write:
        - cn616a_rampsoak_state.json (snapshot)
        - cn616a_rampsoak_log.jsonl (append only when data changes)
        """
        zones = self.zones_enabled
        data = self._controller_call("read_rampsoak_all", zones)

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
            append_jsonl_rotated(
                self.rampsoak_log_path,
                state_obj,
                max_bytes=self.cfg.max_rampsoak_log_bytes,
                max_files=self.cfg.max_rampsoak_log_files,
                flush_each_line=self.cfg.flush_each_line,
            )
            self._append_run_copy("cn616a_rampsoak_log.jsonl", state_obj)
            return state_obj
        return None

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
            append_jsonl_rotated(
                self.analysis_log_path,
                state_obj,
                max_bytes=self.cfg.max_analysis_log_bytes,
                max_files=self.cfg.max_analysis_log_files,
                flush_each_line=self.cfg.flush_each_line,
            )
            self._append_run_copy("cn616a_analysis_log.jsonl", state_obj)

        return state_obj

    # -----------------------------
    # "Start Run" mode (mirrors logs into a custom directory for a bounded time window)
    # -----------------------------
    def _append_run_copy(self, filename: str, obj: dict) -> None:
        """Mirror a just-written log line into the active run's custom directory, if any."""
        if not self._run_active or self._run_dir is None:
            return
        try:
            append_jsonl(self._run_dir / filename, obj, flush_each_line=self.cfg.flush_each_line)
            if filename == "cn616a_telemetry_log.jsonl":
                self._run_last_data_ts = obj.get("ts") or self._run_last_data_ts
                self._persist_active_run()
                self._write_run_info(event="collecting")
        except Exception:
            LOGGER.exception("Failed mirroring %s into run directory %s", filename, self._run_dir)

    def _write_run_info(self, *, event: str, reason: Optional[str] = None) -> None:
        if self._run_dir is None:
            return
        payload = {
            "event": event,
            "reason": reason,
            "ts": now_iso_local_ms(),
            "run_name": self._run_name,
            "run_started_ts": self._run_started_ts,
            "last_data_ts": self._run_last_data_ts,
            "recovered": self._run_recovered,
            "interruption_s": self._run_interruption_s,
            "interruption_started_ts": self._run_interruption_started_ts,
            "recovered_at_ts": self._run_recovered_at_ts,
            "unit": self.unit,
            "port": self.port,
            "zones_enabled": self.zones_enabled,
            "run_schedule_enabled": self._run_schedule_enabled,
            "run_schedule_stop_at": self._run_schedule_stop_at.isoformat() if self._run_schedule_stop_at else None,
        }
        try:
            atomic_write_json(self._run_dir / "cn616a_run_info.json", payload)
        except Exception:
            LOGGER.exception("Failed writing run info snapshot to %s", self._run_dir)

    def _persist_active_run(self, *, service_stopped_ts: Optional[str] = None) -> None:
        if not self._run_active or self._run_dir is None:
            return
        payload = {
            "active": True,
            "run_dir": str(self._run_dir),
            "run_name": self._run_name,
            "run_started_ts": self._run_started_ts,
            "last_data_ts": self._run_last_data_ts,
            "service_stopped_ts": service_stopped_ts,
        }
        try:
            atomic_write_json(self.active_run_state_path, payload)
        except Exception:
            LOGGER.exception("Failed persisting active run recovery state")

    def _restore_active_run(self) -> None:
        if not self.active_run_state_path.exists():
            return
        try:
            payload = json.loads(self.active_run_state_path.read_text(encoding="utf-8"))
        except Exception:
            LOGGER.exception("Failed reading active run recovery state")
            return
        if not isinstance(payload, dict) or not payload.get("active") or not payload.get("run_dir"):
            return

        run_dir = Path(str(payload["run_dir"]))
        if not run_dir.exists():
            LOGGER.warning("Discarding active run recovery state for missing directory %s", run_dir)
            return

        self._run_active = True
        self._run_dir = run_dir
        self._run_name = str(payload.get("run_name") or "")
        self._run_started_ts = payload.get("run_started_ts")
        self._run_started_monotonic = time.monotonic()
        self._run_last_data_ts = payload.get("last_data_ts") or self._run_started_ts
        self._run_recovered = True
        self._run_recovered_at_ts = now_iso_local_ms()

        try:
            interrupted_at = datetime.fromisoformat(str(payload.get("service_stopped_ts")))
            recovered_at = datetime.fromisoformat(self._run_recovered_at_ts)
            if interrupted_at.tzinfo is None:
                interrupted_at = interrupted_at.replace(tzinfo=timezone.utc)
            if recovered_at.tzinfo is None:
                recovered_at = recovered_at.replace(tzinfo=timezone.utc)
            self._run_interruption_started_ts = interrupted_at.isoformat()
            self._run_interruption_s = max(0.0, (recovered_at - interrupted_at).total_seconds())
        except (TypeError, ValueError):
            self._run_interruption_started_ts = payload.get("service_stopped_ts")
            self._run_interruption_s = None

        self._write_run_info(event="recovered")

    def _run_status_payload(self) -> dict:
        elapsed_s = None
        if self._run_active and self._run_started_monotonic is not None:
            elapsed_s = max(0.0, time.monotonic() - self._run_started_monotonic)
        return {
            "run_active": self._run_active,
            "run_dir": str(self._run_dir) if self._run_dir else None,
            "run_name": self._run_name,
            "run_started_ts": self._run_started_ts,
            "run_elapsed_s": elapsed_s,
            "run_schedule_enabled": self._run_schedule_enabled,
            "run_schedule_stop_at": self._run_schedule_stop_at.isoformat() if self._run_schedule_stop_at else None,
            "run_last_data_ts": self._run_last_data_ts,
            "run_recovered": self._run_recovered,
            "run_interruption_s": self._run_interruption_s,
            "run_interruption_started_ts": self._run_interruption_started_ts,
            "run_recovered_at_ts": self._run_recovered_at_ts,
        }

    def _do_stop_run(self, *, reason: str) -> None:
        if not self._run_active:
            return
        self._write_run_info(event="stopped", reason=reason)
        try:
            atomic_write_json(self.active_run_state_path, {"active": False, "stopped_ts": now_iso_local_ms()})
        except Exception:
            LOGGER.exception("Failed clearing active run recovery state")
        self._log_service_config(event="stop_run", patch={"reason": reason})
        self._run_active = False
        self._run_dir = None
        self._run_name = ""
        self._run_started_ts = None
        self._run_started_monotonic = None
        self._run_last_data_ts = None
        self._run_recovered = False
        self._run_interruption_s = None
        self._run_interruption_started_ts = None
        self._run_recovered_at_ts = None
        # Schedule (enabled flag + target time) is left as configured so it can be reused by the
        # next run without the GUI needing to resend it.

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
                    "controller_io": {
                        "last_duration_ms": (
                            round(self._last_controller_io_ms, 3)
                            if self._last_controller_io_ms is not None
                            else None
                        ),
                        "operation_count": self._controller_io_count,
                    },
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
                self._controller_call("set_sp_abs", zone, value_c)
                return {"id": cid, "ok": True}

            if op == "set_control_method":
                zone = int(cmd["zone"])
                method = cmd["method"]
                self._controller_call("set_control_method", zone, method)
                return {"id": cid, "ok": True}

            if op == "set_control_mode":
                zone = int(cmd["zone"])
                mode = cmd["mode"]
                self._controller_call("set_control_mode", zone, mode)
                return {"id": cid, "ok": True}

            if op == "set_autotune_setpoint":
                zones = cmd.get("zones", cmd.get("zone"))
                setpoints = cmd.get("setpoints", cmd.get("value_c"))
                self._controller_call("set_autotune_setpoint", zones, setpoints)
                return {"id": cid, "ok": True}

            if op == "start_autotune":
                zones = cmd.get("zones", cmd.get("zone"))
                self._controller_call("start_autotune", zones)
                return {"id": cid, "ok": True}

            if op == "stop_autotune":
                zone = int(cmd["zone"])
                self._controller_call("stop_autotune", zone)
                return {"id": cid, "ok": True}

            if op == "read_config":
                changed = self.poll_config()
                return {"id": cid, "ok": True, "changed": bool(changed), "zones_enabled": self.zones_enabled}

            if op == "read_rampsoak":
                changed = self.poll_rampsoak()
                return {"id": cid, "ok": True, "changed": bool(changed), "zones_enabled": self.zones_enabled}

            if op == "set_rampsoak_segments":
                zone = int(cmd["zone"])
                segments_raw = cmd.get("segments", {})
                if not isinstance(segments_raw, dict) or not segments_raw:
                    return {"id": cid, "ok": False, "error": "segments must be a non-empty object"}
                segments = {int(seg_i): vals for seg_i, vals in segments_raw.items()}
                self._controller_call("write_rampsoak_profile", zone, segments)
                self.poll_rampsoak()
                return {"id": cid, "ok": True}

            if op == "set_num_segments":
                zone = int(cmd["zone"])
                count = int(cmd["count"])
                self._controller_call("set_num_segments", zone, count)
                self.poll_rampsoak()
                return {"id": cid, "ok": True}

            if op == "set_start_profile":
                zones = cmd.get("zones", cmd.get("zone"))
                enable = bool(cmd.get("enable", False))
                self._controller_call("set_start_profile", zones, enable)
                self.poll_rampsoak()
                return {"id": cid, "ok": True}

            # ---- "Start Run" mode ----
            if op == "start_run":
                if self._run_schedule_enabled and self._run_schedule_stop_at is not None:
                    if datetime.now(timezone.utc) >= self._run_schedule_stop_at:
                        return {
                            "id": cid, "ok": False,
                            "error": (
                                "Scheduled auto-stop time is already in the past "
                                f"({self._run_schedule_stop_at.isoformat()}); update or disable the "
                                "schedule before starting a run."
                            ),
                        }

                run_dir_raw = cmd.get("run_dir")
                if not run_dir_raw:
                    return {"id": cid, "ok": False, "error": "run_dir is required"}
                run_dir = Path(str(run_dir_raw))
                try:
                    run_dir.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    return {"id": cid, "ok": False, "error": f"Cannot create run_dir: {e}"}

                self._run_active = True
                self._run_dir = run_dir
                self._run_name = str(cmd.get("run_name") or "").strip()
                self._run_started_ts = now_iso_local_ms()
                self._run_started_monotonic = time.monotonic()
                self._run_last_data_ts = None
                self._run_recovered = False
                self._run_interruption_s = None
                self._run_interruption_started_ts = None
                self._run_recovered_at_ts = None
                self._persist_active_run()
                self._write_run_info(event="started")
                self._log_service_config(
                    event="start_run",
                    patch={"run_dir": str(run_dir), "run_name": self._run_name},
                )
                return {"id": cid, "ok": True, **self._run_status_payload()}

            if op == "stop_run":
                was_active = self._run_active
                self._do_stop_run(reason="manual")
                return {"id": cid, "ok": True, "was_active": was_active, **self._run_status_payload()}

            if op == "set_run_schedule":
                enabled = bool(cmd.get("enabled", False))
                stop_at_raw = cmd.get("stop_at_iso")
                stop_at_dt = None
                if enabled:
                    if not stop_at_raw:
                        return {"id": cid, "ok": False, "error": "stop_at_iso is required when enabling schedule"}
                    try:
                        stop_at_dt = datetime.fromisoformat(str(stop_at_raw))
                    except Exception:
                        return {"id": cid, "ok": False, "error": "stop_at_iso must be an ISO-8601 timestamp"}
                    if stop_at_dt.tzinfo is None:
                        return {"id": cid, "ok": False, "error": "stop_at_iso must include a UTC offset"}
                    if stop_at_dt <= datetime.now(timezone.utc):
                        return {"id": cid, "ok": False, "error": "stop_at_iso must be in the future"}
                # Plain value swap: safe to call any number of times, whether a run is active or not,
                # and safe to call again mid-run with a different time.
                self._run_schedule_enabled = enabled
                self._run_schedule_stop_at = stop_at_dt
                return {"id": cid, "ok": True, **self._run_status_payload()}

            if op == "get_run_status":
                return {"id": cid, "ok": True, **self._run_status_payload()}

            return {"id": cid, "ok": False, "error": f"Unknown op: {op}"}

        except Exception as e:
            self._last_err = f"{type(e).__name__}: {e}"
            LOGGER.exception("Command failed op=%r id=%r", op, cid)
            return {"id": cid, "ok": False, "error": self._last_err}

    # -----------------------------
    # Main loop
    # -----------------------------
    def run(self, *, poll_s: float, verbose: bool = False) -> None:
        """
        Main service loop:
        1) Service one queued TCP command at each safe boundary
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

            # One FIFO command at each safe boundary prevents command bursts from
            # starving polls, while allowing commands to run between poll steps.
            self._check_scheduled_run_stop()
            self._process_pending_commands()
            self._check_scheduled_run_stop()
            if self.stop_evt.is_set():
                break

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
                    LOGGER.exception("Telemetry poll failed")

                next_tel = now + tel_period
                cycle_ms = int((time.monotonic() - cycle_t0) * 1000)
                self._last_cycle_ms = cycle_ms

                if self.verbose:
                    print(f"[service] telemetry ok={ok} cycle_ms={cycle_ms} z1_pv={z1_pv} err={err}")

            self._process_pending_commands()
            self._check_scheduled_run_stop()
            if self.stop_evt.is_set():
                break

            now = time.monotonic()

            # 3) Config poll (slower)
            cfg_period = hz_to_period_s(self.cfg.config_hz)
            if now >= next_cfg:
                try:
                    self.poll_config()
                except Exception as e:
                    self._last_err = f"{type(e).__name__}: {e}"
                    LOGGER.exception("Config poll failed")
                next_cfg = now + cfg_period

            self._process_pending_commands()
            self._check_scheduled_run_stop()
            if self.stop_evt.is_set():
                break

            now = time.monotonic()

            # 4) Ramp/Soak poll (usually disabled; on-demand otherwise)
            rs_period = hz_to_period_s(self.cfg.rampsoak_hz)
            if now >= next_rs:
                if float(self.cfg.rampsoak_hz) > 0:
                    try:
                        self.poll_rampsoak()
                    except Exception as e:
                        self._last_err = f"{type(e).__name__}: {e}"
                        LOGGER.exception("Ramp/soak poll failed")
                next_rs = now + rs_period

            self._process_pending_commands()
            self._check_scheduled_run_stop()
            if self.stop_evt.is_set():
                break

            now = time.monotonic()

            # 5) Analysis poll (equilibrium metrics)
            an_period = hz_to_period_s(self.cfg.analysis_hz)
            if now >= next_an:
                if float(self.cfg.analysis_hz) > 0:
                    try:
                        self.poll_analysis()
                    except Exception as e:
                        self._last_err = f"{type(e).__name__}: {e}"
                        LOGGER.exception("Analysis poll failed")
                next_an = now + an_period

            self._process_pending_commands()
            self._check_scheduled_run_stop()
            if self.stop_evt.is_set():
                break

            # 6) Scheduled run auto-stop. A cheap comparison against a plain stored value each
            # iteration - no timer/thread to cancel, so toggling the checkbox or editing the target
            # time mid-run from the GUI can never leave a stale callback armed.
            # Avoid busy loop
            time.sleep(0.005)

        # Shutdown
        self.close(service_shutdown=True)


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

    # Optional startup overrides; if omitted, persisted settings are reused.
    ap.add_argument("--telemetry-hz", type=float, default=None)
    ap.add_argument("--config-hz", type=float, default=None)
    ap.add_argument("--rampsoak-hz", type=float, default=None)
    ap.add_argument("--zones", default=None, help="auto or comma list e.g. 1,2,3")
    flush_group = ap.add_mutually_exclusive_group()
    flush_group.add_argument("--flush-each-line", dest="flush_each_line", action="store_const", const=True,
                             default=None, help="Flush JSONL after each write")
    flush_group.add_argument("--no-flush-each-line", dest="flush_each_line", action="store_const", const=False,
                             default=None, help="Do not flush JSONL after each write")

    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    repo_root = infer_repo_root_from_this_file()
    out_dir = Path(args.out_dir).resolve() if args.out_dir else (repo_root / "logs")
    logger = _setup_service_logging(out_dir, verbose=bool(args.verbose))
    _install_exception_hooks(logger)
    logger.info("Service runtime logging initialized")

    # Map path: accept absolute; else interpret relative to repo_root
    map_path = Path(args.map_path)
    if not map_path.is_absolute():
        map_path = (repo_root / map_path).resolve()

    cfg = load_persisted_service_config(out_dir) or ServiceConfig()

    if args.telemetry_hz is not None:
        cfg.telemetry_hz = float(args.telemetry_hz)
    if args.config_hz is not None:
        cfg.config_hz = float(args.config_hz)
    if args.rampsoak_hz is not None:
        cfg.rampsoak_hz = float(args.rampsoak_hz)

    if args.zones is not None:
        if str(args.zones).strip().lower() == "auto":
            cfg.zones_mode = "auto"
            cfg.zones_list = [1, 2, 3, 4, 5, 6]
        else:
            cfg.zones_mode = "list"
            cfg.zones_list = [int(x.strip()) for x in str(args.zones).split(",") if x.strip()]

    if args.flush_each_line is not None:
        cfg.flush_each_line = bool(args.flush_each_line)

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
        logger.info("Service stopping due to KeyboardInterrupt")
        if args.verbose:
            print("[service] stopping (KeyboardInterrupt)")
        svc.stop_evt.set()
        try:
            time.sleep(0.1)
        except Exception:
            pass
        svc.close(service_shutdown=True)
    except Exception:
        logger.exception("Service terminated with unhandled exception")
        raise


if __name__ == "__main__":
    main()