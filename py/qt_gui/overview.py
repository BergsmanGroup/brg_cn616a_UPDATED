"""Read-only, keyed overview of the adapter's cached controller snapshots."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import IntEnum

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
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
    ControllerIdentity,
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


class OverviewPage(QWidget):
    """Prototype-inspired overview; selection only changes the local view."""

    zone_selection_changed = Signal(object)

    def __init__(
        self,
        *,
        stale_after_s: float = 5.0,
        history_requester: Callable[[float], int] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not math.isfinite(stale_after_s) or stale_after_s <= 0:
            raise ValueError("stale_after_s must be positive and finite")
        self.stale_after_s = stale_after_s
        self._history_requester = history_requester
        self.dirty_readbacks = DirtyReadbackGuard()
        self.model = TelemetryTableModel(self)
        self.proxy = ZoneSortProxy(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SORT_ROLE)
        self.proxy.setDynamicSortFilter(True)
        self._status: AdapterStatus | None = None
        self._selected_identity: ControllerIdentity | None = None
        self._require_selection = False
        self._build_ui()
        self._age_timer = QTimer(self)
        self._age_timer.setInterval(1000)
        self._age_timer.timeout.connect(self.refresh_age)
        self._age_timer.start()
        self.refresh_age()
        self._update_detail()

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
        title_row.addWidget(_label("VIEW ONLY · controls not yet migrated", name="viewOnly"))
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
        detail_layout.addWidget(
            _label(
                "Read-only values. Setpoint, autotune and recording controls will be added later.",
                name="readOnlyNote",
            )
        )
        rail_layout.addWidget(_card("Focused zone  /  readbacks", "blue", detail_content))
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
        self.refresh_age()
        self._update_detail()

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

    def _selection_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if current.isValid():
            self._selected_identity = self.model.telemetry_identity
            self._require_selection = False
        self.zone_selection_changed.emit(self.selected_zone_key)
        self.chart_panel.set_selection(self.selected_zone_id, self.selected_zone_key)
        self._update_detail()

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
