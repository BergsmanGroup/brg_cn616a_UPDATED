"""Isolated synthetic baseline runner for the CN616A Tkinter GUI.

Run from the repository root with:
    .\\.venv\\Scripts\\python.exe tests\\gui_responsiveness_baseline.py

This runner never starts or connects to cn616a_service.py. GUI commands are
directed to a disposable fake JSON Lines server bound to 127.0.0.1.
"""

from __future__ import annotations

import argparse
import builtins
import ctypes
import gc
import gzip
import io
import itertools
import json
import logging
import math
import os
import platform
import shutil
import socketserver
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from ctypes import wintypes
from typing import TYPE_CHECKING, Any, Callable, Optional
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from baseline_reference import REFERENCE_DIR, adapt_reference, load_reference  # noqa: E402

if TYPE_CHECKING:
    import tkinter as tk
    from tkinter import ttk


ZONE_IDS = tuple(range(1, 7))
JSON_STATE_FILES = (
    "cn616a_telemetry_state.json",
    "cn616a_config_state.json",
    "cn616a_rampsoak_state.json",
    "cn616a_analysis_state.json",
)
LOG_PATTERNS = (
    "cn616a_telemetry_log*.jsonl",
    "cn616a_analysis_log*.jsonl",
    "cn616a_config_log*.jsonl",
    "cn616a_rampsoak_log*.jsonl",
    "cn616a_service_config_log*.jsonl",
)


@dataclass(frozen=True)
class Scenario:
    chart: str
    log_size: str
    service_mode: str


@dataclass
class RunMetrics:
    callbacks_ms: dict[str, list[float]] = field(default_factory=dict)
    log_reads: dict[str, dict[str, int]] = field(default_factory=dict)
    heartbeat_delay_ms: list[float] = field(default_factory=list)
    warmup_heartbeat_delay_ms: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    actions: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_callback(self, name: str, duration_ms: float) -> None:
        with self._lock:
            self.callbacks_ms.setdefault(name, []).append(duration_ms)

    def add_heartbeat(self, delay_ms: float) -> None:
        with self._lock:
            self.heartbeat_delay_ms.append(delay_ms)

    def add_log_read(self, callback: str, byte_count: int, line_count: int) -> None:
        with self._lock:
            totals = self.log_reads.setdefault(callback, {"bytes": 0, "lines": 0})
            totals["bytes"] += byte_count
            totals["lines"] += line_count


class TkCallbackInstrumentation:
    """Measure scheduled Tk callbacks without changing application files."""

    def __init__(self, metrics: RunMetrics):
        self.metrics = metrics
        self._original_after: Optional[Callable[..., Any]] = None
        self.current_callback = threading.local()

    def __enter__(self) -> "TkCallbackInstrumentation":
        import tkinter as tk

        self._original_after = tk.Misc.after
        original_after = self._original_after
        metrics = self.metrics

        def measured_after(widget, ms=None, func=None, *args):
            if not callable(func):
                return original_after(widget, ms, func, *args)

            owner = getattr(func, "__self__", None)
            method_name = getattr(func, "__name__", type(func).__name__)
            if owner is not None:
                name = f"{type(owner).__name__}.{method_name}"
            else:
                name = getattr(func, "__qualname__", method_name)

            def timed_callback(*callback_args):
                started = time.perf_counter()
                previous_callback = getattr(self.current_callback, "name", None)
                self.current_callback.name = name
                try:
                    return func(*callback_args)
                finally:
                    if previous_callback is None:
                        del self.current_callback.name
                    else:
                        self.current_callback.name = previous_callback
                    metrics.add_callback(name, (time.perf_counter() - started) * 1000.0)

            return original_after(widget, ms, timed_callback, *args)

        tk.Misc.after = measured_after
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        import tkinter as tk

        _ = (_exc_type, _exc_value, _traceback)
        if self._original_after is not None:
            tk.Misc.after = self._original_after


class MeasuredLogFile:
    """Proxy a JSONL stream and count bytes/newlines actually read."""

    def __init__(self, stream, metrics: RunMetrics, callback_name: str):
        self._stream = stream
        self._metrics = metrics
        self._callback_name = callback_name

    def _record(self, data: Any) -> None:
        if isinstance(data, str):
            encoding = getattr(self._stream, "encoding", None) or "utf-8"
            errors = getattr(self._stream, "errors", None) or "strict"
            byte_count = len(data.encode(encoding, errors=errors))
        else:
            byte_count = len(data)
        line_count = data.count("\n") if isinstance(data, str) else data.count(b"\n")
        self._metrics.add_log_read(self._callback_name, byte_count, line_count)

    def read(self, size: int = -1):
        value = self._stream.read(size)
        self._record(value)
        return value

    def readline(self, size: int = -1):
        value = self._stream.readline(size)
        self._record(value)
        return value

    def readlines(self, hint: int = -1):
        values = self._stream.readlines(hint)
        for value in values:
            self._record(value)
        return values

    def readinto(self, buffer):
        count = self._stream.readinto(buffer)
        if count:
            self._metrics.add_log_read(self._callback_name, int(count), 0)
        return count

    def __iter__(self):
        return self

    def __next__(self):
        value = next(self._stream)
        self._record(value)
        return value

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._stream.__exit__(exc_type, exc_value, traceback)

    def __getattr__(self, name):
        return getattr(self._stream, name)


