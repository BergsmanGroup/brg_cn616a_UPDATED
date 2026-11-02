"""Bounded background log and diagnostic presentation for the Qt GUI."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import traceback
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from .adapter import AdapterStatus, CommandOutcome


MAX_DISPLAY_ROWS = 5_000
MAX_INITIAL_BYTES = 512 * 1024
MAX_READ_BYTES = 128 * 1024
MAX_LINE_BYTES = 64 * 1024
MAX_RENDERED_CHARS = 2_000
MAX_GUI_EXCEPTIONS = 100
MAX_GUI_EXCEPTION_CHARS = 16_000
_POLL_INTERVAL_S = 0.75
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?")


@dataclass(frozen=True, slots=True)
class _FileCategory:
    active_name: str
    jsonl: bool
    recording_files: bool = False


_FILE_CATEGORIES: dict[str, _FileCategory] = {
    "Service diagnostics": _FileCategory(
        "cn616a_service_error.log", False
    ),
    "Telemetry JSONL": _FileCategory(
        "cn616a_telemetry_log.jsonl", True
    ),
    "Configuration JSONL": _FileCategory(
        "cn616a_config_log.jsonl", True
    ),
    "Ramp/Soak JSONL": _FileCategory(
        "cn616a_rampsoak_log.jsonl", True
    ),
    "Analysis JSONL": _FileCategory(
        "cn616a_analysis_log.jsonl", True
    ),
    "Service configuration JSONL": _FileCategory(
        "cn616a_service_config_log.jsonl", True
    ),
    "Recording JSONL": _FileCategory(
        "cn616a_telemetry_log.jsonl", True, recording_files=True
    ),
}
_MEMORY_CATEGORIES = {
    "Service activity": "Current adapter status and recent command outcomes",
    "GUI exceptions": "Uncaught GUI and GUI-thread exceptions in this session",
    "Recording summary": "Service-authoritative recording status; not controller Run/Idle",
}
_RUN_JSONL_NAMES = (
    "cn616a_telemetry_log.jsonl",
    "cn616a_config_log.jsonl",
    "cn616a_rampsoak_log.jsonl",
    "cn616a_analysis_log.jsonl",
)


@dataclass(frozen=True, slots=True)
class _TailUpdate:
    reset: bool
    entries: tuple[str, ...]
    error: str | None
    notice: str | None


class BoundedLogTail:
    """Incrementally read one source while bounding bytes, row size, and retained rows."""

    def __init__(self) -> None:
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._pending = b""
        self._discarding_line = False
        self._entries: deque[str] = deque(maxlen=MAX_DISPLAY_ROWS)
        self._source: Path | None = None
        self._jsonl = False
        self.last_read_bytes = 0

    @property
    def entries(self) -> tuple[str, ...]:
        return tuple(self._entries)

    def clear(self) -> None:
        self._identity = None
        self._offset = 0
        self._pending = b""
        self._discarding_line = False
        self._entries.clear()
        self._source = None
        self._jsonl = False
        self.last_read_bytes = 0

    def poll(self, path: Path, *, jsonl: bool) -> _TailUpdate:
        path = Path(path)
        if self._source != path or self._jsonl != jsonl:
            self.clear()
            self._source = path
            self._jsonl = jsonl

        try:
            with path.open("rb") as stream:
                source_stat = os.fstat(stream.fileno())
                identity = (
                    int(source_stat.st_dev),
                    int(getattr(source_stat, "st_ino", 0)),
                )
                size = int(source_stat.st_size)
                replaced = self._identity is not None and identity != self._identity
                truncated = self._identity == identity and size < self._offset
                reset = self._identity is None or replaced or truncated
                notice: str | None = None
                if replaced:
                    notice = "Source was rotated or replaced; showing its current tail."
                elif truncated:
                    notice = "Source was truncated; showing its current tail."

                if reset:
                    self._entries.clear()
                    self._pending = b""
                    self._discarding_line = False
                    start = max(0, size - MAX_INITIAL_BYTES)
                    stream.seek(start)
                    raw = stream.read(min(MAX_INITIAL_BYTES, size - start))
                    self.last_read_bytes = len(raw)
                    self._offset = start + len(raw)
                    self._identity = identity
                    if start:
                        raw = self._drop_partial_prefix(raw)
                        notice = (
                            f"Initial tail limited to {MAX_INITIAL_BYTES} bytes; "
                            "an earlier partial row was skipped."
                        )
                    entries = self._consume(raw)
                    return _TailUpdate(True, entries, None, notice)

                available = max(0, size - self._offset)
                skipped = max(0, available - MAX_READ_BYTES)
                read_start = self._offset + skipped
                stream.seek(read_start)
                raw = stream.read(min(MAX_READ_BYTES, max(0, size - read_start)))
                self.last_read_bytes = len(raw)
                self._offset = read_start + len(raw)
                if skipped:
                    self._pending = b""
                    self._discarding_line = False
                    raw = self._drop_partial_prefix(raw)
                    notice = (
                        f"Skipped {skipped} unread bytes to keep each refresh bounded; "
                        "an earlier partial row was skipped."
                    )
                entries = self._consume(raw)
                return _TailUpdate(False, entries, None, notice)
        except OSError as exc:
            self.last_read_bytes = 0
            return _TailUpdate(
                False,
                (),
                f"Unable to read {path}: {type(exc).__name__}: {exc}",
                None,
            )

    def _drop_partial_prefix(self, raw: bytes) -> bytes:
        boundary = raw.find(b"\n")
        if boundary >= 0:
            return raw[boundary + 1 :]
        self._discarding_line = True
        return b""

    def _consume(self, raw: bytes) -> tuple[str, ...]:
        data = self._pending + raw
        self._pending = b""
        emitted: list[str] = []
        pieces = data.split(b"\n")
        for line in pieces[:-1]:
            if self._discarding_line:
                self._discarding_line = False
                continue
            if len(line) > MAX_LINE_BYTES:
                emitted.append(self._oversized_row())
                continue
            rendered = self._render_record(line)
            self._entries.append(rendered)
            emitted.append(rendered)

        remainder = pieces[-1]
        if self._discarding_line:
            if len(remainder) > MAX_LINE_BYTES:
                self._discarding_line = True
            return tuple(emitted[-MAX_DISPLAY_ROWS:])
        if len(remainder) > MAX_LINE_BYTES:
            self._discarding_line = True
            emitted.append(self._oversized_row())
        else:
            self._pending = remainder
        return tuple(emitted[-MAX_DISPLAY_ROWS:])

    def _oversized_row(self) -> str:
        entry = (
            "[timestamp unavailable] [OVERSIZED ROW] "
            f"Record exceeded {MAX_LINE_BYTES} bytes and was discarded."
        )
        self._entries.append(entry)
        return entry

    def _render_record(self, raw: bytes) -> str:
        text = raw.decode("utf-8", errors="replace").replace("\r", "")
        timestamp = "timestamp unavailable"
        content = text
        if self._jsonl:
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                content = f"[MALFORMED JSONL] {text}"
            else:
                if isinstance(record, Mapping):
                    for key in ("ts", "timestamp", "timestamp_pacific", "created_at"):
                        value = record.get(key)
                        if isinstance(value, str) and value:
                            timestamp = value[:80]
                            break
                    content = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                else:
                    content = f"[MALFORMED JSONL RECORD] {text}"
        elif _TIMESTAMP_RE.match(text):
            timestamp = text[:40]
        if len(content) > MAX_RENDERED_CHARS:
            content = content[:MAX_RENDERED_CHARS] + "… [display truncated]"
        return f"{timestamp} | {content}"


def _discover_sources(
    logs_dir: Path,
    category: str,
    recording_dir: str | None,
) -> tuple[tuple[str, ...], str | None]:
    definition = _FILE_CATEGORIES[category]
    if definition.recording_files:
        if not recording_dir:
            return ("No recording directory is currently reported",), None
        root = Path(recording_dir)
        names = _RUN_JSONL_NAMES
        paths = [root / name for name in names if (root / name).is_file()]
        if not paths:
            return (str(root / definition.active_name),), None
        return tuple(str(path) for path in paths), None

    base = Path(logs_dir)
    active = base / definition.active_name
    try:
        entries = list(base.iterdir())
    except OSError as exc:
        return (str(active),), f"Unable to list source directory {base}: {type(exc).__name__}: {exc}"

    if category == "Service diagnostics":
        paths = [
            item
            for item in entries
            if item.name == definition.active_name
            or item.name.startswith(definition.active_name + ".")
        ]
    else:
        stem = definition.active_name.removesuffix(".jsonl")
        paths = [
            item
            for item in entries
            if item.name == definition.active_name
            or (item.name.startswith(stem + "_") and item.name.endswith(".jsonl"))
        ]
    paths.sort(
        key=lambda item: (
            item.name != definition.active_name,
            item.name,
        )
    )
    if not paths:
        paths = [active]
    return tuple(str(item) for item in paths), None


class _ReaderSignals(QObject):
    catalog_changed = Signal(str, object, object)
    tail_updated = Signal(str, str, bool, object, object, object)


class _LogReaderThread(threading.Thread):
    def __init__(self, logs_dir: Path, signals: _ReaderSignals) -> None:
        super().__init__(name="qt-log-reader", daemon=True)
        self._logs_dir = Path(logs_dir)
        self._signals = signals
        self._lock = threading.Lock()
        self._category = "Service activity"
        self._source: str | None = None
        self._recording_dir: str | None = None
        self._stop_event = threading.Event()
        self.thread_ident: int | None = None
        self._tail = BoundedLogTail()
        self._last_catalog: tuple[str, tuple[str, ...], str | None] | None = None
        self._last_update: tuple[str, str, str | None, str | None] | None = None

    def select(self, category: str, source: str | None) -> None:
        with self._lock:
            self._category = category
            self._source = source

    def set_recording_dir(self, recording_dir: str | None) -> None:
        with self._lock:
            self._recording_dir = recording_dir

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        self.thread_ident = threading.get_ident()
        while not self._stop_event.is_set():
            with self._lock:
                category = self._category
                source = self._source
                recording_dir = self._recording_dir
            if category in _FILE_CATEGORIES:
                sources, discovery_error = _discover_sources(
                    self._logs_dir, category, recording_dir
                )
                catalog = (category, sources, discovery_error)
                if catalog != self._last_catalog:
                    self._signals.catalog_changed.emit(category, sources, discovery_error)
                    self._last_catalog = catalog
                if category == "Recording JSONL" and sources == (
                    "No recording directory is currently reported",
                ):
                    if source != sources[0]:
                        self._signals.tail_updated.emit(
                            category,
                            sources[0],
                            True,
                            (),
                            "No recording directory is currently reported.",
                            None,
                        )
                else:
                    selected = source if source in sources else sources[0]
                    definition = _FILE_CATEGORIES[category]
                    update = self._tail.poll(
                        Path(selected),
                        jsonl=definition.jsonl,
                    )
                    signature = (
                        category,
                        selected,
                        update.error,
                        update.notice,
                    )
                    if (
                        update.reset
                        or update.entries
                        or signature != self._last_update
                    ):
                        errors = "\n".join(
                            item
                            for item in (update.error, discovery_error)
                            if item
                        )
                        self._signals.tail_updated.emit(
                            category,
                            selected,
                            update.reset,
                            update.entries,
                            errors or None,
                            update.notice,
                        )
                    self._last_update = signature
            else:
                self._tail.clear()
                catalog = (category, (_MEMORY_CATEGORIES[category],), None)
                if catalog != self._last_catalog:
                    self._signals.catalog_changed.emit(*catalog)
                    self._last_catalog = catalog
            self._stop_event.wait(_POLL_INTERVAL_S)


class GuiExceptionBuffer(QObject):
    records_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._records: deque[str] = deque(maxlen=MAX_GUI_EXCEPTIONS)
        self._lock = threading.Lock()

    def record(self, detail: str) -> None:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        detail = detail.strip() or "Uncaught exception"
        if len(detail) > MAX_GUI_EXCEPTION_CHARS:
            detail = "[earlier traceback text omitted]\n" + detail[-MAX_GUI_EXCEPTION_CHARS:]
        rendered = f"{timestamp} | {detail}"
        with self._lock:
            self._records.append(rendered)
            records = tuple(self._records)
        self.records_changed.emit(records)

    def records(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._records)


GUI_EXCEPTION_BUFFER = GuiExceptionBuffer()
_HOOKS_INSTALLED = False


def install_gui_exception_hooks() -> None:
    """Retain uncaught GUI exceptions while preserving Python's default reporting."""
    global _HOOKS_INSTALLED
    if _HOOKS_INSTALLED:
        return
    previous_sys_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def handle_sys_exception(exc_type, exc_value, exc_tb) -> None:
        GUI_EXCEPTION_BUFFER.record(
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        )
        previous_sys_hook(exc_type, exc_value, exc_tb)

    def handle_thread_exception(args) -> None:
        GUI_EXCEPTION_BUFFER.record(
            "".join(
                traceback.format_exception(
                    args.exc_type,
                    args.exc_value,
                    args.exc_traceback,
                )
            )
        )
        previous_thread_hook(args)

    sys.excepthook = handle_sys_exception
    threading.excepthook = handle_thread_exception
    _HOOKS_INSTALLED = True


