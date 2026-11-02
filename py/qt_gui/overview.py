"""Read-only, keyed overview of the adapter's cached controller snapshots."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QLocale,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QDoubleValidator, QFocusEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from py.gui.state_values import get_field, get_numeric

from .adapter import (
    AdapterHistory,
    AdapterSnapshot,
    AdapterStatus,
    CommandOutcome,
    ControllerIdentity,
    SubmissionStatus,
    ZoneKey,
    snapshot_age_seconds,
)
from .charts import ChartsPanel


ZONE_IDS = tuple(range(1, 7))
ZONE_KEY_ROLE = Qt.ItemDataRole.UserRole.value + 1
ZONE_NUMBER_ROLE = Qt.ItemDataRole.UserRole.value + 2
SORT_ROLE = Qt.ItemDataRole.UserRole.value + 3


class Column(IntEnum):
    ZONE = 0
    PV = 1
    SETPOINT = 2
    OUTPUT = 3
    METHOD_LOOP = 4
    ERROR = 5
    EQUILIBRIUM = 6


HEADERS = ("Zone", "PV (°C)", "SP (°C)", "Output (%)", "Method / loop", "MAE (°C)", "Equil.")
NUMERIC_COLUMNS = {Column.PV, Column.SETPOINT, Column.OUTPUT, Column.ERROR}
COLORS = {Column.PV: "#1264bd", Column.SETPOINT: "#9b5b00", Column.ERROR: "#6841a5"}
FLOAT32_MAX = 3.4028234663852886e38
CONTROL_FEEDBACK_PREFIXES = (
    "APPLYING",
    "SIMULATED",
    "Service acknowledged",
    "Fresh readback",
    "Fresh controller",
    "Readback",
    "No automatic retry",
    "Enter finite",
    "Apply blocked",
    "Request was not queued",
    "set_sp_abs",
    "set_control_method",
    "set_autotune_setpoint",
)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _field(values: Mapping[str, object], *aliases: str) -> object:
    return get_field(dict(values), *aliases)


def _number(values: Mapping[str, object], *aliases: str) -> float | None:
    return get_numeric(dict(values), *aliases)


def _nested(values: Mapping[str, object], *keys: str) -> object:
    current: object = values
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _first(*values: object) -> object:
    return next((value for value in values if value is not None), None)


def _text(value: object) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, str):
        return value.strip().replace("_", " ") or "N/A"
    if isinstance(value, (int, float)) and math.isfinite(value):
        return str(value)
    return "N/A"


def _number_text(value: float | None, decimals: int, unit: str = "") -> str:
    return f"{value:.{decimals}f}{unit}" if value is not None else "N/A"


def _as_float(value: object) -> float | None:
    return value if isinstance(value, float) else None


def _equilibrium(value: object) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return "N/A"


def _names(snapshot: AdapterSnapshot | None) -> dict[int, str]:
    config = _mapping(snapshot.data.get("config")) if snapshot and snapshot.data else {}
    raw = config.get("zone_names")
    result = {}
    for zone_id in ZONE_IDS:
        value: object = None
        if isinstance(raw, Mapping):
            value = raw.get(str(zone_id), raw.get(zone_id))
        elif isinstance(raw, (list, tuple)) and len(raw) >= zone_id:
            value = raw[zone_id - 1]
        name = str(value).strip() if value is not None else ""
        result[zone_id] = name or f"Zone {zone_id}"
    return result


@dataclass(frozen=True, slots=True)
class ZoneReadback:
    zone_id: int
    key: ZoneKey | None
    name: str
    telemetry: Mapping[str, object]
    config: Mapping[str, object]
    analysis: Mapping[str, object]


class DirtyReadbackGuard:
    """Keep later editable fields scoped to their target and safe from refreshes."""

    def __init__(self) -> None:
        self._dirty: set[tuple[ZoneKey, str]] = set()

    def set_dirty(self, target: ZoneKey, field: str, dirty: bool = True) -> None:
        key = (target, field)
        if dirty:
            self._dirty.add(key)
        else:
            self._dirty.discard(key)

    def is_dirty(self, target: ZoneKey, field: str) -> bool:
        return (target, field) in self._dirty

    def apply_readback(self, target: ZoneKey, field: str, apply: Callable[[], None]) -> bool:
        if self.is_dirty(target, field):
            return False
        apply()
        return True


class TelemetryTableModel(QAbstractTableModel):
    """Six stable rows; changes update cached values without resetting the model."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._status: AdapterStatus | None = None
        self._snapshots: dict[str, AdapterSnapshot] = {}
        self._rows = [
            ZoneReadback(zone_id, None, f"Zone {zone_id}", {}, {}, {})
            for zone_id in ZONE_IDS
        ]

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return HEADERS[section] if 0 <= section < len(HEADERS) else None
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        column = Column(index.column())
        values = self._values(row)
        if role == ZONE_KEY_ROLE:
            return row.key
        if role == ZONE_NUMBER_ROLE:
            return row.zone_id
        if role == SORT_ROLE:
            if column == Column.EQUILIBRIUM:
                return int(values[column]) if isinstance(values[column], bool) else None
            return values[column]
        display = self._display(column, values[column])
        if role == Qt.ItemDataRole.DisplayRole:
            return display
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{HEADERS[column]}: {display}"
        if role == Qt.ItemDataRole.ToolTipRole:
            identity = (
                f"{row.key.bus_id}, controller {row.key.controller_id}"
                if row.key is not None
                else "Controller identity unknown"
            )
            return f"Zone {row.zone_id} · {row.name}\n{identity}\n{HEADERS[column]}: {display}"
        if role == Qt.ItemDataRole.TextAlignmentRole and column in NUMERIC_COLUMNS:
            return (Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter).value
        if role == Qt.ItemDataRole.ForegroundRole and column in COLORS and values[column] is not None:
            return QColor(COLORS[column])
        return None

    @staticmethod
    def _values(row: ZoneReadback) -> tuple[object, ...]:
        telemetry = row.telemetry
        analysis = row.analysis
        method = _field(telemetry, "control_method", "mode", "control_mode")
        loop = _field(telemetry, "loop_status", "status")
        method_loop = None if method is None and loop is None else f"{_text(method)} / {_text(loop)}"
        return (
            f"{row.zone_id} / {row.name}",
            _number(telemetry, "pv_c", "pv", "PV", "process_value_c", "process_value"),
            _number(
                telemetry, "sp_abs_c", "sp_abs", "sp_c", "sp", "SP_abs", "SP", "setpoint_c", "setpoint"
            ),
            _number(telemetry, "out_pct", "output_pct", "output", "OUT"),
            method_loop,
            _number(analysis, "avg_abs_error_c", "mae_c", "mae", "mean_abs_error_c"),
            _field(analysis, "in_equilibrium", "equilibrium"),
        )

    @staticmethod
    def _display(column: Column, value: object) -> str:
        if column in NUMERIC_COLUMNS:
            decimals = 1 if column == Column.OUTPUT else 3 if column == Column.ERROR else 2
            return _number_text(_as_float(value), decimals)
        if column == Column.EQUILIBRIUM:
            return _equilibrium(value)
        if column == Column.ZONE and isinstance(value, str):
            return value
        return _text(value)

    def snapshot(self, category: str) -> AdapterSnapshot | None:
        return self._snapshots.get(category)

    @property
    def telemetry_identity(self) -> ControllerIdentity | None:
        snapshot = self.snapshot("telemetry")
        if snapshot is None or snapshot.data is None or self.identity_mismatch("telemetry"):
            return None
        return snapshot.identity

    @property
    def has_telemetry(self) -> bool:
        snapshot = self.snapshot("telemetry")
        return snapshot is not None and snapshot.data is not None and not self.identity_mismatch("telemetry")

    def identity_mismatch(self, category: str) -> bool:
        snapshot = self.snapshot(category)
        if snapshot is None or snapshot.identity is None:
            return False
        target = self._status.target if self._status is not None else None
        telemetry = self.snapshot("telemetry")
        reference = target or (telemetry.identity if telemetry is not None else None)
        return reference is not None and snapshot.identity != reference

    def row_for_zone(self, zone_id: int) -> ZoneReadback:
        return self._rows[zone_id - 1]

    def set_status(self, status: AdapterStatus | None) -> None:
        self._status = status
        self._update_rows()

    def set_snapshot(self, snapshot: AdapterSnapshot) -> None:
        self._snapshots[snapshot.category] = snapshot
        if snapshot.category in {"telemetry", "config", "analysis", "service_config"}:
            self._update_rows()

    def _zone_values(self, category: str, zone_id: int) -> Mapping[str, object]:
        snapshot = self.snapshot(category)
        if snapshot is None or snapshot.data is None or self.identity_mismatch(category):
            return {}
        return snapshot.zone_values.get(zone_id, {})

    def _update_rows(self) -> None:
        names = _names(
            None if self.identity_mismatch("service_config") else self.snapshot("service_config")
        )
        identity = self.telemetry_identity
        for index, zone_id in enumerate(ZONE_IDS):
            key = (
                ZoneKey(identity.key.bus_id, identity.key.controller_id, zone_id)
                if identity is not None
                else None
            )
            updated = ZoneReadback(
                zone_id=zone_id,
                key=key,
                name=names[zone_id],
                telemetry=self._zone_values("telemetry", zone_id),
                config=self._zone_values("config", zone_id),
                analysis=self._zone_values("analysis", zone_id),
            )
            if updated != self._rows[index]:
                self._rows[index] = updated
                self.dataChanged.emit(
                    self.index(index, 0),
                    self.index(index, len(HEADERS) - 1),
                    [
                        Qt.ItemDataRole.DisplayRole,
                        Qt.ItemDataRole.ForegroundRole,
                        Qt.ItemDataRole.ToolTipRole,
                        Qt.ItemDataRole.AccessibleTextRole,
                        ZONE_KEY_ROLE,
                        SORT_ROLE,
                    ],
                )


