"""Qt configuration readbacks and service-backed settings."""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from py.gui.config_values import parse_retention_seconds
from py.gui.retention_estimator import RetentionEstimateWorker

from .adapter import AdapterSnapshot, AdapterStatus, CommandOutcome, SubmissionStatus


LOGGER = logging.getLogger("cn616a.qt_gui.configuration")
_ZONE_NAMES = {str(zone_id): f"Zone {zone_id}" for zone_id in range(1, 7)}
_COLOR_OPTIONS = ("black", "blue", "red", "green", "orange", "purple", "gray")
_POLLING_DEFAULTS = {
    "telemetry_hz": 2.0,
    "config_hz": 0.2,
    "rampsoak_hz": 0.0,
    "analysis_hz": 1.0,
    "gui_refresh_hz": 2.0,
    "equilibrium_window_s": 30.0,
    "equilibrium_threshold_c": 0.25,
}
_LOG_DEFAULTS = {
    "max_service_config_log_bytes": 2_000_000,
    "max_service_config_log_files": 10,
    "max_telemetry_log_bytes": 10_000_000,
    "max_config_log_bytes": 5_000_000,
    "max_config_log_files": 10,
    "max_rampsoak_log_bytes": 5_000_000,
    "max_rampsoak_log_files": 10,
    "max_analysis_log_bytes": 10_000_000,
}


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _format_value(value: object) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, Mapping):
        return ", ".join(f"{key}: {_format_value(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return ", ".join(_format_value(item) for item in value) or "None"
    return str(value)


def _flatten_values(values: Mapping[str, object], prefix: str = "") -> list[tuple[str, object]]:
    rows: list[tuple[str, object]] = []
    for key in sorted(values, key=str):
        label = f"{prefix}.{key}" if prefix else str(key)
        value = values[key]
        if isinstance(value, Mapping):
            rows.extend(_flatten_values(value, label))
        else:
            rows.append((label, value))
    return rows


def _zone_id(value: object) -> int | None:
    text = str(value).split(":", 1)[0].strip().lower()
    if text.startswith("z"):
        text = text[1:]
    try:
        zone_id = int(text)
    except ValueError:
        return None
    return zone_id if 1 <= zone_id <= 6 else None


def _zone_map(value: object) -> Mapping[str, object]:
    mapping = _mapping(value)
    return {
        str(zone_id): item
        for key, item in mapping.items()
        if (zone_id := _zone_id(key)) is not None
    }


def _viewer_settings(config: Mapping[str, object]) -> dict[str, object]:
    nested = _mapping(config.get("viewer"))
    aliases = {
        "history_hours": "viewer_history_hours",
        "line_width": "viewer_line_width",
        "pv_color": "viewer_pv_color",
        "sp_color": "viewer_sp_color",
        "sp_autotune_color": "viewer_sp_autotune_color",
        "show_sp_abs": "viewer_show_sp_abs",
        "show_sp_autotune": "viewer_show_sp_autotune",
        "show_mae": "viewer_show_mae",
    }
    defaults: dict[str, object] = {
        "history_hours": 1.0,
        "line_width": 2.5,
        "pv_color": "blue",
        "sp_color": "red",
        "sp_autotune_color": "purple",
        "show_sp_abs": True,
        "show_sp_autotune": True,
        "show_mae": True,
    }
    return {
        key: nested.get(key, config.get(alias, defaults[key]))
        for key, alias in aliases.items()
    }


class ConfigurationPage(QWidget):
    """Display configuration snapshots and apply v1 service configuration patches."""

    status_refresh_requested = Signal()

    def __init__(
        self,
        *,
        logs_dir: Path,
        command_submitter: Callable[
            [tuple[Mapping[str, object], ...]], SubmissionStatus
        ]
        | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.logs_dir = Path(logs_dir)
        self._command_submitter = command_submitter
        self._snapshots: dict[str, AdapterSnapshot] = {}
        self._service_config: dict[str, object] = {}
        self._status: AdapterStatus | None = None
        self._service_writes_allowed = False
        self._form_updating = False
        self._form_dirty = False
        self._edit_revision = 0
        self._pending_action: str | None = None
        self._pending_operations: list[str] = []
        self._pending_revision = 0
        self._action_results: list[CommandOutcome] = []
        self._retention_revision = 0
        self._retention_specs: dict[str, tuple[str, float | None, float | None, int | None, int]] = {}
        self._retention_estimates: dict[str, object] = {}
        self._retention_worker = RetentionEstimateWorker(self.logs_dir)
        self._retention_timer = QTimer(self)
        self._retention_timer.setInterval(200)
        self._retention_timer.timeout.connect(self._poll_retention_estimates)
        self._retention_timer.start()

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)
        title = QLabel("Configuration and readbacks")
        title.setObjectName("overviewTitle")
        root.addWidget(title)
        self.status_label = QLabel("Waiting for configuration snapshots.")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self._build_readbacks_tab()
        self._build_settings_tab()
        self._set_action_states()

    @property
    def form_dirty(self) -> bool:
        return self._form_dirty

    def _build_readbacks_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        action_row = QHBoxLayout()
        self.active_endpoint_label = QLabel("Actual service endpoint: not selected")
        self.active_endpoint_label.setWordWrap(True)
        action_row.addWidget(self.active_endpoint_label, 1)
        self.refresh_readbacks_button = QPushButton("Refresh controller readbacks")
        self.refresh_readbacks_button.clicked.connect(self._request_readback_refresh)
        action_row.addWidget(self.refresh_readbacks_button)
        layout.addLayout(action_row)
        self.readback_note = QLabel(
            "Zone monitor selection controls polling scope; it does not enable or disable instrument hardware."
        )
        self.readback_note.setWordWrap(True)
        layout.addWidget(self.readback_note)

        self.readback_tabs = QTabWidget()
        layout.addWidget(self.readback_tabs, 1)
        self.system_table = self._new_readback_table()
        self.readback_tabs.addTab(self.system_table, "System")
        self.zone_tabs = QTabWidget()
        self.readback_tabs.addTab(self.zone_tabs, "Zones")
        self.tabs.addTab(page, "Readbacks")

    @staticmethod
    def _new_readback_table() -> QTableWidget:
        table = QTableWidget(0, 2)
        table.setHorizontalHeaderLabels(("Field", "Readback"))
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        return table

    def _build_settings_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        self.stored_vs_active_label = QLabel(
            "Stored TCP settings are not the active GUI endpoint. Applying or changing them will not switch this client."
        )
        self.stored_vs_active_label.setWordWrap(True)
        layout.addWidget(self.stored_vs_active_label)
        action_row = QHBoxLayout()
        self.apply_button = QPushButton("Apply settings")
        self.apply_button.setToolTip("Store service polling, logging, zone, viewer, and connection settings.")
        self.apply_button.clicked.connect(self._apply_settings)
        action_row.addWidget(self.apply_button)
        self.connect_button = QPushButton("Connect")
        self.connect_button.setToolTip("Store connection settings, then request a controller connection.")
        self.connect_button.clicked.connect(self._connect_serial)
        action_row.addWidget(self.connect_button)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.setToolTip("Request controller disconnection through the selected service.")
        self.disconnect_button.clicked.connect(self._disconnect_serial)
        action_row.addWidget(self.disconnect_button)
        self.refresh_connection_button = QPushButton("Refresh connection")
        self.refresh_connection_button.setToolTip("Store connection settings, then request service reconnection.")
        self.refresh_connection_button.clicked.connect(self._refresh_connection)
        action_row.addWidget(self.refresh_connection_button)
        layout.addLayout(action_row)

        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        content = QWidget()
        self.settings_layout = QVBoxLayout(content)
        self.settings_layout.setContentsMargins(2, 2, 2, 2)
        self.settings_layout.setSpacing(8)
        self._line_edits: dict[str, QLineEdit] = {}
        self._combos: dict[str, QComboBox] = {}
        self._checks: dict[str, QCheckBox] = {}
        self._build_connection_form()
        self._build_polling_form()
        self._build_log_form()
        self._build_zones_form()
        self._build_viewer_form()
        self.settings_layout.addStretch(1)
        self.settings_scroll.setWidget(content)
        layout.addWidget(self.settings_scroll, 1)
        self.retention_estimate_label = QLabel("Retention estimate is being prepared.")
        self.retention_estimate_label.setWordWrap(True)
        layout.addWidget(self.retention_estimate_label)
        self.tabs.addTab(page, "Service settings")

    def _new_group(self, title: str) -> QFormLayout:
        group = QGroupBox(title)
        form = QFormLayout(group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.settings_layout.addWidget(group)
        return form

    def _add_line(
        self,
        form: QFormLayout,
        key: str,
        label: str,
        default: object,
    ) -> QLineEdit:
        field = QLineEdit(str(default))
        field.setObjectName(f"configuration_{key}")
        field.textEdited.connect(self._on_form_edited)
        self._line_edits[key] = field
        form.addRow(label, field)
        return field

    def _add_combo(
        self,
        form: QFormLayout,
        key: str,
        label: str,
        values: Sequence[str],
        default: str,
    ) -> QComboBox:
        combo = QComboBox()
        combo.setObjectName(f"configuration_{key}")
        combo.addItems(values)
        combo.setCurrentText(default)
        combo.currentTextChanged.connect(self._on_form_edited)
        self._combos[key] = combo
        form.addRow(label, combo)
        return combo

    def _add_check(self, form: QFormLayout, key: str, label: str, default: bool) -> QCheckBox:
        checkbox = QCheckBox(label)
        checkbox.setObjectName(f"configuration_{key}")
        checkbox.setChecked(default)
        checkbox.toggled.connect(self._on_form_edited)
        self._checks[key] = checkbox
        form.addRow(checkbox)
        return checkbox

    def _build_connection_form(self) -> None:
        form = self._new_group("Connection settings (stored on the service)")
        for key, label, default in (
            ("last_serial_port", "Serial port", ""),
            ("serial_baudrate", "Baud rate", 115200),
        ):
            self._add_line(form, key, label, default)
        self._add_combo(form, "serial_parity", "Parity", ("N", "E", "O"), "N")
        self._add_combo(form, "serial_stopbits", "Stop bits", ("1", "2"), "1")
        self._add_combo(form, "serial_bytesize", "Byte size", ("7", "8"), "8")
        self._add_line(form, "serial_timeout", "Serial timeout (s)", 1.0)
        self._add_line(form, "last_tcp_host", "Stored service host", "127.0.0.1")
        self._add_line(form, "last_tcp_port", "Stored service TCP port", 8765)

    def _build_polling_form(self) -> None:
        form = self._new_group("Polling and equilibrium analysis")
        labels = {
            "telemetry_hz": "Telemetry (Hz)",
            "config_hz": "Configuration (Hz)",
            "rampsoak_hz": "Ramp/Soak (Hz)",
            "analysis_hz": "Analysis (Hz)",
            "gui_refresh_hz": "GUI refresh (Hz)",
            "equilibrium_window_s": "Equilibrium window (s)",
            "equilibrium_threshold_c": "Equilibrium threshold (°C)",
        }
        for key, default in _POLLING_DEFAULTS.items():
            self._add_line(form, key, labels[key], default)
        self._add_check(form, "flush_each_line", "Flush each log line", True)

    def _build_log_form(self) -> None:
        form = self._new_group("Log rotation and retention")
        for key, default in _LOG_DEFAULTS.items():
            self._add_line(form, key, key.replace("_", " ").removesuffix(" log bytes").title(), default)
        self._add_line(form, "telemetry_retention", "Telemetry retention duration", "24 h")
        self._add_line(form, "analysis_retention", "Analysis retention duration", "24 h")

    def _build_zones_form(self) -> None:
        form = self._new_group("Zones and monitor list")
        self._add_combo(form, "zones_mode", "Monitor selection", ("auto", "list"), "auto")
        self._add_line(form, "zones_list", "Zones (comma-separated, 1–6)", "1,2,3,4,5,6")
        for zone_id in range(1, 7):
            self._add_line(form, f"zone_name_{zone_id}", f"Zone {zone_id} name", f"Zone {zone_id}")

    def _build_viewer_form(self) -> None:
        form = self._new_group("Chart and viewer preferences")
        self._add_line(form, "viewer_history_seconds", "Chart history window (seconds)", 3600)
        self._add_line(form, "viewer_line_width", "Chart line width", 2.5)
        for key, label, default in (
            ("viewer_pv_color", "PV color", "blue"),
            ("viewer_sp_color", "SP color", "red"),
            ("viewer_sp_autotune_color", "Autotune SP color", "purple"),
        ):
            self._add_combo(form, key, label, _COLOR_OPTIONS, default)
        self._add_check(form, "viewer_show_sp_abs", "Show absolute SP", True)
        self._add_check(form, "viewer_show_sp_autotune", "Show autotune SP", True)
        self._add_check(form, "viewer_show_mae", "Show MAE", True)

    def set_adapter_status(
        self,
        status: AdapterStatus | None,
        *,
        service_writes_allowed: bool = False,
    ) -> None:
        self._status = status
        self._service_writes_allowed = service_writes_allowed
        if status is None:
            self.active_endpoint_label.setText("Actual service endpoint: not selected")
        else:
            self.active_endpoint_label.setText(
                f"Actual service endpoint used by this GUI: {status.endpoint or 'none'}\n"
                f"Connection: {status.message}"
            )
        self._set_action_states()

    def show_message(self, message: str) -> None:
        self.status_label.setText(message)

    def set_snapshot(self, snapshot: AdapterSnapshot) -> None:
        self._snapshots[snapshot.category] = snapshot
        if snapshot.category == "service_config" and snapshot.data is not None:
            root = _mapping(snapshot.data)
            raw = _mapping(root.get("config", root))
            if raw:
                self._service_config = dict(raw)
                self._show_service_config(raw)
                if not self._form_dirty:
                    self._populate_form(raw)
        elif snapshot.category == "config":
            self._render_readbacks()
        elif snapshot.category == "telemetry":
            self._render_readbacks()
        elif snapshot.category == "service_config":
            self._show_service_config({})
        self._render_readbacks()

    def _show_service_config(self, config: Mapping[str, object]) -> None:
        endpoint = self._status.endpoint if self._status is not None else "not selected"
        host = str(config.get("last_tcp_host", "") or "(unset)")
        port = config.get("last_tcp_port", 0)
        self.stored_vs_active_label.setText(
            f"Stored service endpoint: {host}:{port} · actual GUI endpoint: {endpoint}. "
            "Changing stored values never switches this GUI client or proves the service listener changed."
        )

    def _populate_form(self, config: Mapping[str, object]) -> None:
        self._form_updating = True
        try:
            serial = _mapping(config.get("last_serial_params"))
            values: dict[str, object] = {
                **_POLLING_DEFAULTS,
                **_LOG_DEFAULTS,
                "last_serial_port": config.get("last_serial_port", ""),
                "serial_baudrate": serial.get("baudrate", 115200),
                "serial_timeout": serial.get("timeout", 1.0),
                "last_tcp_host": config.get("last_tcp_host", "127.0.0.1") or "127.0.0.1",
                "last_tcp_port": config.get("last_tcp_port", 8765) or 8765,
                "flush_each_line": config.get("flush_each_line", True),
                "telemetry_retention": self._format_duration(
                    config.get("telemetry_retention_s", 86_400.0)
                ),
                "analysis_retention": self._format_duration(
                    config.get("analysis_retention_s", 86_400.0)
                ),
                "zones_mode": "list" if str(config.get("zones_mode", "auto")) == "list" else "auto",
                "zones_list": self._format_zones(config.get("zones_list", range(1, 7))),
            }
            for key, default in _LOG_DEFAULTS.items():
                values[key] = config.get(key, default)
            for key, default in _POLLING_DEFAULTS.items():
                alias = {
                    "telemetry_hz": "telemetry_poll_hz",
                    "config_hz": "config_poll_hz",
                    "rampsoak_hz": "rampsoak_poll_hz",
                    "analysis_hz": "analysis_poll_hz",
                }.get(key)
                values[key] = config.get(key, config.get(alias, default) if alias else default)
            names = _mapping(config.get("zone_names"))
            for zone_id in range(1, 7):
                values[f"zone_name_{zone_id}"] = names.get(
                    str(zone_id), names.get(zone_id, f"Zone {zone_id}")
                )
            viewer = _viewer_settings(config)
            values.update(
                {
                    "viewer_history_seconds": self._number(viewer["history_hours"], 1.0) * 3600,
                    "viewer_line_width": viewer["line_width"],
                    "viewer_pv_color": viewer["pv_color"],
                    "viewer_sp_color": viewer["sp_color"],
                    "viewer_sp_autotune_color": viewer["sp_autotune_color"],
                    "viewer_show_sp_abs": viewer["show_sp_abs"],
                    "viewer_show_sp_autotune": viewer["show_sp_autotune"],
                    "viewer_show_mae": viewer["show_mae"],
                }
            )
            values.update(
                {
                    "serial_parity": serial.get("parity", "N"),
                    "serial_stopbits": str(serial.get("stopbits", 1)),
                    "serial_bytesize": str(serial.get("bytesize", 8)),
                }
            )
            for key, field in self._line_edits.items():
                field.setText(str(values.get(key, field.text())))
            for key, combo in self._combos.items():
                value = str(values.get(key, combo.currentText()))
                index = combo.findText(value)
                if index >= 0:
                    combo.setCurrentIndex(index)
            for key, checkbox in self._checks.items():
                checkbox.setChecked(bool(values.get(key, checkbox.isChecked())))
            self._form_dirty = False
            self._request_retention_estimate()
        finally:
            self._form_updating = False

    @staticmethod
    def _format_zones(value: object) -> str:
        if isinstance(value, (list, tuple)):
            return ",".join(str(item) for item in value)
        return "1,2,3,4,5,6"

    @staticmethod
    def _number(value: object, default: float) -> float:
        if isinstance(value, bool):
            return default
        try:
            result = float(value)
        except (TypeError, ValueError):
            return default
        return result if math.isfinite(result) else default

    @staticmethod
    def _format_duration(seconds: object) -> str:
        try:
            hours = float(seconds) / 3600
        except (TypeError, ValueError):
            hours = 24.0
        return f"{hours:g} h"

    def _on_form_edited(self) -> None:
        if self._form_updating:
            return
        self._form_dirty = True
        self._edit_revision += 1
        self.status_label.setText("Unsaved settings edits; refresh will preserve this draft.")
        self._request_retention_estimate()

    def _read_float(
        self,
        key: str,
        label: str,
        *,
        minimum: float | None = None,
        strict_minimum: bool = False,
        maximum: float | None = None,
    ) -> float:
        text = self._line_edits[key].text().strip()
        try:
            value = float(text)
        except ValueError as exc:
            raise ValueError(f"{label} must be a number.") from exc
        if not math.isfinite(value):
            raise ValueError(f"{label} must be finite.")
        if minimum is not None and (value <= minimum if strict_minimum else value < minimum):
            relation = "greater than" if strict_minimum else "at least"
            raise ValueError(f"{label} must be {relation} {minimum}.")
        if maximum is not None and value > maximum:
            raise ValueError(f"{label} must be at most {maximum}.")
        return value

    def _read_int(self, key: str, label: str, *, minimum: int, maximum: int | None = None) -> int:
        text = self._line_edits[key].text().strip()
        if not re.fullmatch(r"[+-]?\d+", text):
            raise ValueError(f"{label} must be a whole number.")
        value = int(text)
        if value < minimum or (maximum is not None and value > maximum):
            limit = f"{minimum}..{maximum}" if maximum is not None else f"at least {minimum}"
            raise ValueError(f"{label} must be {limit}.")
        return value

    def _build_connection_patch(self) -> dict[str, object]:
        serial_params = {
            "baudrate": self._read_int("serial_baudrate", "Baud rate", minimum=1),
            "parity": self._combos["serial_parity"].currentText(),
            "stopbits": int(self._combos["serial_stopbits"].currentText()),
            "bytesize": int(self._combos["serial_bytesize"].currentText()),
            "timeout": self._read_float("serial_timeout", "Serial timeout", minimum=0, strict_minimum=True),
        }
        host = self._line_edits["last_tcp_host"].text().strip()
        if not host:
            raise ValueError("Stored service host must not be empty.")
        return {
            "last_serial_port": self._line_edits["last_serial_port"].text().strip(),
            "last_serial_params": serial_params,
            "last_tcp_host": host,
            "last_tcp_port": self._read_int(
                "last_tcp_port", "Stored service TCP port", minimum=1, maximum=65535
            ),
        }

    def _build_patch(self) -> dict[str, object]:
        patch: dict[str, object] = {}
        for key, label in (
            ("telemetry_hz", "Telemetry rate"),
            ("config_hz", "Configuration rate"),
            ("rampsoak_hz", "Ramp/Soak rate"),
            ("analysis_hz", "Analysis rate"),
        ):
            patch[key] = self._read_float(key, label, minimum=0)
        patch["gui_refresh_hz"] = self._read_float(
            "gui_refresh_hz", "GUI refresh rate", minimum=0, strict_minimum=True
        )
        patch["equilibrium_window_s"] = self._read_float(
            "equilibrium_window_s", "Equilibrium window", minimum=0, strict_minimum=True
        )
        patch["equilibrium_threshold_c"] = self._read_float(
            "equilibrium_threshold_c", "Equilibrium threshold", minimum=0
        )
        patch["flush_each_line"] = self._checks["flush_each_line"].isChecked()

        for key in _LOG_DEFAULTS:
            patch[key] = self._read_int(key, key.replace("_", " "), minimum=1)
        for key, field_key in (
            ("telemetry_retention_s", "telemetry_retention"),
            ("analysis_retention_s", "analysis_retention"),
        ):
            seconds = parse_retention_seconds(self._line_edits[field_key].text())
            if seconds is None:
                raise ValueError(f"{field_key.replace('_', ' ').title()} must be a positive duration.")
            patch[key] = seconds
        mode = self._combos["zones_mode"].currentText()
        patch["zones_mode"] = mode
        if mode == "list":
            zones_text = self._line_edits["zones_list"].text().strip()
            tokens = [token.strip() for token in zones_text.split(",")]
            if not tokens or any(not re.fullmatch(r"\d+", token) for token in tokens):
                raise ValueError("Zone list must contain comma-separated zone numbers 1 through 6.")
            zones = sorted({int(token) for token in tokens})
            if not zones or any(zone_id < 1 or zone_id > 6 for zone_id in zones):
                raise ValueError("Zone list must contain at least one zone from 1 through 6.")
            patch["zones_list"] = zones
        patch["zone_names"] = {
            str(zone_id): self._line_edits[f"zone_name_{zone_id}"].text().strip()
            or f"Zone {zone_id}"
            for zone_id in range(1, 7)
        }

        history_seconds = self._read_float(
            "viewer_history_seconds", "Chart history window", minimum=0, strict_minimum=True
        )
        viewer: dict[str, object] = {
            "history_hours": history_seconds / 3600,
            "line_width": self._read_float(
                "viewer_line_width", "Chart line width", minimum=0, strict_minimum=True, maximum=10
            ),
            "pv_color": self._combos["viewer_pv_color"].currentText(),
            "sp_color": self._combos["viewer_sp_color"].currentText(),
            "sp_autotune_color": self._combos["viewer_sp_autotune_color"].currentText(),
            "show_sp_abs": self._checks["viewer_show_sp_abs"].isChecked(),
            "show_sp_autotune": self._checks["viewer_show_sp_autotune"].isChecked(),
            "show_mae": self._checks["viewer_show_mae"].isChecked(),
        }
        patch["viewer"] = viewer
        for key, value in viewer.items():
            patch[f"viewer_{key}"] = value
        patch.update(self._build_connection_patch())

        for key, estimate_key in (
            ("max_telemetry_log_files", "telemetry"),
            ("max_analysis_log_files", "analysis"),
        ):
            estimate = _mapping(self._retention_estimates.get(estimate_key))
            if estimate.get("file_count_confident") is True:
                count = estimate.get("file_count")
                if isinstance(count, int) and count >= 1:
                    patch[key] = count
        return patch

    def _apply_settings(self) -> None:
        try:
            patch = self._build_patch()
        except ValueError as exc:
            self.status_label.setText(f"Invalid settings: {exc}")
            return
        self._queue_action(
            "apply",
            ({"op": "set_service_config", "patch": patch},),
            ("set_service_config",),
        )

    def _connect_serial(self) -> None:
        self._connection_action("connect_serial", "Save stored settings and request controller connect")

    def _refresh_connection(self) -> None:
        self._connection_action("refresh_connection", "Save stored settings and request connection refresh")

    def _connection_action(self, operation: str, label: str) -> None:
        try:
            patch = self._build_connection_patch()
        except ValueError as exc:
            self.status_label.setText(f"Invalid connection settings: {exc}")
            return
        messages = (
            {"op": "set_service_config", "patch": patch},
            {"op": operation},
        )
        self._queue_action(operation, messages, ("set_service_config", operation), label)

    def _disconnect_serial(self) -> None:
        self._queue_action(
            "disconnect_serial",
            ({"op": "disconnect_serial"},),
            ("disconnect_serial",),
            "Requesting controller disconnect.",
        )

    def _request_readback_refresh(self) -> None:
        if not self._status or self._status.mode == "offline-replay":
            self.status_label.setText(
                "SIMULATED ONLY — replay readback refresh does not access a controller."
            )
            return
        self._queue_action(
            "read_config",
            ({"op": "read_config"},),
            ("read_config",),
            "Requesting controller configuration readback through the selected service.",
        )

    def _queue_action(
        self,
        action: str,
        messages: tuple[Mapping[str, object], ...],
        operations: tuple[str, ...],
        pending_text: str | None = None,
    ) -> None:
        if self._pending_action is not None:
            return
        if not self._command_submitter:
            self.status_label.setText("No service adapter is configured; no request was sent.")
            return
        is_read = operations == ("read_config",)
        if not is_read and not self._service_writes_allowed:
            self.status_label.setText("Service writes are unavailable or read-only; no request was sent.")
            return
        self._pending_action = action
        self._pending_operations = list(operations)
        self._pending_revision = self._edit_revision
        self._action_results = []
        self.status_label.setText(pending_text or "Applying service settings…")
        self._set_action_states()
        try:
            result = self._command_submitter(messages)
        except (OSError, RuntimeError, ValueError) as exc:
            self._finish_action()
            self.status_label.setText(f"Unable to queue {action}: {exc}")
            return
        if result not in {"submitted", "mocked"} and self._pending_action is not None:
            self._finish_action()
            self.status_label.setText(f"Service request {result}; no successful outcome was confirmed.")

    def set_command_outcome(self, outcome: CommandOutcome) -> None:
        if not self._pending_operations or outcome.operation != self._pending_operations[0]:
            return
        self._pending_operations.pop(0)
        self._action_results.append(outcome)
        is_settings_op = outcome.operation == "set_service_config"
        if is_settings_op and outcome.result in {"confirmed_success", "mock"}:
            response = _mapping(outcome.response)
            service_config = _mapping(response.get("service_config"))
            if service_config:
                self._service_config = dict(service_config)
                self._show_service_config(service_config)
                if self._pending_revision == self._edit_revision:
                    self._populate_form(service_config)
                else:
                    self._form_dirty = True
            elif self._pending_revision == self._edit_revision:
                self._form_dirty = False

        if self._pending_operations:
            self.status_label.setText(
                f"{outcome.operation}: {outcome.result} — {outcome.message}; waiting for next operation."
            )
            return

        results = [item.result for item in self._action_results]
        detail = "; ".join(
            f"{item.operation}: {item.result} ({item.message})" for item in self._action_results
        )
        simulated = (
            self._status is not None and self._status.mode == "offline-replay"
        ) or any(item.result == "mock" for item in self._action_results)
        successful = all(result in {"confirmed_success", "mock"} for result in results)
        prefix = "SIMULATED ONLY — " if simulated else ""
        if self._pending_action == "apply":
            message = (
                "Settings acknowledged; the active serial connection/listener was not inferred."
                if successful
                else f"Settings were not fully confirmed: {detail}"
            )
        elif self._pending_action in {"connect_serial", "disconnect_serial", "refresh_connection"}:
            if self._status is not None and self._status.mode == "offline-replay":
                message = (
                    "Connection operation simulated; no serial port, service endpoint, or listener was contacted."
                    if successful
                    else f"Simulated connection transition did not complete: {detail}"
                )
            else:
                message = (
                    "Service acknowledged the connection operation; refreshing authoritative service status. "
                    "Stored host/port values did not switch this GUI endpoint."
                    if successful
                    else f"Connection transition not fully confirmed: {detail}"
                )
                self.status_refresh_requested.emit()
        else:
            message = (
                "Controller readback request acknowledged; snapshots remain authoritative."
                if successful
                else f"Readback request did not complete: {detail}"
            )
        self._finish_action()
        self.status_label.setText(prefix + message)
        self._set_action_states()

    def _finish_action(self) -> None:
        self._pending_action = None
        self._pending_operations = []
        self._action_results = []
        self._set_action_states()

    def _set_action_states(self) -> None:
        pending = self._pending_action is not None
        writes_allowed = self._service_writes_allowed
        for button in (self.apply_button, self.connect_button, self.disconnect_button, self.refresh_connection_button):
            button.setEnabled(writes_allowed and not pending)
        can_read = bool(self._status and self._status.mode == "service" and self._status.connected)
        self.refresh_readbacks_button.setEnabled(can_read and not pending)

    def _request_retention_estimate(self) -> None:
        def duration(key: str) -> float | None:
            return parse_retention_seconds(self._line_edits[key].text())

        def numeric(key: str) -> float | None:
            try:
                value = float(self._line_edits[key].text())
            except ValueError:
                return None
            return value if math.isfinite(value) and value > 0 else None

        def integer(key: str) -> int | None:
            try:
                value = int(self._line_edits[key].text())
            except ValueError:
                return None
            return value if value > 0 else None

        mode = self._combos["zones_mode"].currentText()
        zones_count = 6
        if mode == "list":
            values = [token.strip() for token in self._line_edits["zones_list"].text().split(",")]
            parsed = {_zone_id(token) for token in values}
            valid = {value for value in parsed if value is not None}
            if valid:
                zones_count = len(valid)
        factor = zones_count / 6
        specs = {
            "telemetry": (
                "cn616a_telemetry_log.jsonl",
                duration("telemetry_retention"),
                numeric("telemetry_hz"),
                integer("max_telemetry_log_bytes"),
                max(800, int(1600 * factor)),
            ),
            "analysis": (
                "cn616a_analysis_log.jsonl",
                duration("analysis_retention"),
                numeric("analysis_hz"),
                integer("max_analysis_log_bytes"),
                max(500, int(1000 * factor)),
            ),
        }
        try:
            self._retention_revision = self._retention_worker.request(specs)
        except RuntimeError:
            LOGGER.exception("Retention estimate requested after worker shutdown")
            return
        if specs != self._retention_specs:
            self._retention_estimates = {}
            self.retention_estimate_label.setText(
                "Estimating retention from bounded log samples; scan runs in the background."
            )
        self._retention_specs = specs

    def _poll_retention_estimates(self) -> None:
        for revision, _, estimates in self._retention_worker.poll_results():
            if revision != self._retention_revision:
                continue
            self._retention_estimates = estimates
            descriptions = []
            for key in ("telemetry", "analysis"):
                estimate = _mapping(estimates.get(key))
                if estimate.get("error"):
                    descriptions.append(f"{key}: unavailable ({estimate['error']})")
                elif estimate.get("estimated_bytes") is None:
                    descriptions.append(f"{key}: enter positive duration, polling rate, and file size")
                else:
                    confidence = "sampled" if estimate.get("file_count_confident") else "uncertain"
                    descriptions.append(
                        f"{key}: {estimate['file_count']} files, "
                        f"~{self._format_bytes(estimate['estimated_bytes'])} "
                        f"({confidence}; {estimate['sampled_records']} records sampled)"
                    )
            self.retention_estimate_label.setText(
                "Estimated retention — " + "; ".join(descriptions)
                + ". Analysis is an upper bound because only changed results are logged."
            )

    @staticmethod
    def _format_bytes(value: object) -> str:
        if not isinstance(value, (int, float)):
            return "N/A"
        size = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    def _render_readbacks(self) -> None:
        config_snapshot = self._snapshots.get("config")
        config_root = _mapping(config_snapshot.data) if config_snapshot is not None else {}
        config_data = _mapping(config_root.get("config", config_root))
        self._fill_table(self.system_table, _flatten_values(_mapping(config_data.get("system"))))

        zones = _zone_map(config_data.get("zones"))
        telemetry = self._snapshots.get("telemetry")
        telemetry_values = telemetry.zone_values if telemetry is not None else {}
        service_cfg = self._service_config
        names = _mapping(service_cfg.get("zone_names"))
        current_zone = _zone_id(self.zone_tabs.tabText(self.zone_tabs.currentIndex())) if self.zone_tabs.count() else None
        while self.zone_tabs.count():
            page = self.zone_tabs.widget(0)
            self.zone_tabs.removeTab(0)
            page.deleteLater()
        for zone_id in sorted(set(_zone_id(key) for key in zones) | set(range(1, 7))):
            if zone_id is None:
                continue
            zone_data = _mapping(zones.get(str(zone_id)))
            rows = _flatten_values(zone_data)
            pid_values = _mapping(telemetry_values.get(zone_id))
            rows.extend(
                (f"PID {label} gain", pid_values.get(field))
                for label, field in (("P", "p_gain"), ("I", "i_gain"), ("D", "d_gain"))
            )
            table = self._new_readback_table()
            self._fill_table(table, rows)
            page = QWidget()
            layout = QVBoxLayout(page)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(table)
            layout.addWidget(scroll)
            zone_name = names.get(str(zone_id), _ZONE_NAMES[str(zone_id)])
            self.zone_tabs.addTab(page, f"{zone_id}: {zone_name}")
        if current_zone is not None:
            for index in range(self.zone_tabs.count()):
                if _zone_id(self.zone_tabs.tabText(index)) == current_zone:
                    self.zone_tabs.setCurrentIndex(index)
                    break

    @staticmethod
    def _fill_table(table: QTableWidget, rows: Sequence[tuple[str, object]]) -> None:
        table.setRowCount(max(1, len(rows)))
        if not rows:
            table.setItem(0, 0, QTableWidgetItem("Readback"))
            table.setItem(0, 1, QTableWidgetItem("No data available"))
            return
        for row, (label, value) in enumerate(rows):
            table.setItem(row, 0, QTableWidgetItem(str(label)))
            table.setItem(row, 1, QTableWidgetItem(_format_value(value)))

    def shutdown(self) -> None:
        self._retention_timer.stop()
        self._retention_worker.shutdown()