class LogReadInstrumentation:
    """Count reads of history JSONL files under the temporary fixture only."""

    def __init__(
        self,
        logs_dir: Path,
        metrics: RunMetrics,
        callback_context: threading.local,
    ):
        self.logs_dir = logs_dir.resolve()
        self.metrics = metrics
        self.callback_context = callback_context
        self._original_builtin_open = builtins.open
        self._original_path_open = Path.open

    def _wrap(self, path, mode: str, stream):
        try:
            candidate = Path(path).resolve()
            candidate.relative_to(self.logs_dir)
        except (OSError, TypeError, ValueError):
            return stream
        if candidate.suffix.lower() != ".jsonl" or "r" not in mode:
            return stream
        callback_name = getattr(
            self.callback_context,
            "name",
            "GUI startup / event handling",
        )
        return MeasuredLogFile(stream, self.metrics, callback_name)

    def __enter__(self) -> "LogReadInstrumentation":
        original_builtin_open = self._original_builtin_open
        original_path_open = self._original_path_open

        def tracked_builtin_open(
            file,
            mode="r",
            buffering=-1,
            encoding=None,
            errors=None,
            newline=None,
            closefd=True,
            opener=None,
        ):
            stream = original_builtin_open(
                file, mode, buffering, encoding, errors, newline, closefd, opener
            )
            return self._wrap(file, mode, stream)

        def tracked_path_open(
            path,
            mode="r",
            buffering=-1,
            encoding=None,
            errors=None,
            newline=None,
        ):
            stream = original_path_open(
                path, mode, buffering, encoding, errors, newline
            )
            return self._wrap(path, mode, stream)

        builtins.open = tracked_builtin_open
        Path.open = tracked_path_open
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        _ = (_exc_type, _exc_value, _traceback)
        builtins.open = self._original_builtin_open
        Path.open = self._original_path_open


class LoopbackJsonlService:
    """Fake service endpoint; it has no controller or serial-port code."""

    def __init__(self, mode: str, delay_seconds: float = 0.35):
        if mode not in {"normal", "delayed", "disconnected", "error"}:
            raise ValueError(f"Unsupported fake service mode: {mode}")
        self.mode = mode
        self.delay_seconds = max(0.0, float(delay_seconds))
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.active_connections = 0
        self.max_active_connections = 0
        self._server = self._make_server()
        self.host, self.port = self._server.server_address
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="baseline-fake-service",
            daemon=True,
        )

    def _make_server(self):
        owner = self

        class RequestHandler(socketserver.StreamRequestHandler):
            def handle(self):
                with owner._lock:
                    owner.active_connections += 1
                    owner.max_active_connections = max(
                        owner.max_active_connections, owner.active_connections
                    )

                try:
                    raw = self.rfile.readline(1_048_577)
                    if not raw:
                        return
                    if len(raw) > 1_048_576:
                        return
                    try:
                        request = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        return
                    if not isinstance(request, dict):
                        return

                    started = time.perf_counter()
                    request_record = {
                        "op": str(request.get("op", "")),
                        "id": request.get("id"),
                        "started_at": datetime.now().astimezone().isoformat(),
                    }
                    with owner._lock:
                        owner.requests.append(request_record)

                    if owner.mode == "disconnected":
                        return
                    if owner.mode == "delayed":
                        time.sleep(owner.delay_seconds)

                    response = owner._response(request)
                    self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
                    self.wfile.flush()
                    with owner._lock:
                        request_record["response_ms"] = (
                            time.perf_counter() - started
                        ) * 1000.0
                finally:
                    with owner._lock:
                        owner.active_connections -= 1

        class ThreadingServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = ThreadingServer(("127.0.0.1", 0), RequestHandler)
        server.timeout = 0.2
        return server

    def _response(self, request: dict[str, Any]) -> dict[str, Any]:
        response: dict[str, Any] = {
            "id": request.get("id"),
            "ok": self.mode != "error",
        }
        if self.mode == "error":
            response["error"] = "Synthetic baseline service error"
            return response

        op = request.get("op")
        if op == "get_run_status":
            response.update(
                {
                    "run_active": False,
                    "run_schedule_enabled": False,
                    "run_recovered": False,
                }
            )
        elif op in {"get_status", "get_service_config"}:
            response["service_config"] = {
                "config": {"zones_mode": "all", "zones_list": list(ZONE_IDS)}
            }
        elif op == "read_rampsoak":
            response["zones"] = {
                str(zone): {
                    "num_segments": 20,
                    "start_profile": "DISABLE",
                    "segments": [],
                }
                for zone in ZONE_IDS
            }
        return response

    def start(self) -> None:
        if self.host != "127.0.0.1":
            raise RuntimeError("The fake service must bind only to loopback")
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            requests = [dict(item) for item in self.requests]
            max_active = self.max_active_connections
        response_times = [
            item["response_ms"] for item in requests if "response_ms" in item
        ]
        return {
            "mode": self.mode,
            "host": self.host,
            "request_count": len(requests),
            "operations": [item["op"] for item in requests],
            "max_active_connections": max_active,
            "response_latency_ms": summarize(response_times),
        }