class ZoneSortProxy(QSortFilterProxyModel):
    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        left_value = left.data(SORT_ROLE)
        right_value = right.data(SORT_ROLE)
        if left_value is None or right_value is None:
            if left_value is not right_value:
                return left_value is not None
        elif left_value != right_value:
            if isinstance(left_value, (int, float)) and isinstance(right_value, (int, float)):
                return left_value < right_value
            return str(left_value).casefold() < str(right_value).casefold()
        return left.data(ZONE_NUMBER_ROLE) < right.data(ZONE_NUMBER_ROLE)


def _card(title: str, tone: str, content: QWidget) -> QFrame:
    frame = QFrame()
    frame.setObjectName("overviewCard")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    heading = QLabel(title)
    heading.setObjectName("cardTitle")
    heading.setProperty("tone", tone)
    layout.addWidget(heading)
    layout.addWidget(content)
    return frame


def _label(text: str = "", *, name: str = "") -> QLabel:
    label = QLabel(text)
    if name:
        label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _age_text(age: float | None) -> str:
    if age is None:
        return "age unavailable"
    seconds = int(age)
    if seconds < 60:
        return f"{seconds} s old"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60:02d} s old"
    return f"{seconds // 3600} h {(seconds % 3600) // 60:02d} min old"


class SelectAllLineEdit(QLineEdit):
    def focusInEvent(self, event: QFocusEvent) -> None:
        super().focusInEvent(event)
        self.selectAll()