class LogsPage(QWidget):
    """Present log tails and session diagnostics without blocking the GUI thread."""

    def __init__(self, logs_dir: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._logs_dir = Path(logs_dir)
        self._adapter_status: AdapterStatus | None = None
        self._outcomes: deque[CommandOutcome] = deque(maxlen=100)
        self._recording_summary: Mapping[str, object] = {"status": None, "error": None}
        self._rendered_category: str | None = None
        self._shutdown = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(8)
        self.provenance_label = QLabel()
        self.provenance_label.setWordWrap(True)
        layout.addWidget(self.provenance_label)
        self.recording_summary_label = QLabel("Recording status is not yet available.")
        self.recording_summary_label.setWordWrap(True)
        layout.addWidget(self.recording_summary_label)
        self.category_combo = QComboBox()
        for category in (
            *_FILE_CATEGORIES.keys(),
            *_MEMORY_CATEGORIES.keys(),
        ):
            self.category_combo.addItem(category, category)
        layout.addWidget(self.category_combo)
        self.source_combo = QComboBox()
        self.source_combo.setObjectName("logSource")
        layout.addWidget(self.source_combo)
        self.source_label = QLabel("Source: not selected")
        self.source_label.setObjectName("logSourcePath")
        self.source_label.setWordWrap(True)
        layout.addWidget(self.source_label)
        self.error_label = QLabel()
        self.error_label.setObjectName("logSourceError")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        self.output = QPlainTextEdit()
        self.output.setObjectName("logOutput")
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(MAX_DISPLAY_ROWS)
        self.output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.output, 1)

        self._signals = _ReaderSignals()
        self._signals.catalog_changed.connect(self._on_catalog_changed)
        self._signals.tail_updated.connect(self._on_tail_updated)
        self._reader = _LogReaderThread(self._logs_dir, self._signals)
        self._reader.start()
        self.category_combo.currentIndexChanged.connect(self._on_category_changed)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        GUI_EXCEPTION_BUFFER.records_changed.connect(self._on_exceptions_changed)
        self.category_combo.setCurrentIndex(
            self.category_combo.findData("Service activity")
        )
        self._on_category_changed(self.category_combo.currentIndex())

    def _on_category_changed(self, index: int) -> None:
        if index < 0:
            return
        category = str(self.category_combo.itemData(index))
        self._rendered_category = category
        self.output.clear()
        self.error_label.clear()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        if category in _MEMORY_CATEGORIES:
            self.source_combo.addItem(_MEMORY_CATEGORIES[category])
            self.source_label.setText(f"Source: in-memory {category.lower()} for this session")
            self.source_combo.setEnabled(False)
            self._reader.select(category, None)
            self._render_memory_category(category)
        else:
            self.source_combo.setEnabled(True)
            fallback = str(self._logs_dir / _FILE_CATEGORIES[category].active_name)
            self.source_combo.addItem(fallback, fallback)
            self.source_combo.blockSignals(False)
            self._reader.select(category, fallback)
            self.source_label.setText(f"Source: {fallback}")
            self.source_combo.blockSignals(True)
        self.source_combo.blockSignals(False)

    def _on_source_changed(self, index: int) -> None:
        category = self._rendered_category
        if index < 0 or category not in _FILE_CATEGORIES:
            return
        source = self.source_combo.itemData(index)
        if not isinstance(source, str):
            return
        self.output.clear()
        self.error_label.clear()
        self.source_label.setText(f"Source: {source}")
        self._reader.select(category, source)

    def _on_catalog_changed(
        self,
        category: str,
        sources: tuple[str, ...],
        discovery_error: str | None,
    ) -> None:
        if category != self._rendered_category:
            return
        self.source_combo.blockSignals(True)
        current = self.source_combo.currentData()
        self.source_combo.clear()
        if sources:
            for source in sources:
                self.source_combo.addItem(source, source)
        selected = self.source_combo.findData(current)
        if selected < 0 and self.source_combo.count():
            selected = 0
        if selected >= 0:
            self.source_combo.setCurrentIndex(selected)
        self.source_combo.blockSignals(False)
        source = self.source_combo.currentData()
        if isinstance(source, str):
            self.source_label.setText(f"Source: {source}")
            self._reader.select(category, source)
        if discovery_error:
            self.error_label.setText(discovery_error)

    def _on_tail_updated(
        self,
        category: str,
        source: str,
        reset: bool,
        entries: tuple[str, ...],
        error: str | None,
        notice: str | None,
    ) -> None:
        if (
            category != self._rendered_category
            or self.source_combo.currentData() != source
        ):
            return
        if reset:
            self.output.clear()
        if entries:
            self.output.appendPlainText("\n".join(entries))
        details = [item for item in (error, notice) if item]
        self.error_label.setText("\n".join(details))
        self.source_label.setText(f"Source: {source}")

    def _render_memory_category(self, category: str) -> None:
        if category == "Service activity":
            lines: list[str] = []
            if self._adapter_status is None:
                lines.append("timestamp unavailable | Disconnected; no service or hardware access attempted.")
            else:
                lines.append(
                    f"timestamp unavailable | Adapter status: {self._adapter_status.message}; "
                    f"source: {self._adapter_status.provenance}"
                )
            for outcome in self._outcomes:
                timestamp = (
                    outcome.created_at.isoformat()
                    if outcome.created_at is not None
                    else "timestamp unavailable"
                )
                target = (
                    f" target={outcome.target.bus_id}/{outcome.target.controller_id}/"
                    f"zone-{outcome.target.zone_id}"
                    if outcome.target is not None
                    else ""
                )
                lines.append(
                    f"{timestamp} | {outcome.operation}: {outcome.result}{target} — "
                    f"{outcome.message}"
                )
            self.output.setPlainText("\n".join(lines))
        elif category == "GUI exceptions":
            self._on_exceptions_changed(GUI_EXCEPTION_BUFFER.records())
        elif category == "Recording summary":
            summary = self._recording_summary
            status = summary.get("status")
            error = summary.get("error")
            lines = []
            if not isinstance(status, Mapping):
                lines.append("Recording status unknown.")
            else:
                active = status.get("run_active") is True
                lines.append(f"Recording {'ACTIVE' if active else 'INACTIVE'} (service status).")
                for key, label in (
                    ("run_name", "Name"),
                    ("run_dir", "Directory"),
                    ("run_started_ts", "Started"),
                    ("run_last_data_ts", "Last data"),
                    ("run_schedule_stop_at", "Scheduled stop"),
                    ("run_recovered", "Recovered"),
                ):
                    value = status.get(key)
                    if value is not None:
                        lines.append(f"{label}: {value}")
            if isinstance(error, str) and error:
                lines.append(f"Status error: {error}")
            if summary.get("simulated") is True:
                lines.insert(0, "SIMULATED ONLY — temporary offline replay status.")
            self.output.setPlainText("\n".join(lines))

    def _on_exceptions_changed(self, records: object) -> None:
        if self._rendered_category != "GUI exceptions":
            return
        if isinstance(records, (tuple, list)):
            self.output.setPlainText("\n".join(str(record) for record in records))
        if self.output.toPlainText().strip():
            self.error_label.setText(
                f"{len(records)} uncaught GUI exception(s) captured; original exception "
                "reporting remains enabled."
            )
        else:
            self.error_label.setText("No uncaught GUI exceptions captured in this session.")

    def set_adapter_status(self, status: AdapterStatus | None) -> None:
        self._adapter_status = status
        if status is None:
            self.provenance_label.setText(
                f"Disconnected; no endpoint probed. Log directory: {self._logs_dir}"
            )
        elif status.mode == "offline-replay":
            self.provenance_label.setText(
                f"OFFLINE REPLAY / NO HARDWARE — temporary synthetic sources: {self._logs_dir}"
            )
        else:
            self.provenance_label.setText(
                f"{status.mode.upper()} — {status.provenance}; log directory: {self._logs_dir}"
            )
        if self._rendered_category == "Service activity":
            self._render_memory_category("Service activity")

    def add_command_outcome(self, outcome: CommandOutcome) -> None:
        self._outcomes.append(outcome)
        if self._rendered_category == "Service activity":
            self._render_memory_category("Service activity")

    def set_recording_summary(self, summary: Mapping[str, object]) -> None:
        self._recording_summary = dict(summary)
        status = summary.get("status")
        error = summary.get("error")
        if isinstance(status, Mapping):
            state = "ACTIVE" if status.get("run_active") is True else "INACTIVE"
            name = status.get("run_name") or status.get("run_dir") or "unnamed"
            directory = status.get("run_dir") or "directory unavailable"
            last_data = status.get("run_last_data_ts") or "unknown"
            self.recording_summary_label.setText(
                f"Recording {state} — {name}; {directory}; last data: {last_data}."
            )
        else:
            self.recording_summary_label.setText(
                str(error) if isinstance(error, str) and error else "Recording status is unknown."
            )
        if summary.get("simulated") is True:
            self.recording_summary_label.setText(
                "SIMULATED ONLY — " + self.recording_summary_label.text()
            )
        recording_dir = status.get("run_dir") if isinstance(status, Mapping) else None
        if isinstance(recording_dir, str) and recording_dir:
            self._reader.set_recording_dir(recording_dir)
        if self._rendered_category == "Recording summary":
            self._render_memory_category("Recording summary")

    def show_service_activity(self) -> None:
        index = self.category_combo.findData("Service activity")
        if index >= 0:
            self.category_combo.setCurrentIndex(index)

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self._reader.stop()
        self._reader.join(timeout=1.0)
        GUI_EXCEPTION_BUFFER.records_changed.disconnect(self._on_exceptions_changed)