class SyntheticLogWriter:
    """Write controllable telemetry/state fixtures under a temporary directory."""

    def __init__(
        self,
        logs_dir: Path,
        *,
        sample_rate_hz: float,
        duration_seconds: float,
        service_port: int,
        copy_logs_from: Optional[Path] = None,
        end_time: Optional[datetime] = None,
        reference_dir: Path = REFERENCE_DIR,
    ):
        self.logs_dir = logs_dir
        self.sample_rate_hz = float(sample_rate_hz)
        self.duration_seconds = float(duration_seconds)
        self.service_port = int(service_port)
        self.copy_logs_from = copy_logs_from
        self.end_time = end_time
        self.reference = adapt_reference(reference_dir)
        self.copied_files: list[str] = []
        self.generated_records = 0
        self._write_lock = threading.Lock()
        self._write_fixture()

    def _copy_source_logs(self) -> None:
        if self.copy_logs_from is None:
            return
        source = self.copy_logs_from.resolve()
        if not source.is_dir():
            raise FileNotFoundError(f"Log source directory does not exist: {source}")

        candidates: set[Path] = set()
        for pattern in LOG_PATTERNS:
            candidates.update(source.glob(pattern))

        for index, path in enumerate(sorted(candidates)):
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size == 0:
                continue
            category = next(
                pattern.split("_log", 1)[0].replace("cn616a_", "")
                for pattern in LOG_PATTERNS
                if path.name.startswith(pattern.split("*", 1)[0])
            )
            target = self.logs_dir / f"cn616a_{category}_log_source_copy_{index:03d}.jsonl"
            shutil.copy2(path, target)
            self.copied_files.append(path.name)

    def _telemetry_record(self, timestamp: datetime, sequence: int) -> dict[str, Any]:
        return {
            "timestamp_pacific": timestamp.isoformat(timespec="milliseconds"),
            "ts": timestamp.isoformat(timespec="milliseconds"),
            "unit": 1,
            "port": "SYNTHETIC",
            "zones": list(ZONE_IDS),
            "telemetry": self.reference.telemetry[sequence % len(self.reference.telemetry)],
        }

    def _analysis_record(self, timestamp: datetime, sequence: int) -> dict[str, Any]:
        zones = self.reference.telemetry[sequence % len(self.reference.telemetry)]["zones"]
        return {
            "timestamp_pacific": timestamp.isoformat(timespec="milliseconds"),
            "ts": timestamp.isoformat(timespec="milliseconds"),
            "unit": 1,
            "analysis": {
                str(zone): {
                    "avg_abs_error_c": round(abs(zones[str(zone)]["pv_c"] - zones[str(zone)]["sp_abs"]), 3),
                    "equilibrium": False,
                    "point_count": 30,
                }
                for zone in ZONE_IDS
            },
        }

    def _write_jsonl(self, path: Path, records: list[dict[str, Any]]) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            for record in records:
                stream.write(json.dumps(record, separators=(",", ":")) + "\n")

    def _write_rotated_records(
        self,
        stem: str,
        records: list[dict[str, Any]],
        records_per_file: int,
    ) -> None:
        chunks = [
            records[index : index + records_per_file]
            for index in range(0, len(records), records_per_file)
        ]
        for index, chunk in enumerate(chunks):
            is_active = index == len(chunks) - 1
            if is_active:
                name = f"{stem}.jsonl"
            else:
                name = f"{stem}_baseline_{index:03d}.jsonl"
            self._write_jsonl(self.logs_dir / name, chunk)

    def _write_json(self, filename: str, value: dict[str, Any]) -> None:
        target = self.logs_dir / filename
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
        # Windows refuses to replace a file the GUI has open for reading; retry briefly.
        for attempt in range(20):
            try:
                os.replace(temporary, target)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.01)

    def _write_fixture(self) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._copy_source_logs()

        duration = max(self.duration_seconds, 1.0 / max(self.sample_rate_hz, 0.01))
        count = max(1, int(math.ceil(duration * self.sample_rate_hz)))
        step = 1.0 / self.sample_rate_hz
        end = self.end_time or datetime.now(timezone.utc)
        start = end - timedelta(seconds=(count - 1) * step)
        telemetry_records = [
            self._telemetry_record(start + timedelta(seconds=index * step), index)
            for index in range(count)
        ]
        analysis_records = [
            self._analysis_record(start + timedelta(seconds=index * step), index)
            for index in range(count)
        ]
        records_per_file = max(1, int(self.sample_rate_hz * 900))
        self._write_rotated_records(
            "cn616a_telemetry_log", telemetry_records, records_per_file
        )
        self._write_rotated_records(
            "cn616a_analysis_log", analysis_records, records_per_file
        )
        self.generated_records = count
        latest = telemetry_records[-1]

        self._write_json(
            "cn616a_telemetry_state.json",
            {
                "ts": latest["ts"],
                "unit": 1,
                "port": "SYNTHETIC",
                "zones": list(ZONE_IDS),
                "telemetry": latest["telemetry"],
            },
        )
        self._write_json(
            "cn616a_config_state.json",
            {
                "ts": latest["ts"],
                "unit": 1,
                "port": "SYNTHETIC",
                "zones": list(ZONE_IDS),
                "config": self.reference.config,
            },
        )
        self._write_json(
            "cn616a_rampsoak_state.json",
            {
                "ts": latest["ts"],
                "unit": 1,
                "port": "SYNTHETIC",
                "zones": list(ZONE_IDS),
                "rampsoak": self.reference.rampsoak,
            },
        )
        self._write_json(
            "cn616a_analysis_state.json",
            {
                "ts": latest["ts"],
                "unit": 1,
                "port": "SYNTHETIC",
                "zones": list(ZONE_IDS),
                "analysis": self._analysis_record(
                    datetime.fromisoformat(latest["ts"]), count - 1
                )["analysis"],
            },
        )
        self.write_service_config()

    def write_service_config(self) -> None:
        self._write_json(
            "cn616a_service_config_state.json",
            {
                "ts": datetime.now().astimezone().isoformat(),
                "config": {
                    "gui_refresh_hz": 2.0,
                    "telemetry_poll_hz": self.sample_rate_hz,
                    "analysis_poll_hz": self.sample_rate_hz,
                    "zones_mode": "all",
                    "zones_list": list(ZONE_IDS),
                    "zone_names": {str(zone): f"Synthetic Zone {zone}" for zone in ZONE_IDS},
                    "viewer": {
                        "history_hours": self.duration_seconds / 3600.0,
                        "show_sp_abs": True,
                        "show_sp_autotune": True,
                        "show_mae": True,
                        "line_width": 1.5,
                    },
                    "last_tcp_host": "127.0.0.1",
                    "last_tcp_port": self.service_port,
                },
            },
        )

    def append_live_record(self, sequence: int) -> None:
        now = datetime.now(timezone.utc)
        record = self._telemetry_record(now, self.generated_records + sequence)
        with self._write_lock:
            with (self.logs_dir / "cn616a_telemetry_log.jsonl").open(
                "a", encoding="utf-8", newline="\n"
            ) as stream:
                stream.write(json.dumps(record, separators=(",", ":")) + "\n")
                stream.flush()
            self._write_json(
                "cn616a_telemetry_state.json",
                {
                    "ts": record["ts"],
                    "unit": 1,
                    "port": "SYNTHETIC",
                    "zones": list(ZONE_IDS),
                    "telemetry": record["telemetry"],
                },
            )

    def file_inventory(self) -> dict[str, Any]:
        files = [
            path
            for pattern in LOG_PATTERNS
            for path in self.logs_dir.glob(pattern)
            if path.is_file()
        ]
        return {
            "files": len(files),
            "bytes": sum(path.stat().st_size for path in files),
            "lines": sum(_count_lines(path) for path in files),
            "rotated_files": sum("_baseline_" in path.name or "_source_copy_" in path.name for path in files),
            "generated_telemetry_records": self.generated_records,
            "generated_analysis_records": self.generated_records,
            "reference_content_sha256": self.reference.manifest["content_sha256"],
            "reference_archive_bytes": self.reference.manifest["archive_bytes"],
            "copied_source_files": list(self.copied_files),
        }