class OverviewPage(QWidget):
    """Overview telemetry, charts, and target-bound zone update controls."""

    zone_selection_changed = Signal(object)

    def __init__(
        self,
        *,
        stale_after_s: float = 5.0,
        history_requester: Callable[[float], int] | None = None,
        command_submitter: Callable[
            [tuple[Mapping[str, object], ...], ZoneKey], SubmissionStatus
        ]
        | None = None,
        mock_scenario_setter: Callable[[str], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not math.isfinite(stale_after_s) or stale_after_s <= 0:
            raise ValueError("stale_after_s must be positive and finite")
        self.stale_after_s = stale_after_s
        self._history_requester = history_requester
        self._command_submitter = command_submitter
        self._mock_scenario_setter = mock_scenario_setter
        self.dirty_readbacks = DirtyReadbackGuard()
        self._control_drafts: dict[ZoneKey, tuple[str, str, str]] = {}
        self._pending_target: ZoneKey | None = None
        self._pending_outcomes: list[CommandOutcome] = []
        self._pending_messages: tuple[Mapping[str, object], ...] = ()
        self._awaiting_readback: tuple[
            ZoneKey, tuple[float, str, float], datetime
        ] | None = None
        self._controls_target: ZoneKey | None = None
        self._updating_controls = False
        self._control_status_text = "Select a verified zone to edit its command values."
        self.model = TelemetryTableModel(self)
        self.proxy = ZoneSortProxy(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SORT_ROLE)
        self.proxy.setDynamicSortFilter(True)
        self._status: AdapterStatus | None = None
        self._selected_identity: ControllerIdentity | None = None
        self._require_selection = False
        self._build_ui()
        if self._mock_scenario_setter is not None:
            self._mock_scenario_setter("success")
        self._age_timer = QTimer(self)
        self._age_timer.setInterval(1000)
        self._age_timer.timeout.connect(self.refresh_age)
        self._age_timer.start()
        self.refresh_age()
        self._update_detail()
        self._sync_zone_controls()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 10, 0, 0)
        outer.setSpacing(10)

        title_row = QHBoxLayout()
        title = _label("Overview", name="overviewTitle")
        title_row.addWidget(title)
        subtitle = _label("Telemetry / select a zone for readbacks", name="overviewSubtitle")
        title_row.addWidget(subtitle)
        title_row.addStretch(1)
        title_row.addWidget(_label("TARGET-BOUND · explicit Apply", name="viewOnly"))
        outer.addLayout(title_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        workspace = QWidget()
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(10)

        table_content = QWidget()
        table_layout = QVBoxLayout(table_content)
        table_layout.setContentsMargins(8, 8, 8, 8)
        table_layout.setSpacing(5)
        self.zone_table = QTableView()
        self.zone_table.setObjectName("zoneTable")
        self.zone_table.setModel(self.proxy)
        self.zone_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.zone_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.zone_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.zone_table.setAlternatingRowColors(True)
        self.zone_table.verticalHeader().hide()
        self.zone_table.verticalHeader().setDefaultSectionSize(34)
        self.zone_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.zone_table.horizontalHeader().setSectionResizeMode(Column.ZONE, QHeaderView.ResizeMode.Stretch)
        self.zone_table.setMinimumHeight(270)
        self.zone_table.setMaximumHeight(315)
        self.zone_table.setSortingEnabled(True)
        self.zone_table.sortByColumn(Column.ZONE, Qt.SortOrder.AscendingOrder)
        self.zone_table.selectionModel().currentRowChanged.connect(self._selection_changed)
        table_layout.addWidget(self.zone_table)
        table_layout.addWidget(
            _label(
                "PV / measured     SP / target     MAE / analysis     N/A / unavailable",
                name="visualKey",
            )
        )
        workspace_layout.addWidget(_card("Zone overview  /  select a row to focus", "navy", table_content))

        self.chart_panel = ChartsPanel(
            self._history_requester,
            parent=self,
        )
        workspace_layout.addWidget(_card("Focused zone  /  trend", "teal", self.chart_panel))
        workspace_layout.addStretch(1)

        rail_content = QWidget()
        rail_layout = QVBoxLayout(rail_content)
        rail_layout.setContentsMargins(0, 0, 0, 0)
        rail_layout.setSpacing(10)
        health_content = QWidget()
        health_layout = QVBoxLayout(health_content)
        health_layout.setContentsMargins(10, 10, 10, 10)
        health_layout.setSpacing(6)
        self.health_label = _label(name="healthSummary")
        self.identity_label = _label(name="sourceIdentity")
        self.source_label = _label(name="sourceDetails")
        health_layout.addWidget(self.health_label)
        health_layout.addWidget(self.identity_label)
        health_layout.addWidget(self.source_label)
        rail_layout.addWidget(_card("Source & service health", "teal", health_content))

        detail_content = QWidget()
        detail_layout = QVBoxLayout(detail_content)
        detail_layout.setContentsMargins(10, 10, 10, 10)
        detail_layout.setSpacing(7)
        self.focus_label = _label(name="focusIdentity")
        self.measurement_label = _label(name="focusMeasurements")
        detail_layout.addWidget(self.focus_label)
        detail_layout.addWidget(self.measurement_label)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)
        self.detail_labels: dict[str, QLabel] = {}
        for key, title in (
            ("sensor", "Sensor"),
            ("sensor_status", "Sensor status"),
            ("alarms", "Alarm 1 / 2"),
            ("control", "Control / loop"),
            ("autotune", "Autotune"),
            ("pid", "PID P / I / D"),
            ("timing", "Deadband / cycle"),
            ("segment", "Profile segment"),
            ("analysis", "Analysis"),
        ):
            field_name = _label(title, name="fieldLabel")
            value = _label("N/A", name=f"detail_{key}")
            self.detail_labels[key] = value
            form.addRow(field_name, value)
        detail_layout.addLayout(form)
        rail_layout.addWidget(_card("Focused zone  /  readbacks", "blue", detail_content))
        rail_layout.addWidget(self._build_zone_controls())
        rail_layout.addStretch(1)
        rail = QScrollArea()
        rail.setObjectName("overviewRail")
        rail.setWidgetResizable(True)
        rail.setMinimumWidth(280)
        rail.setWidget(rail_content)
        workspace_scroll = QScrollArea()
        workspace_scroll.setObjectName("overviewWorkspace")
        workspace_scroll.setWidgetResizable(True)
        workspace_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        workspace_scroll.setMinimumWidth(320)
        workspace_scroll.setWidget(workspace)
        splitter.addWidget(workspace_scroll)
        splitter.addWidget(rail)
        splitter.setSizes([760, 330])
        outer.addWidget(splitter, 1)

    def _build_zone_controls(self) -> QFrame:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(7)
        self.control_target_label = _label("No verified target selected", name="focusIdentity")
        layout.addWidget(self.control_target_label)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)
        self.sp_abs_entry = SelectAllLineEdit()
        self.sp_abs_entry.setObjectName("absoluteSetpoint")
        sp_validator = QDoubleValidator(-FLOAT32_MAX, FLOAT32_MAX, 100, self)
        sp_validator.setLocale(QLocale.c())
        sp_validator.setNotation(QDoubleValidator.Notation.ScientificNotation)
        self.sp_abs_entry.setValidator(sp_validator)
        self.sp_abs_entry.validator().setNotation(QDoubleValidator.Notation.StandardNotation)
        self.method_combo = QComboBox()
        self.method_combo.setObjectName("controlMethod")
        self.method_combo.addItem("PID", "PID_CONTROL")
        self.method_combo.addItem("ON/OFF", "ON_OFF_CONTROL")
        self.autotune_sp_entry = SelectAllLineEdit()
        self.autotune_sp_entry.setObjectName("autotuneSetpoint")
        autotune_validator = QDoubleValidator(-FLOAT32_MAX, FLOAT32_MAX, 100, self)
        autotune_validator.setLocale(QLocale.c())
        autotune_validator.setNotation(QDoubleValidator.Notation.ScientificNotation)
        self.autotune_sp_entry.setValidator(autotune_validator)
        form.addRow(_label("Absolute SP (°C)", name="fieldLabel"), self.sp_abs_entry)
        method_label = _label("Control method", name="fieldLabel")
        method_label.setToolTip("Distinct from the Ramp/Soak control mode.")
        self.method_combo.setToolTip("This selects control_method, not the profile control_mode.")
        form.addRow(method_label, self.method_combo)
        form.addRow(_label("Autotune SP (°C)", name="fieldLabel"), self.autotune_sp_entry)
        layout.addLayout(form)

        action_row = QHBoxLayout()
        self.apply_controls_button = QPushButton("Apply to selected zone")
        self.apply_controls_button.setObjectName("applyZoneUpdates")
        self.discard_controls_button = QPushButton("Discard edits")
        action_row.addWidget(self.apply_controls_button)
        action_row.addWidget(self.discard_controls_button)
        layout.addLayout(action_row)

        self.mock_scenario_label = _label("SIMULATED OUTCOME (no service/hardware request)", name="readOnlyNote")
        self.mock_scenario_combo = QComboBox()
        self.mock_scenario_combo.setObjectName("mockOutcomeScenario")
        for label, value in (
            ("Success", "success"),
            ("Rejected", "rejection"),
            ("Partial sequence failure", "partial"),
            ("Unknown after send", "unknown"),
        ):
            self.mock_scenario_combo.addItem(label, value)
        scenario_row = QHBoxLayout()
        scenario_row.addWidget(self.mock_scenario_label)
        scenario_row.addWidget(self.mock_scenario_combo, 1)
        layout.addLayout(scenario_row)
        self.mock_scenario_label.hide()
        self.mock_scenario_combo.hide()

        self.control_status_label = _label(name="readOnlyNote")
        layout.addWidget(self.control_status_label)

        self.sp_abs_entry.textEdited.connect(self._control_form_edited)
        self.autotune_sp_entry.textEdited.connect(self._control_form_edited)
        self.method_combo.currentIndexChanged.connect(self._control_form_edited)
        self.mock_scenario_combo.currentIndexChanged.connect(self._mock_scenario_changed)
        self.apply_controls_button.clicked.connect(self._apply_zone_updates)
        self.discard_controls_button.clicked.connect(self._discard_zone_edits)
        return _card("Focused zone / updates", "blue", content)

    def _control_write_ready(self, target: ZoneKey | None) -> bool:
        status = self._status
        if target is None or status is None or self._command_submitter is None:
            return False
        identity = status.target
        if (
            identity is None
            or target.bus_id != identity.key.bus_id
            or target.controller_id != identity.key.controller_id
        ):
            return False
        if status.mode == "offline-replay":
            return self._mock_scenario_setter is not None
        return (
            status.mode == "service"
            and status.connected
            and status.controller_connected is True
            and not status.read_only
        )

    @staticmethod
    def _format_control_value(value: float | None) -> str:
        return f"{value:.8f}".rstrip("0").rstrip(".") if value is not None else ""

    def _readback_controls(self, target: ZoneKey) -> tuple[str, str, str]:
        row = self.model.row_for_zone(target.zone_id)
        telemetry = row.telemetry
        config = row.config
        sp = _first(
            _number(telemetry, "sp_abs_c", "sp_abs", "sp_c", "sp", "setpoint_c", "setpoint"),
            _number(config, "sp_abs_c", "sp_abs", "sp_c", "sp", "setpoint_c", "setpoint"),
        )
        autotune_sp = _first(
            _number(telemetry, "autotune_sp_c", "autotune_sp", "sp_autotune_c", "sp_autotune"),
            _number(config, "autotune_sp_c", "autotune_sp", "sp_autotune_c", "sp_autotune"),
        )
        method = _first(
            _field(telemetry, "control_method"),
            _field(config, "control_method"),
        )
        normalized_method = str(method).strip().upper() if method is not None else ""
        if normalized_method in {"PID", "PID_CONTROL"}:
            method_value = "PID_CONTROL"
        elif normalized_method in {"ON/OFF", "ON_OFF_CONTROL"}:
            method_value = "ON_OFF_CONTROL"
        else:
            method_value = ""
        return (
            self._format_control_value(sp if isinstance(sp, float) else None),
            method_value,
            self._format_control_value(autotune_sp if isinstance(autotune_sp, float) else None),
        )

    def _sync_zone_controls(
        self,
        *,
        update_values: bool = True,
        force_values: bool = False,
    ) -> None:
        target = self.selected_zone_key
        if target is not None and target in self._control_drafts:
            values = self._control_drafts[target]
        elif target is not None:
            values = self._readback_controls(target)
        else:
            values = ("", "", "")
        render_values = force_values or (
            update_values
            and (target != self._controls_target or target not in self._control_drafts)
        )

        self._updating_controls = True
        try:
            self.control_target_label.setText(
                f"Apply target: {target.bus_id} · controller {target.controller_id} · zone {target.zone_id}"
                if target is not None
                else "No verified target selected"
            )
            if render_values:
                self.sp_abs_entry.setText(values[0])
                index = self.method_combo.findData(values[1])
                self.method_combo.setCurrentIndex(index)
                self.autotune_sp_entry.setText(values[2])
                self._controls_target = target
            offline = self._status is not None and self._status.mode == "offline-replay"
            self.mock_scenario_label.setVisible(offline)
            self.mock_scenario_combo.setVisible(offline)
            if self._pending_target is not None:
                self._control_status_text = (
                    "APPLYING — waiting for per-operation outcomes for "
                    f"zone {self._pending_target.zone_id}."
                )
            elif target is None:
                self._control_status_text = (
                    "Controller identity is not verified; zone updates are disabled."
                )
            elif not self._control_status_text.startswith(CONTROL_FEEDBACK_PREFIXES):
                if not self._control_write_ready(target):
                    self._control_status_text = (
                        "Writes are disabled until the selected service reports a connected, "
                        "fresh, verified controller. Replay edits are simulated only."
                    )
                elif self._status is not None and self._status.mode == "offline-replay":
                    self._control_status_text = (
                        "SIMULATED ONLY — choose an outcome scenario; no service or hardware request is sent."
                    )
                elif target in self._control_drafts:
                    self._control_status_text = (
                        f"Draft retained for zone {target.zone_id}; nothing has been sent."
                    )
                else:
                    self._control_status_text = (
                        "Edits stay scoped to this zone until Apply. "
                        "Service acknowledgment is not readback."
                    )
            self.control_status_label.setText(self._control_status_text)
        finally:
            self._updating_controls = False

        writable = self._control_write_ready(target) and self._pending_target is None
        awaiting_current_readback = (
            target is not None
            and self._awaiting_readback is not None
            and self._awaiting_readback[0] == target
        )
        for field in (self.sp_abs_entry, self.method_combo, self.autotune_sp_entry):
            field.setEnabled(writable)
        dirty = target is not None and target in self._control_drafts
        parsed_values = (
            self._parse_finite(self.sp_abs_entry.text()),
            self.method_combo.currentData(),
            self._parse_finite(self.autotune_sp_entry.text()),
        )
        valid = (
            parsed_values[0] is not None
            and parsed_values[1] in {"PID_CONTROL", "ON_OFF_CONTROL"}
            and parsed_values[2] is not None
        )
        self.apply_controls_button.setEnabled(
            bool(writable and dirty and valid and not awaiting_current_readback)
        )
        self.discard_controls_button.setEnabled(bool(writable and dirty))
        self.mock_scenario_combo.setEnabled(
            self._status is not None
            and self._status.mode == "offline-replay"
            and self._pending_target is None
        )

    @staticmethod
    def _parse_finite(value: str) -> float | None:
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return number if math.isfinite(number) and abs(number) <= FLOAT32_MAX else None

    def _control_form_edited(self, *_args: object) -> None:
        if self._updating_controls:
            return
        target = self.selected_zone_key
        if target is None:
            return
        self._control_drafts[target] = (
            self.sp_abs_entry.text(),
            str(self.method_combo.currentData() or ""),
            self.autotune_sp_entry.text(),
        )
        self._control_status_text = (
            f"Unsaved draft for zone {target.zone_id}; Apply sends only to this target."
        )
        self._sync_zone_controls(update_values=False)

    def _mock_scenario_changed(self, _index: int) -> None:
        if self._updating_controls or self._mock_scenario_setter is None:
            return
        scenario = self.mock_scenario_combo.currentData()
        if isinstance(scenario, str):
            self._mock_scenario_setter(scenario)
        if self._status is not None and self._status.mode == "offline-replay":
            self._control_status_text = (
                f"SIMULATED ONLY — {self.mock_scenario_combo.currentText()} selected; "
                "no service or hardware request is sent."
            )
            self.control_status_label.setText(self._control_status_text)

    def _discard_zone_edits(self) -> None:
        target = self.selected_zone_key
        if target is None or self._pending_target is not None:
            return
        self._control_drafts.pop(target, None)
        self._control_status_text = f"Draft for zone {target.zone_id} discarded; no command was sent."
        self._sync_zone_controls(force_values=True)
        self.control_status_label.setText(self._control_status_text)

    def _apply_zone_updates(self) -> None:
        target = self.selected_zone_key
        if target is None or not self._control_write_ready(target) or self._pending_target is not None:
            self._control_status_text = "Apply blocked: selected zone is not a verified writable target."
            self._sync_zone_controls()
            self.control_status_label.setText(self._control_status_text)
            return
        absolute_sp = self._parse_finite(self.sp_abs_entry.text())
        autotune_sp = self._parse_finite(self.autotune_sp_entry.text())
        method = self.method_combo.currentData()
        if (
            absolute_sp is None
            or autotune_sp is None
            or method not in {"PID_CONTROL", "ON_OFF_CONTROL"}
        ):
            self._control_status_text = (
                "Enter finite numeric °C values and select PID or ON/OFF. No command was sent."
            )
            self._sync_zone_controls()
            self.control_status_label.setText(self._control_status_text)
            return

        messages: tuple[Mapping[str, object], ...] = (
            {"op": "set_sp_abs", "zone": target.zone_id, "value_c": absolute_sp},
            {"op": "set_control_method", "zone": target.zone_id, "method": method},
            {
                "op": "set_autotune_setpoint",
                "zone": target.zone_id,
                "value_c": autotune_sp,
            },
        )
        self._pending_target = target
        self._pending_messages = messages
        self._pending_outcomes = []
        self._control_status_text = (
            "SIMULATED APPLY — outcomes pending."
            if self._status is not None and self._status.mode == "offline-replay"
            else "APPLYING — waiting for per-operation service outcomes."
        )
        self._sync_zone_controls()
        try:
            status = self._command_submitter(messages, target)
        except (OSError, RuntimeError, ValueError) as exc:
            self._pending_target = None
            self._pending_messages = ()
            self._pending_outcomes = []
            self._control_status_text = f"Request was not queued: {exc}"
            self._sync_zone_controls()
            self.control_status_label.setText(self._control_status_text)
            return
        if status in {"submitted", "mocked"} and self._pending_target is not None:
            self._control_status_text = (
                "SIMULATED APPLY — waiting for simulated outcomes."
                if status == "mocked"
                else "APPLYING — waiting for per-operation service outcomes."
            )
        elif self._pending_target is not None:
            self._control_status_text = (
                f"Request status: {status}; waiting for explicit operation outcomes."
            )
        self._sync_zone_controls()

    def set_command_outcome(self, outcome: CommandOutcome) -> None:
        if self._pending_target is None or outcome.target != self._pending_target:
            return
        expected_index = len(self._pending_outcomes)
        if expected_index >= len(self._pending_messages):
            return
        if outcome.operation != self._pending_messages[expected_index].get("op"):
            return
        self._pending_outcomes.append(outcome)
        lines = [
            f"{item.operation}: {self._outcome_text(item.result)}"
            for item in self._pending_outcomes
        ]
        simulated = outcome.endpoint == "offline-replay"
        prefix = "SIMULATED ONLY — " if simulated else ""
        self._control_status_text = prefix + "\n".join(lines)
        if len(self._pending_outcomes) < len(self._pending_messages):
            self._sync_zone_controls()
            return

        outcomes = tuple(self._pending_outcomes)
        target = self._pending_target
        messages = self._pending_messages
        self._pending_target = None
        self._pending_messages = ()
        self._pending_outcomes = []
        if all(item.result == "confirmed_success" for item in outcomes) and not simulated:
            self._awaiting_readback = (
                target,
                (
                    float(messages[0]["value_c"]),
                    str(messages[1]["method"]),
                    float(messages[2]["value_c"]),
                ),
                max(
                    (item.created_at for item in outcomes if item.created_at is not None),
                    default=datetime.now(timezone.utc),
                ),
            )
            self._control_status_text = (
                "Service acknowledged all three operations; awaiting fresh controller readback. "
                "Acknowledgment alone is not success."
            )
        elif simulated:
            self._control_status_text = (
                "SIMULATED ONLY — "
                + "\n".join(lines)
                + "\nThe modeled sequence is not atomic. No service/hardware request or readback occurred."
            )
        else:
            self._control_status_text += (
                "\nThe sequence is not atomic. No automatic retry. "
                "Review the outcome and controller state."
            )
        self._sync_zone_controls()
        self.control_status_label.setText(self._control_status_text)

    @staticmethod
    def _outcome_text(result: str) -> str:
        return {
            "confirmed_success": "acknowledged (readback not verified)",
            "confirmed_failure": "rejected",
            "not_sent": "not sent",
            "unknown": "unknown after send; do not retry blindly",
            "mock": "simulated",
        }.get(result, result)

    def _reconcile_control_readback(self, snapshot: AdapterSnapshot) -> None:
        pending = self._awaiting_readback
        if (
            pending is None
            or snapshot.category != "telemetry"
            or snapshot.data is None
            or snapshot.error
            or snapshot.stale
            or snapshot.identity is None
            or snapshot.identity.key.bus_id != pending[0].bus_id
            or snapshot.identity.key.controller_id != pending[0].controller_id
            or snapshot.source_time is None
            or snapshot.source_time.tzinfo is None
            or snapshot.source_time < pending[2]
            or self.model.identity_mismatch("telemetry")
        ):
            return
        target, expected, _acknowledged_at = pending
        row = self.model.row_for_zone(target.zone_id)
        telemetry = row.telemetry
        actual_sp = _number(
            telemetry, "sp_abs_c", "sp_abs", "sp_c", "sp", "setpoint_c", "setpoint"
        )
        actual_method = _field(telemetry, "control_method")
        actual_autotune_sp = _number(
            telemetry, "autotune_sp_c", "autotune_sp", "sp_autotune_c", "sp_autotune"
        )
        method_text = str(actual_method).strip().upper() if actual_method is not None else ""
        method_matches = method_text in {expected[1], "PID" if expected[1] == "PID_CONTROL" else "ON/OFF"}
        values_present = actual_sp is not None and actual_autotune_sp is not None
        matches = (
            values_present
            and math.isclose(actual_sp, expected[0], rel_tol=1e-6, abs_tol=1e-4)
            and method_matches
            and math.isclose(actual_autotune_sp, expected[2], rel_tol=1e-6, abs_tol=1e-4)
        )
        if matches:
            self._control_drafts.pop(target, None)
            self._awaiting_readback = None
            self._control_status_text = (
                f"Fresh controller readback matches all requested values for zone {target.zone_id}."
            )
        else:
            self._awaiting_readback = None
            self._control_status_text = (
                f"Fresh controller readback differs for zone {target.zone_id}; draft retained. "
                "Review controller state before choosing another action."
            )
        self._sync_zone_controls()

    @property
    def selected_zone_id(self) -> int | None:
        index = self.zone_table.currentIndex()
        value = index.data(ZONE_NUMBER_ROLE) if index.isValid() else None
        return value if isinstance(value, int) else None

    @property
    def selected_zone_key(self) -> ZoneKey | None:
        index = self.zone_table.currentIndex()
        value = index.data(ZONE_KEY_ROLE) if index.isValid() else None
        return value if isinstance(value, ZoneKey) else None

    def set_status(self, status: AdapterStatus | None) -> None:
        if (
            self._selected_identity is not None
            and status is not None
            and status.target is not None
            and status.target != self._selected_identity
        ):
            self._clear_selection()
        self._status = status
        self.model.set_status(status)
        self.refresh_age()
        self._update_detail()
        self._sync_zone_controls()

    def set_snapshot(self, snapshot: AdapterSnapshot) -> None:
        had_telemetry = self.model.has_telemetry
        old_identity = self.model.telemetry_identity
        selected_zone = self.selected_zone_id
        selected_identity = self._selected_identity
        self.model.set_snapshot(snapshot)
        if snapshot.category == "service_config":
            self.chart_panel.set_service_config(snapshot)
        elif snapshot.category == "analysis":
            self.chart_panel.set_analysis_snapshot(snapshot)
        new_identity = self.model.telemetry_identity
        if (
            snapshot.category == "telemetry"
            and selected_zone is not None
            and new_identity is not None
            and (
                (selected_identity is not None and selected_identity != new_identity)
                or (selected_identity is None and old_identity is None and had_telemetry)
            )
        ):
            self._clear_selection()
        elif selected_identity is not None:
            self._selected_identity = selected_identity
        if (
            not had_telemetry
            and self.model.has_telemetry
            and self.selected_zone_id is None
            and not self._require_selection
        ):
            self._select_zone(1)
        self._reconcile_control_readback(snapshot)
        self.refresh_age()
        self._update_detail()
        self._sync_zone_controls()

    def set_history(self, history: AdapterHistory | None) -> None:
        self.chart_panel.set_history(history)

    def _select_zone(self, zone_id: int) -> None:
        source_index = self.model.index(zone_id - 1, 0)
        index = self.proxy.mapFromSource(source_index)
        if index.isValid():
            self.zone_table.selectRow(index.row())
            self.zone_table.setCurrentIndex(index)

    def _clear_selection(self) -> None:
        self._require_selection = True
        self._selected_identity = None
        self.zone_table.clearSelection()
        self.zone_table.setCurrentIndex(QModelIndex())
        self.zone_selection_changed.emit(None)
        self.chart_panel.set_selection(None, None)
        self._update_detail()
        self._sync_zone_controls()

    def _selection_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if current.isValid():
            self._selected_identity = self.model.telemetry_identity
            self._require_selection = False
        target = self.selected_zone_key
        if self._pending_target is None:
            if (
                target is not None
                and self._awaiting_readback is not None
                and self._awaiting_readback[0] == target
            ):
                self._control_status_text = (
                    "Service acknowledged all three operations; awaiting fresh controller readback. "
                    "Acknowledgment alone is not success."
                )
            elif target is not None and target in self._control_drafts:
                self._control_status_text = (
                    f"Draft retained for zone {target.zone_id}; nothing has been sent."
                )
            elif target is None:
                self._control_status_text = (
                    "Controller identity is not verified; zone updates are disabled."
                )
            else:
                self._control_status_text = (
                    "Edits stay scoped to this zone until Apply. "
                    "Service acknowledgment is not readback."
                )
        self.zone_selection_changed.emit(self.selected_zone_key)
        self.chart_panel.set_selection(self.selected_zone_id, self.selected_zone_key)
        self._update_detail()
        self._sync_zone_controls()

    def _snapshot_status(self, category: str) -> str:
        snapshot = self.model.snapshot(category)
        if snapshot is None:
            return "WAITING — no snapshot"
        if self.model.identity_mismatch(category):
            return "IDENTITY MISMATCH — withheld"
        if snapshot.error:
            return f"ERROR — {snapshot.error}"
        if snapshot.data is None:
            return "MISSING — no data"
        age = snapshot_age_seconds(snapshot)
        stale = snapshot.stale or (age is not None and age > self.stale_after_s)
        time_known = snapshot.source_time is not None and snapshot.source_time.tzinfo is not None
        state = "STALE" if stale else "CURRENT" if time_known else "TIME UNKNOWN"
        timestamp = (
            snapshot.source_time.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
            if time_known and snapshot.source_time is not None
            else "source time unavailable"
        )
        age_label = _age_text(age) if time_known else f"file {_age_text(age)}"
        return f"{state} · {timestamp} · {age_label}"

    def refresh_age(self) -> None:
        status = self._status
        if status is None:
            mode = "DISCONNECTED / READ-ONLY"
        elif status.mode == "offline-replay":
            mode = "OFFLINE REPLAY / STATIC · NO HARDWARE"
        elif status.connected and not status.read_only:
            mode = "SERVICE AND CONTROLLER CONNECTED · zone updates enabled"
        elif status.connected:
            mode = "SERVICE REACHABLE · READ-ONLY VIEW"
        else:
            mode = "SERVICE UNAVAILABLE · READ-ONLY VIEW"
        self.health_label.setText(mode)
        identity = self.model.telemetry_identity
        if identity is None:
            self.identity_label.setText("Controller identity unknown or unverified")
        else:
            self.identity_label.setText(
                f"{identity.key.bus_id} · controller {identity.key.controller_id} "
                f"· address {identity.modbus_address}"
            )
        notes = [
            f"Telemetry: {self._snapshot_status('telemetry')}",
            f"Analysis: {self._snapshot_status('analysis')}",
            f"Config: {self._snapshot_status('config')}",
            f"Names: {self._snapshot_status('service_config')}",
        ]
        if status is not None and status.identity_warning:
            notes.append(status.identity_warning)
        if status is not None and status.mode == "offline-replay":
            notes.append("Replay analysis is mock-derived; no controller is connected.")
        self.source_label.setText("\n".join(notes))

    def _update_detail(self) -> None:
        zone_id = self.selected_zone_id
        if zone_id is None:
            message = "Select a zone to inspect readbacks"
            if self._require_selection:
                message = "Controller changed — select a zone again"
            self.focus_label.setText(message)
            self.measurement_label.setText("PV: N/A    SP: N/A    Output: N/A")
            for label in self.detail_labels.values():
                label.setText("N/A")
            return

        row = self.model.row_for_zone(zone_id)
        identity = (
            f"{row.key.bus_id} / controller {row.key.controller_id}"
            if row.key is not None
            else "Identity unknown / read-only"
        )
        self.focus_label.setText(f"Zone {zone_id} · {row.name}\n{identity}")
        values = self.model._values(row)
        self.measurement_label.setText(
            f"PV: {_number_text(_as_float(values[Column.PV]), 2, ' °C')}    "
            f"SP: {_number_text(_as_float(values[Column.SETPOINT]), 2, ' °C')}    "
            f"Output: {_number_text(_as_float(values[Column.OUTPUT]), 1, ' %')}"
        )
        telemetry, config, analysis = row.telemetry, row.config, row.analysis
        sensor_type = _first(_field(telemetry, "sensor_type"), _field(config, "sensor_type"))
        sensor_subtype = _first(_field(telemetry, "sensor_subtype"), _field(config, "sensor_subtype"))
        self.detail_labels["sensor"].setText(
            f"{_text(sensor_type)} · subtype {_text(sensor_subtype)}"
        )
        self.detail_labels["sensor_status"].setText(
            _text(_first(_field(config, "sensor_status"), _field(telemetry, "sensor_status")))
        )
        alarm1 = _first(
            _nested(config, "alarms", "alarm1", "status"),
            _field(telemetry, "alarm1_status", "alarm_status"),
        )
        alarm2 = _first(_nested(config, "alarms", "alarm2", "status"), _field(telemetry, "alarm2_status"))
        self.detail_labels["alarms"].setText(f"{_text(alarm1)} / {_text(alarm2)}")
        self.detail_labels["control"].setText(
            f"Method: {_text(_field(telemetry, 'control_method', 'mode', 'control_mode'))}\n"
            f"Mode: {_text(_field(telemetry, 'control_mode', 'mode', 'control_method'))}\n"
            f"Loop: {_text(_field(telemetry, 'loop_status', 'status'))}"
        )
        autotune = _field(telemetry, "autotune_enable", "autotune_enabled")
        if autotune == "ENABLE" or autotune is True:
            autotune_text = "On"
        elif autotune == "DISABLE" or autotune is False:
            autotune_text = "Off"
        else:
            autotune_text = _text(autotune)
        autotune_sp = _number(
            telemetry, "autotune_sp_c", "autotune_sp", "sp_autotune_c", "sp_autotune", "SP_autotune"
        )
        self.detail_labels["autotune"].setText(
            f"{autotune_text} · SP {_number_text(autotune_sp, 1, ' °C')}"
        )
        gains = (
            _number(telemetry, "p_gain", "kp", "proportional_gain"),
            _number(telemetry, "i_gain", "ki", "integral_gain"),
            _number(telemetry, "d_gain", "kd", "derivative_gain"),
        )
        self.detail_labels["pid"].setText(" / ".join(_number_text(value, 4) for value in gains))
        pid_config = _mapping(config.get("pid_parameters"))
        deadband = _number(pid_config, "deadband")
        cycle = _first(_number(pid_config, "cycle_time_s"), _number(telemetry, "cycle_time_s"))
        self.detail_labels["timing"].setText(
            f"{_number_text(deadband, 2, ' °C')} / "
            f"{_number_text(cycle if isinstance(cycle, float) else None, 1, ' s')}"
        )
        segment = _mapping(telemetry.get("segment"))
        segment_index = _first(
            _field(telemetry, "current_segment_index", "segment_index", "ramp_soak_segment"),
            _field(segment, "current_index"),
        )
        segment_state = _first(
            _field(telemetry, "current_segment_state", "segment_state"),
            _field(segment, "current_state"),
        )
        remaining = _first(
            _number(telemetry, "ramp_soak_remaining", "ramp_soak_remaining_s", "remaining_s"),
            _number(segment, "ramp_soak_remaining", "remaining_s"),
        )
        self.detail_labels["segment"].setText(
            f"{_text(segment_index)} · {_text(segment_state)}\n"
            f"Remaining: {_number_text(remaining if isinstance(remaining, float) else None, 1, ' s')}"
        )
        mae = _number(analysis, "avg_abs_error_c", "mae_c", "mae", "mean_abs_error_c")
        threshold = _number(analysis, "threshold_c", "equilibrium_threshold_c")
        window = _number(analysis, "window_s")
        points = _number(analysis, "n_points", "point_count")
        equilibrium = _equilibrium(_field(analysis, "in_equilibrium", "equilibrium"))
        prefix = "Mock-derived · " if self._status and self._status.mode == "offline-replay" else ""
        self.detail_labels["analysis"].setText(
            f"{prefix}Equilibrium: {equilibrium}\n"
            f"MAE: {_number_text(mae, 3, ' °C')} · threshold: {_number_text(threshold, 2, ' °C')}\n"
            f"Window: {_number_text(window, 0, ' s')} · samples: {_number_text(points, 0)}"
        )

    def shutdown(self) -> None:
        self._age_timer.stop()
        self.chart_panel.shutdown()