class LiveDataWriter:
    def __init__(self, fixture: SyntheticLogWriter, interval_seconds: float):
        self.fixture = fixture
        self.interval_seconds = max(0.0, interval_seconds)
        self._stop = threading.Event()
        self.records_written = 0
        self.error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.interval_seconds <= 0:
            return

        def write_until_stopped():
            while not self._stop.wait(self.interval_seconds):
                try:
                    self.fixture.append_live_record(self.records_written + 1)
                except (OSError, ValueError) as exc:
                    self.error = f"Synthetic writer: {type(exc).__name__}: {exc}"
                    self._stop.set()
                    return
                self.records_written += 1

        self._thread = threading.Thread(
            target=write_until_stopped,
            name="baseline-synthetic-live-data",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def _count_lines(path: Path) -> int:
    with path.open("rb") as stream:
        return sum(1 for _ in stream)


def _peak_working_set_bytes() -> tuple[Optional[int], Optional[str]]:
    if os.name != "nt":
        return None, "Windows process-memory API is unavailable on this platform"

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    try:
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        if not psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            return None, str(error)
        return int(counters.PeakWorkingSetSize), None
    except (AttributeError, OSError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def summarize(values: list[float]) -> dict[str, Optional[float] | int]:
    if not values:
        return {
            "sample_count": 0,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
        }
    ordered = sorted(float(value) for value in values)

    def nearest_rank(percent: float) -> float:
        index = max(0, math.ceil(percent * len(ordered)) - 1)
        return round(ordered[index], 3)

    return {
        "sample_count": len(ordered),
        "p50": nearest_rank(0.50),
        "p95": nearest_rank(0.95),
        "p99": nearest_rank(0.99),
        "max": round(ordered[-1], 3),
    }


def _find_notebook(root: tk.Misc) -> Optional[ttk.Notebook]:
    from tkinter import ttk

    for child in root.winfo_children():
        if isinstance(child, ttk.Notebook):
            return child
        nested = _find_notebook(child)
        if nested is not None:
            return nested
    return None


def _file_dialog_button(run_panel) -> Optional[tk.Widget]:
    for child in run_panel.winfo_children():
        for nested in child.winfo_children():
            if str(nested.cget("text")) == "Browse...":
                return nested
    return None


def _simulate_entry_typing(entry: ttk.Entry, text: str) -> bool:
    import tkinter as tk

    entry.focus_force()
    entry.update()
    entry.event_generate("<FocusIn>")
    entry.update_idletasks()
    keysym_map = {"-": "minus", ".": "period"}
    for character in text:
        keysym = keysym_map.get(character, character)
        entry.event_generate("<KeyPress>", keysym=keysym)
        entry.event_generate("<KeyRelease>", keysym=keysym)
    if entry.get() == text:
        return True

    entry.delete(0, tk.END)
    entry.insert(0, text)
    return False


def _run_gui_scenario(
    scenario: Scenario,
    logs_dir: Path,
    fixture: SyntheticLogWriter,
    fake_service: LoopbackJsonlService,
    *,
    duration_seconds: float,
    growth_interval_seconds: float,
    feedback_samples: int = 20,
) -> dict[str, Any]:
    import tkinter as tk
    from py.gui import main_gui
    from py.gui import run_panel as run_panel_module

    metrics = RunMetrics()
    cpu_started = time.process_time()
    startup_started = time.perf_counter()
    app = None
    gui_logger = logging.getLogger("cn616a.gui")
    original_handlers = list(gui_logger.handlers)
    original_logger_level = gui_logger.level
    original_logger_propagate = gui_logger.propagate
    original_excepthook = sys.excepthook
    original_thread_excepthook = threading.excepthook
    heartbeat_interval_ms = 50
    writer = LiveDataWriter(fixture, interval_seconds=growth_interval_seconds)

    try:
        with patch.dict(os.environ, {"CN616A_GUI_ALLOW_UNSAFE_CHART": ""}), TkCallbackInstrumentation(metrics) as callback_instrumentation, LogReadInstrumentation(
            logs_dir, metrics, callback_instrumentation.current_callback
        ):
            callback_instrumentation.current_callback.name = "GUI startup"
            try:
                app = main_gui.CN616AGUI(
                    logs_dir=logs_dir,
                    refresh_interval=0.5,
                    debug=False,
                    allow_unsafe_chart=False,
                )
            finally:
                del callback_instrumentation.current_callback.name
            def report_callback_exception(exc, value, traceback):
                metrics.errors.append(f"Tk callback: {exc.__name__}: {value}")
                app._report_callback_exception(exc, value, traceback)

            app.root.report_callback_exception = report_callback_exception
            app.root.protocol("WM_DELETE_WINDOW", app._on_closing)
            startup_ms = (time.perf_counter() - startup_started) * 1000.0
            root = app.root
            notebook = _find_notebook(root)
            if notebook is None:
                raise RuntimeError("Could not locate the GUI tab notebook")

            heartbeat_due = time.perf_counter() + heartbeat_interval_ms / 1000.0
            heartbeat_after_id = None

            def heartbeat():
                nonlocal heartbeat_due, heartbeat_after_id
                heartbeat_after_id = None
                now = time.perf_counter()
                delay_ms = max(0.0, (now - heartbeat_due) * 1000.0)
                if now - scenario_started < 2.0:
                    metrics.warmup_heartbeat_delay_ms.append(delay_ms)
                else:
                    metrics.add_heartbeat(delay_ms)
                heartbeat_due = now + heartbeat_interval_ms / 1000.0
                if root.winfo_exists():
                    heartbeat_after_id = root.after(heartbeat_interval_ms, heartbeat)

            heartbeat_after_id = root.after(heartbeat_interval_ms, heartbeat)
            writer.start()
            scenario_started = time.perf_counter()

            def finish():
                nonlocal heartbeat_after_id
                metrics.actions["elapsed_seconds"] = round(
                    time.perf_counter() - scenario_started, 3
                )
                if heartbeat_after_id is not None:
                    try:
                        root.after_cancel(heartbeat_after_id)
                    except tk.TclError:
                        pass
                    heartbeat_after_id = None
                if app is not None:
                    app._on_closing()

            def choose_chart():
                if scenario.chart == "unopened":
                    return
                notebook.select(app.chart_panel_frame)
                root.update_idletasks()
                root.update()
                metrics.actions["chart_initialized"] = app.chart_panel is not None
                metrics.actions["chart_available"] = bool(app.chart_available)
                if scenario.chart == "hidden":
                    root.after(2000, hide_chart)

            def hide_chart():
                notebook.select(0)
                root.update_idletasks()
                metrics.actions["chart_hidden_after_open"] = True

            def exercise_controls():
                try:
                    panel = app.command_panel
                    if panel is not None:
                        panel.unit_combo.current(0)
                        panel.unit_combo.event_generate("<<ComboboxSelected>>")
                        if panel.zone_combo.cget("values"):
                            panel.zone_combo.current(0)
                            panel.zone_combo.event_generate("<<ComboboxSelected>>")
                        sp_typed = _simulate_entry_typing(panel.sp_abs_entry, "80")
                        autotune_typed = _simulate_entry_typing(
                            panel.sp_autotune_entry, "81"
                        )
                        panel.mode_combo.current(0)
                        panel.mode_combo.event_generate("<<ComboboxSelected>>")
                        metrics.actions["entry_keyboard_events_replaced_text"] = {
                            "setpoint": sp_typed,
                            "autotune": autotune_typed,
                        }

                        feedback_values: list[float] = []

                        def invoke_command(sample_index: int):
                            panel.sp_abs_var.set("80")
                            panel.sp_autotune_var.set("81")
                            panel.mode_var.set("PID")
                            started = time.perf_counter()
                            panel.apply_button.invoke()

                            def first_idle_feedback():
                                elapsed_ms = (time.perf_counter() - started) * 1000.0
                                feedback_values.append(elapsed_ms)
                                metrics.actions[
                                    "command_button_first_idle_feedback_ms"
                                ] = round(elapsed_ms, 3)
                                metrics.actions["command_status_after_action"] = (
                                    panel.status_label.cget("text")
                                )
                                metrics.actions["command_button_action"] = (
                                    "tk.Button.invoke"
                                )
                                metrics.actions["command_feedback_samples_ms"] = list(
                                    feedback_values
                                )
                                if sample_index + 1 < feedback_samples:
                                    root.after(
                                        300,
                                        lambda: invoke_command(sample_index + 1),
                                    )

                            root.after_idle(first_idle_feedback)

                        invoke_command(0)

                    run_panel = next(
                        (candidate for candidate in app.panels if isinstance(candidate, run_panel_module.RunPanel)),
                        None,
                    )
                    if run_panel is not None:
                        browse = _file_dialog_button(run_panel)
                        if browse is not None:
                            with patch.object(
                                run_panel_module.filedialog,
                                "askdirectory",
                                return_value=str(logs_dir),
                            ):
                                browse.invoke()
                            metrics.actions["folder_picker"] = (
                                "Browse callback exercised with a fake chooser; "
                                "native Windows dialog not opened"
                            )
                    metrics.actions["dropdown_and_edit_events"] = True
                except Exception as exc:
                    metrics.errors.append(
                        f"GUI interaction simulation: {type(exc).__name__}: {exc}"
                    )

            root.after(250, choose_chart)
            root.after(2500, exercise_controls)
            root.after(max(1, int(duration_seconds * 1000)), finish)
            root.mainloop()
    finally:
        writer.close()
        if writer.error:
            metrics.errors.append(writer.error)
        try:
            if app is not None:
                try:
                    if app.root.winfo_exists():
                        app._on_closing()
                except tk.TclError:
                    pass
        finally:
            for handler in list(gui_logger.handlers):
                if handler not in original_handlers:
                    gui_logger.removeHandler(handler)
                    handler.flush()
                    handler.close()
            gui_logger.setLevel(original_logger_level)
            gui_logger.propagate = original_logger_propagate
            sys.excepthook = original_excepthook
            threading.excepthook = original_thread_excepthook

    callback_summaries = {
        name: summarize(samples)
        for name, samples in sorted(metrics.callbacks_ms.items())
    }
    peak_working_set_bytes, memory_measurement_error = _peak_working_set_bytes()
    slow_callbacks = sorted(
        (
            (name, summary)
            for name, summary in callback_summaries.items()
            if summary["max"] is not None
        ),
        key=lambda item: float(item[1]["p95"] or 0.0),
        reverse=True,
    )[:20]
    return {
        "scenario": {
            "chart": scenario.chart,
            "log_size": scenario.log_size,
            "service_mode": scenario.service_mode,
        },
        "runtime": {
            "python": sys.version.split()[0],
            "tk": tk.TkVersion,
            "platform": platform.platform(),
            "executable": sys.executable,
        },
        "gui_configuration": {
            "refresh_interval_seconds": round(float(app.refresh_interval), 3),
            "enabled_zones": list(ZONE_IDS),
            "chart_history_hours": round(fixture.duration_seconds / 3600.0, 4),
        },
        "startup_ms": round(startup_ms, 3),
        "duration_seconds": metrics.actions.get("elapsed_seconds"),
        "warmup_seconds": 2.0,
        "warmup_heartbeat_delay_ms": summarize(metrics.warmup_heartbeat_delay_ms),
        "event_loop_heartbeat_delay_ms": summarize(metrics.heartbeat_delay_ms),
        "command_feedback_latency_ms": summarize(
            [float(value) for value in metrics.actions.get("command_feedback_samples_ms", [])]
        ),
        "jsonl_reads": {
            "total_bytes_read": sum(item["bytes"] for item in metrics.log_reads.values()),
            "total_newline_delimited_lines_read": sum(
                item["lines"] for item in metrics.log_reads.values()
            ),
            "by_callback": metrics.log_reads,
        },
        "scheduled_callback_duration_ms": {
            "callback_types": len(callback_summaries),
            "top_slowest_by_p95": [
                {"callback": name, **summary} for name, summary in slow_callbacks
            ],
        },
        "gui_actions": {
            key: value
            for key, value in metrics.actions.items()
            if key != "command_feedback_started_at"
        },
        "fake_service": fake_service.snapshot(),
        "log_inventory_final": fixture.file_inventory(),
        "synthetic_live_records_added": writer.records_written,
        "process_cpu_seconds": round(time.process_time() - cpu_started, 3),
        "peak_working_set_bytes": peak_working_set_bytes,
        "memory_measurement_error": memory_measurement_error,
        "errors": metrics.errors,
        "validation_limits": [
            "No real service or controller was contacted.",
            "Controller write latency and hardware behavior were not measured.",
            "The folder-picker callback used a fake chooser; the native dialog was not tested.",
            "Button actions use Tk's invoke() method, not OS-level mouse injection.",
        ],
    }


def _scenario_duration(log_size: str, hours: float) -> float:
    if log_size == "small":
        return min(max(hours * 3600.0, 1.0), 60.0)
    return max(hours * 3600.0, 1.0)


def _run_scenario(
    scenario: Scenario,
    *,
    hours: float,
    sample_rate_hz: float,
    duration_seconds: float,
    delay_seconds: float,
    growth_interval_seconds: float,
    copy_logs_from: Optional[Path],
    feedback_samples: int = 20,
    reference_dir: Path = REFERENCE_DIR,
) -> dict[str, Any]:
    fake_service = LoopbackJsonlService(scenario.service_mode, delay_seconds)
    fake_service.start()
    try:
        with tempfile.TemporaryDirectory(prefix="cn616a-gui-baseline-") as temp_dir:
            logs_dir = Path(temp_dir) / "logs"
            fixture = SyntheticLogWriter(
                logs_dir,
                sample_rate_hz=sample_rate_hz,
                duration_seconds=_scenario_duration(scenario.log_size, hours),
                service_port=fake_service.port,
                copy_logs_from=copy_logs_from,
                reference_dir=reference_dir,
            )
            initial_inventory = fixture.file_inventory()
            result = _run_gui_scenario(
                scenario,
                logs_dir,
                fixture,
                fake_service,
                duration_seconds=duration_seconds,
                growth_interval_seconds=growth_interval_seconds,
                feedback_samples=feedback_samples,
            )
            # Collect the destroyed GUI's reference cycles on the Tk thread; collection
            # on a helper thread makes tkinter.Variable.__del__ raise RuntimeError.
            gc.collect()
            result["log_inventory_initial"] = initial_inventory
            return result
    finally:
        fake_service.close()


def verify_data(hours: float = 1.0, sample_rate_hz: float = 2.0) -> dict[str, Any]:
    """Measure the portable input and expanded workload without Tk or sockets."""
    if not math.isfinite(hours) or not math.isfinite(sample_rate_hz) or hours <= 0 or sample_rate_hz <= 0:
        raise ValueError("History hours and sample rate must be finite and positive")
    _, manifest = load_reference()
    with tempfile.TemporaryDirectory(prefix="cn616a-baseline-data-check-") as temporary:
        fixture = SyntheticLogWriter(
            Path(temporary), sample_rate_hz=sample_rate_hz,
            duration_seconds=hours * 3600.0, service_port=12345,
            end_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        inventory = fixture.file_inventory()
        compressed_buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=compressed_buffer, mode="wb", filename="", mtime=0, compresslevel=9) as compressed:
            for path in sorted(fixture.logs_dir.glob("*.jsonl")):
                with path.open("rb") as source:
                    shutil.copyfileobj(source, compressed)
        compressed_bytes = len(compressed_buffer.getvalue())
    return {
        "reference": manifest,
        "history_hours": hours,
        "sample_rate_hz": sample_rate_hz,
        "expanded_jsonl": inventory,
        "expanded_jsonl_gzip_bytes": compressed_bytes,
        "compression_ratio": round(inventory["bytes"] / compressed_bytes, 2),
        "gui_started": False,
        "network_started": False,
        "note": "Only the compressed raw reference is stored in Git; expanded GUI logs are generated temporarily.",
    }


def _number(value: Any, unit: str = "ms") -> str:
    return "unavailable" if value is None else f"{float(value):.1f} {unit}"


def format_performance_summary(report: dict[str, Any]) -> str:
    """Present measured takeaways, never a success-shaped result for missing data."""
    targets = report.get("targets_ms", {
        "event_loop_p95": 100.0, "event_loop_p99": 250.0,
        "visible_button_feedback_p95": 150.0,
    })
    lines = ["CN616A responsiveness - synthetic data / fake service only"]
    if "reference" in report:
        lines.append(f"Input v1 SHA-256: {report['reference']['content_sha256']}")
    else:
        lines.append("Legacy report: no pinned raw input; compare cautiously with report version 2.")
    for result in report["results"]:
        scenario = result["scenario"]
        heartbeat = result["event_loop_heartbeat_delay_ms"]
        feedback = result.get("command_feedback_latency_ms", summarize(
            result.get("gui_actions", {}).get("command_feedback_samples_ms", [])
        ))
        errors = result.get("errors", [])
        chart_missing = scenario["chart"] != "unopened" and not result.get("gui_actions", {}).get("chart_initialized", False)
        missed = [
            label
            for label, value, limit in (
                ("event-loop p95", heartbeat.get("p95"), targets["event_loop_p95"]),
                ("event-loop p99", heartbeat.get("p99"), targets["event_loop_p99"]),
                ("button feedback", feedback.get("p95"), targets["visible_button_feedback_p95"]),
            )
            if value is not None and float(value) >= limit
        ]
        limited = heartbeat.get("sample_count", 0) < 100 or feedback.get("sample_count", 0) < 20
        if errors:
            status = "ERROR"
        elif chart_missing or not heartbeat.get("sample_count") or not feedback.get("sample_count"):
            status = "INCOMPLETE"
        elif missed:
            status = "TARGET MISSED"
        elif limited:
            status = "LIMITED SAMPLES"
        else:
            status = "WITHIN TARGET (this workload only)"
        lines.extend([
            "",
            f"{scenario['log_size']}/{scenario['chart']}/{scenario['service_mode']}  {status}",
            f"  UI delay: p95 {_number(heartbeat.get('p95'))}; p99 {_number(heartbeat.get('p99'))}; max {_number(heartbeat.get('max'))}; n={heartbeat.get('sample_count', 0)}",
            f"  Button feedback: p95 {_number(feedback.get('p95'))}; n={feedback.get('sample_count', 0)}  (target <{targets['visible_button_feedback_p95']:g} ms)",
        ])
        callbacks = [
            callback
            for callback in result.get("scheduled_callback_duration_ms", {}).get("top_slowest_by_p95", [])
            if not callback["callback"].startswith("_run_gui_scenario.")
        ]
        if callbacks:
            worst = max(callbacks, key=lambda item: float(item.get("p95") or 0))
            lines.append(f"  Slowest app callback: {worst['callback']} p95 {_number(worst.get('p95'))}")
        read_bytes = result.get("jsonl_reads", {}).get("total_bytes_read")
        initial_bytes = result.get("log_inventory_initial", {}).get("bytes")
        if read_bytes is not None and initial_bytes:
            lines.append(f"  History I/O: {read_bytes / 1048576:.1f} MiB read; {read_bytes / initial_bytes:.1f}x fixture-size amplification")
        memory = result.get("peak_working_set_bytes")
        memory_text = "unavailable" if memory is None else f"{memory / 1048576:.1f} MiB"
        lines.append(f"  Startup {_number(result.get('startup_ms'))}; CPU {_number(result.get('process_cpu_seconds'), 's')}; process peak RAM {memory_text}")
        duration = result.get("duration_seconds")
        updates = result.get("synthetic_live_records_added")
        if duration and updates is not None:
            lines.append(f"  Live input: {updates} new records / {duration:g} s ({updates / duration:.2f} Hz)")
        if missed:
            lines.append("  Takeaway: target exceeded for " + ", ".join(missed) + ".")
        if limited:
            lines.append("  Sampling: fewer than 100 heartbeats or 20 clicks; increase --duration-seconds before trusting tail percentiles.")
        if chart_missing:
            lines.append("  Chart unavailable/not initialized: use supported Python 3.12; the chart safety guard is not bypassed.")
        if errors:
            lines.extend(f"  Error: {error}" for error in errors)
        if result.get("memory_measurement_error"):
            lines.append(f"  Memory: {result['memory_measurement_error']}")
    lines.extend([
        "",
        "UI feedback is time to the next Tk idle opportunity, not a measured screen repaint.",
        "Instrumentation adds overhead. Native dialogs, hardware timing and real-time soak are not verified.",
    ])
    return "\n".join(lines)


def format_data_summary(report: dict[str, Any]) -> str:
    reference = report["reference"]
    inventory = report["expanded_jsonl"]
    return "\n".join([
        "CN616A portable baseline data - no GUI or network started",
        f"  Immutable raw input: {reference['sample_count']} Modbus snapshots; gzip {reference['archive_bytes']:,} bytes; raw {reference['uncompressed_bytes']:,} bytes",
        f"  Expanded {report['history_hours']:g} h at {report['sample_rate_hz']:g} Hz: {inventory['generated_telemetry_records']:,} telemetry + {inventory['generated_analysis_records']:,} derived analysis rows",
        f"  Runtime JSONL: {inventory['bytes']:,} bytes; gzip equivalent {report['expanded_jsonl_gzip_bytes']:,} bytes ({report['compression_ratio']:.1f}x smaller)",
        f"  Reference SHA-256: {reference['content_sha256']}",
        "  Git stores the small compressed raw seed, not the expanded runtime logs.",
    ])


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure the real CN616A Tk GUI using temporary synthetic logs and "
            "a loopback-only fake service. Never starts the controller service."
        )
    )
    parser.add_argument("--reference-dir", type=Path, default=REFERENCE_DIR, help="Versioned raw input corpus to replay offline.")
    parser.add_argument("--chart", choices=("unopened", "open", "hidden"), default="unopened")
    parser.add_argument("--log-size", choices=("small", "large"), default="large")
    parser.add_argument(
        "--service-mode",
        choices=("normal", "delayed", "disconnected", "error"),
        default="normal",
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--matrix",
        action="store_true",
        help="Run all chart/log-size/service-mode combinations (24 GUI sessions).",
    )
    scope.add_argument("--quick", action="store_true", help="Compare large-history chart-open normal and delayed fake-service sessions.")
    scope.add_argument("--verify-data", action="store_true", help="Validate the immutable raw reference and measure workload/compression sizes without a GUI or network.")
    scope.add_argument("--summarize", type=Path, help="Print refined metrics from an existing JSON report without starting a GUI or network.")
    parser.add_argument("--hours", type=float, default=1.0, help="Large-fixture history duration.")
    parser.add_argument("--sample-rate-hz", type=float, default=2.0)
    parser.add_argument("--duration-seconds", type=float, default=30.0)
    parser.add_argument("--feedback-samples", type=int, default=20, help="Maximum simulated button actions per session; summaries require at least 20 for a within-target result.")
    parser.add_argument("--delay-seconds", type=float, default=0.35)
    parser.add_argument(
        "--growth-interval-seconds",
        type=float,
        default=0.5,
        help="Synthetic live append interval; set 0 to disable.",
    )
    parser.add_argument(
        "--copy-logs-from",
        type=Path,
        help=(
            "Optionally copy recognized JSONL history files into the temporary "
            "fixture. Source files are read-only and never modified."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the JSON report here; defaults to a timestamped file in the OS temp directory.",
    )
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    values = (args.hours, args.sample_rate_hz, args.duration_seconds, args.delay_seconds, args.growth_interval_seconds)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Numeric workload options must be finite")
    if args.hours <= 0 or args.sample_rate_hz <= 0:
        raise ValueError("--hours and --sample-rate-hz must be positive")
    if args.duration_seconds < 3 or args.feedback_samples < 1 or args.delay_seconds < 0 or args.growth_interval_seconds < 0:
        raise ValueError("Duration must be at least 3 seconds; feedback samples positive; delay/growth intervals nonnegative")
    if args.copy_logs_from is not None and not args.copy_logs_from.is_dir():
        raise FileNotFoundError(f"--copy-logs-from is not a directory: {args.copy_logs_from}")

    if args.matrix:
        scenarios = [
            Scenario(chart, log_size, service_mode)
            for chart, log_size, service_mode in itertools.product(
                ("unopened", "open", "hidden"),
                ("small", "large"),
                ("normal", "delayed", "disconnected", "error"),
            )
        ]
    elif args.quick:
        scenarios = [Scenario("open", "large", mode) for mode in ("normal", "delayed")]
    else:
        scenarios = [Scenario(args.chart, args.log_size, args.service_mode)]

    report = {
        "report_version": 2,
        "reference": load_reference(args.reference_dir)[1],
        "workload_encoding": "Frozen raw Modbus input decoded by the current driver; temporary service/GUI JSON adapter.",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "safety": {
            "real_service_started": False,
            "real_service_contacted": False,
            "controller_connected": False,
            "production_logs_modified": False,
            "test_storage": "temporary directory; removed after each scenario",
        },
        "scenario_count": len(scenarios),
        "results": [
            _run_scenario(
                scenario,
                hours=args.hours,
                sample_rate_hz=args.sample_rate_hz,
                duration_seconds=args.duration_seconds,
                delay_seconds=args.delay_seconds,
                growth_interval_seconds=args.growth_interval_seconds,
                copy_logs_from=args.copy_logs_from,
                feedback_samples=args.feedback_samples,
                reference_dir=args.reference_dir,
            )
            for scenario in scenarios
        ],
        "targets_ms": {
            "event_loop_p95": 100.0,
            "event_loop_p99": 250.0,
            "visible_button_feedback_p95": 150.0,
        },
        "interpretation": (
            "Targets are comparison goals, not pass/fail claims. Results are "
            "synthetic GUI/fake-service measurements; hardware latency is not measured."
        ),
    }
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if args.summarize:
        try:
            report = json.loads(args.summarize.read_text(encoding="utf-8"))
            print(format_performance_summary(report))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.exit(2, f"Cannot summarize report: {exc}\n")
        return 0
    try:
        if args.verify_data:
            report = verify_data(args.hours, args.sample_rate_hz)
        else:
            try:
                import tkinter as tk
            except ImportError as exc:
                parser.exit(2, f"Tk is required for GUI performance checks: {exc}\n")
            try:
                report = run(args)
            except tk.TclError as exc:
                parser.exit(2, f"Unable to run Tk: {exc}. Use an interactive desktop with Python 3.12.\n")
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        parser.exit(2, f"Baseline failed: {exc}\n")

    output_path = args.output
    if output_path is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = Path(tempfile.gettempdir()) / f"cn616a_gui_baseline_{timestamp}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(format_data_summary(report) if args.verify_data else format_performance_summary(report))
    print(f"\nJSON report: {output_path.resolve()}")
    if not args.verify_data and any(result.get("errors") for result in report["results"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
