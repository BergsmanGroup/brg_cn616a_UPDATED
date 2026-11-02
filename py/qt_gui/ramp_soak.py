from __future__ import annotations

import math
import struct
from collections.abc import Mapping
from typing import Any, Protocol

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from py.gui.state_values import get_field, get_numeric, get_zone, get_zones

from .adapter import (
    AdapterSnapshot,
    AdapterStatus,
    CommandOutcome,
    ControllerIdentity,
    SubmissionStatus,
    ZoneKey,
    snapshot_age_seconds,
)


SEGMENT_COUNT = 20
CONTROL_MODES = (
    ("Standard (no Ramp/Soak)", "STANDARD_CONTROL"),
    ("Ramp/Soak - Stop at end", "RAMP_SOAK_1_STOP"),
    ("Ramp/Soak - Hold at end", "RAMP_SOAK_2_HOLD"),
)
_SEGMENT_FIELDS = (
    ("sp_c", ("sp_c", "setpoint_c", "setpoint")),
    ("slope_c_per_min", ("slope_c_per_min", "slope", "rate_c_per_min")),
    ("time_h", ("time_h", "time", "duration_h")),
)


class RampSoakSubmitter(Protocol):
    def __call__(
        self,
        messages: tuple[Mapping[str, object], ...],
        *,
        target: ZoneKey | None,
        coalesce_key: str | None,
    ) -> SubmissionStatus: ...


class ProfileTableModel(QAbstractTableModel):
    """Editable 20-row profile table; blank cells remain distinct from zero."""

    dirty_changed = Signal(bool)
    HEADERS = ("Segment", "Setpoint (°C)", "Slope (°C/min)", "Time (h)")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows = [["", "", ""] for _ in range(SEGMENT_COUNT)]
        self._dirty = False

    @property
    def dirty(self) -> bool:
        return self._dirty

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else SEGMENT_COUNT

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < SEGMENT_COUNT):
            return None
        if index.column() == 0:
            if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
                return index.row() + 1
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return int(Qt.AlignmentFlag.AlignCenter)
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return self._rows[index.row()][index.column() - 1]
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section] if 0 <= section < len(self.HEADERS) else None
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Vertical:
            return section + 1
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() > 0:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(
        self,
        index: QModelIndex,
        value: Any,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if (
            role != Qt.ItemDataRole.EditRole
            or not index.isValid()
            or index.column() == 0
            or not (0 <= index.row() < SEGMENT_COUNT)
        ):
            return False
        text = str(value).strip()
        if text == self._rows[index.row()][index.column() - 1]:
            return True
        self._rows[index.row()][index.column() - 1] = text
        self._set_dirty(True)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole])
        return True

    def set_segments(self, segments: object) -> None:
        self.beginResetModel()
        rows = [["", "", ""] for _ in range(SEGMENT_COUNT)]
        if isinstance(segments, Mapping):
            for index in range(1, SEGMENT_COUNT + 1):
                segment = get_zone(segments, index)
                if not isinstance(segment, Mapping):
                    continue
                for column, (_field, aliases) in enumerate(_SEGMENT_FIELDS):
                    raw = get_field(segment, *aliases)
                    if (
                        isinstance(raw, (int, float))
                        and not isinstance(raw, bool)
                        and math.isfinite(float(raw))
                    ):
                        rows[index - 1][column] = f"{float(raw):.6f}".rstrip("0").rstrip(".")
        self._rows = rows
        self._dirty = False
        self.endResetModel()
        self.dirty_changed.emit(False)

    def clear(self) -> None:
        self.set_segments({})

    def build_payload(self) -> dict[str, dict[str, float]]:
        payload: dict[str, dict[str, float]] = {}
        for row_index, row in enumerate(self._rows):
            parsed: list[float | None] = []
            for column, text in enumerate(row):
                if not text:
                    parsed.append(None)
                    continue
                field_name = self.HEADERS[column + 1]
                try:
                    number = float(text)
                except ValueError as exc:
                    raise ValueError(f"Segment {row_index + 1} {field_name} must be a number.") from exc
                if not math.isfinite(number):
                    raise ValueError(f"Segment {row_index + 1} {field_name} must be finite.")
                try:
                    struct.pack(">f", number)
                except (OverflowError, struct.error) as exc:
                    raise ValueError(
                        f"Segment {row_index + 1} {field_name} is outside the controller's float range."
                    ) from exc
                parsed.append(number)
            if any(value is not None for value in parsed):
                payload[str(row_index + 1)] = {
                    field_name: value if value is not None else 0.0
                    for (field_name, _aliases), value in zip(_SEGMENT_FIELDS, parsed)
                }
        return payload

    def mark_clean(self) -> None:
        self._set_dirty(False)

    def _set_dirty(self, value: bool) -> None:
        if self._dirty != value:
            self._dirty = value
            self.dirty_changed.emit(value)


class RampSoakPage(QWidget):
    """Qt editor for the existing v1 Ramp/Soak profile read/write contract."""

    def __init__(
        self,
        command_submitter: RampSoakSubmitter | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._command_submitter = command_submitter
        self._snapshots: dict[str, AdapterSnapshot] = {}
        self._status: AdapterStatus | None = None
        self._writes_allowed = False
        self._selected_zone_id = 1
        self._identity: ControllerIdentity | None = None
        self._updating = False
        self._count_dirty = False
        self._mode_dirty = False
        self._pending_operations: set[str] = set()
        self._pending_trigger_key: str | None = None
        self._pending_profile_zones: tuple[int, ...] = ()
        self._unresolved_triggers: dict[str, str] = {}
        self._snapshot_signatures: dict[str, tuple[object, ...]] = {}
        self._request_signature: tuple[object, ...] | None = None
        self._awaiting_category: str | None = None
        self._awaiting_readback = False

        self._build_ui()
        self.profile_model.dirty_changed.connect(self._on_dirty_changed)
        self.zone_combo.currentIndexChanged.connect(self._on_zone_changed)
        self.segment_count.valueChanged.connect(self._on_count_edited)
        self.control_mode.currentIndexChanged.connect(self._on_mode_edited)
        self.reload_button.clicked.connect(self._reload_clicked)
        self.discard_button.clicked.connect(self.discard_edits)
        self.save_button.clicked.connect(self._save_segments)
        self.apply_count_button.clicked.connect(self._apply_count)
        self.apply_mode_button.clicked.connect(self._apply_mode)
        self.start_selected_button.clicked.connect(
            lambda: self._start_stop_profile(True, all_monitored=False)
        )
        self.stop_selected_button.clicked.connect(
            lambda: self._start_stop_profile(False, all_monitored=False)
        )
        self.start_monitored_button.clicked.connect(
            lambda: self._start_stop_profile(True, all_monitored=True)
        )
        self.stop_monitored_button.clicked.connect(
            lambda: self._start_stop_profile(False, all_monitored=True)
        )
        self.start_autotune_button.clicked.connect(self._start_autotune)
        self.stop_autotune_button.clicked.connect(self._stop_autotune)
        self.acknowledge_unknown_button.clicked.connect(self._acknowledge_unknown)
        self._refresh_action_states()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)

        intro = QLabel(
            "Edit up to 20 profile rows. Blank fields in a populated row are written as zero. "
            "Profile start/stop controls are intentionally not available in this step."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        identity = QFormLayout()
        self.zone_combo = QComboBox()
        self.zone_combo.setObjectName("rampSoakZone")
        self.zone_combo.setMinimumContentsLength(24)
        self.zone_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        identity.addRow("Selected zone:", self.zone_combo)
        self.live_status_label = QLabel("Current state/segment: unavailable")
        self.live_status_label.setWordWrap(True)
        identity.addRow("Live readback:", self.live_status_label)
        self.profile_source_label = QLabel("Profile readback: unavailable")
        self.profile_source_label.setWordWrap(True)
        identity.addRow("Profile source:", self.profile_source_label)
        layout.addLayout(identity)

        controls = QHBoxLayout()
        self.control_mode = QComboBox()
        self.control_mode.setObjectName("rampSoakControlMode")
        for label, wire_mode in CONTROL_MODES:
            self.control_mode.addItem(label, wire_mode)
        self.apply_mode_button = QPushButton("Apply mode")
        controls.addWidget(QLabel("Control mode:"))
        controls.addWidget(self.control_mode)
        controls.addWidget(self.apply_mode_button)
        self.segment_count = QSpinBox()
        self.segment_count.setObjectName("rampSoakSegmentCount")
        self.segment_count.setRange(0, SEGMENT_COUNT)
        self.apply_count_button = QPushButton("Apply count")
        controls.addSpacing(14)
        controls.addWidget(QLabel("Active segments:"))
        controls.addWidget(self.segment_count)
        controls.addWidget(self.apply_count_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.profile_execution_label = QLabel("Profile execution readback: unavailable.")
        self.profile_execution_label.setWordWrap(True)
        layout.addWidget(self.profile_execution_label)
        profile_actions = QHBoxLayout()
        self.start_selected_button = QPushButton("Start selected zone")
        self.stop_selected_button = QPushButton("Stop selected zone")
        self.start_monitored_button = QPushButton("Start all monitored zones")
        self.stop_monitored_button = QPushButton("Stop all monitored zones")
        for button in (
            self.start_selected_button,
            self.stop_selected_button,
            self.start_monitored_button,
            self.stop_monitored_button,
        ):
            profile_actions.addWidget(button)
        profile_actions.addStretch(1)
        layout.addLayout(profile_actions)

        autotune_info = QHBoxLayout()
        self.autotune_state_label = QLabel("Autotune state: unavailable.")
        self.autotune_state_label.setWordWrap(True)
        self.autotune_setpoint_label = QLabel(
            "Autotune SP: unavailable (a setpoint, not a start trigger)."
        )
        self.autotune_setpoint_label.setWordWrap(True)
        autotune_info.addWidget(self.autotune_state_label, 1)
        autotune_info.addWidget(self.autotune_setpoint_label, 1)
        layout.addLayout(autotune_info)
        autotune_actions = QHBoxLayout()
        self.start_autotune_button = QPushButton("Start autotune — selected zone")
        self.stop_autotune_button = QPushButton("Stop autotune — selected zone")
        autotune_actions.addWidget(self.start_autotune_button)
        autotune_actions.addWidget(self.stop_autotune_button)
        autotune_actions.addStretch(1)
        layout.addLayout(autotune_actions)

        self.profile_model = ProfileTableModel(self)
        self.profile_table = QTableView()
        self.profile_table.setObjectName("rampSoakProfileTable")
        self.profile_table.setModel(self.profile_model)
        self.profile_table.setAlternatingRowColors(True)
        self.profile_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectItems)
        self.profile_table.setTabKeyNavigation(True)
        self.profile_table.setMinimumHeight(420)
        self.profile_table.horizontalHeader().setStretchLastSection(True)
        self.profile_table.setColumnWidth(0, 75)
        self.profile_table.setColumnWidth(1, 190)
        self.profile_table.setColumnWidth(2, 190)
        self.profile_table.setColumnWidth(3, 150)
        layout.addWidget(self.profile_table, 1)

        actions = QHBoxLayout()
        self.reload_button = QPushButton("Reload from service")
        self.discard_button = QPushButton("Discard edits")
        self.save_button = QPushButton("Save segment changes")
        actions.addWidget(self.reload_button)
        actions.addWidget(self.discard_button)
        actions.addWidget(self.save_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.status_label = QLabel("No profile action requested.")
        self.status_label.setObjectName("rampSoakStatus")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.uncertainty_label = QLabel("")
        self.uncertainty_label.setObjectName("rampSoakUncertainty")
        self.uncertainty_label.setWordWrap(True)
        self.uncertainty_label.setStyleSheet("color: #8b1e1e; font-weight: 700;")
        layout.addWidget(self.uncertainty_label)
        self.acknowledge_unknown_button = QPushButton("Acknowledge unresolved outcome")
        layout.addWidget(self.acknowledge_unknown_button)

    def set_adapter_status(self, status: AdapterStatus | None, *, writes_allowed: bool) -> None:
        self._status = status
        self._writes_allowed = bool(writes_allowed)
        self._identity = status.target if status is not None else None
        self._refresh_action_states()

    def set_snapshot(self, snapshot: AdapterSnapshot) -> None:
        self._snapshots[snapshot.category] = snapshot
        if snapshot.category in {"rampsoak", "telemetry"}:
            previous = self._snapshot_signatures.get(snapshot.category)
            signature = self._signature(snapshot)
            self._snapshot_signatures[snapshot.category] = signature
            if (
                self._awaiting_readback
                and snapshot.category == self._awaiting_category
                and signature != self._request_signature
                and signature != previous
            ):
                self._awaiting_readback = False
                self.status_label.setText("Profile snapshot refreshed; readback is now available.")
        if snapshot.category == "service_config":
            self._refresh_zone_names()
        if snapshot.category == "rampsoak":
            self._update_profile_source(snapshot)
            self._refresh_zone_names()
            self._refresh_zone_fields(force=False)
        elif snapshot.category == "telemetry":
            self._update_live_status()
            self._refresh_zone_fields(force=False)

    def set_command_outcome(self, outcome: CommandOutcome) -> None:
        operation = outcome.operation
        if operation not in self._pending_operations:
            return
        self._pending_operations.discard(operation)
        if outcome.result in {"confirmed_success", "mock"}:
            if operation == "set_rampsoak_segments":
                self.profile_model.mark_clean()
            elif operation == "set_num_segments":
                self._count_dirty = False
            elif operation == "set_control_mode":
                self._mode_dirty = False
            current_signature = self._current_signature(self._awaiting_category)
            snapshot_refreshed = (
                self._request_signature is not None
                and current_signature != self._request_signature
            )
            if snapshot_refreshed:
                self._awaiting_readback = False
                self.status_label.setText(
                    "Profile snapshot refreshed; readback is now available."
                )
            elif operation == "read_rampsoak":
                if self._awaiting_readback:
                    self.status_label.setText(
                        "Read request acknowledged; waiting for the refreshed profile snapshot."
                    )
                else:
                    self.status_label.setText("Read request acknowledged; cached profile remains unchanged.")
            else:
                simulated = "SIMULATED ONLY; " if outcome.result == "mock" else ""
                self.status_label.setText(
                    f"{simulated}{operation} acknowledged; readback is not yet verified. "
                    "Use Reload from service to verify."
                )
            self._pending_trigger_key = None
        else:
            self._awaiting_readback = False
            if outcome.result == "unknown" and self._pending_trigger_key is not None:
                self._unresolved_triggers[self._pending_trigger_key] = (
                    f"{self._pending_trigger_key}: outcome unknown after send; it may have "
                    "executed. Status snapshots cannot prove whether the trigger ran. "
                    "No retry was made."
                )
                self._pending_trigger_key = None
                self._refresh_uncertainty()
            if (
                operation == "set_start_profile"
                and len(self._pending_profile_zones) > 1
                and outcome.result == "confirmed_failure"
            ):
                detail = (
                    "The service rejected/failed the multi-zone request; some earlier zone "
                    "writes may have applied. Inspect fresh per-zone readbacks before any "
                    "further action. No retry was made."
                )
            else:
                detail = outcome.message
            self.status_label.setText(f"{operation}: {outcome.result} — {detail}")
            self._pending_trigger_key = None
        self._pending_profile_zones = ()
        self._refresh_action_states()

    def on_tab_selected(self) -> None:
        self._request_read()

    def shutdown(self) -> None:
        self._pending_operations.clear()

    def _signature(self, snapshot: AdapterSnapshot) -> tuple[object, ...]:
        data = dict(snapshot.data) if snapshot.data is not None else None
        section = "rampsoak" if snapshot.category == "rampsoak" else "telemetry"
        zones = get_zones(data, section)
        current = get_zone(zones, self._selected_zone_id)
        return (
            snapshot.source_time,
            snapshot.modified_at,
            repr(dict(current)) if isinstance(current, Mapping) else repr(current),
        )

    def _current_signature(self, category: str | None) -> tuple[object, ...] | None:
        snapshot = self._snapshots.get(category) if category is not None else None
        return self._signature(snapshot) if snapshot is not None else None

    def _refresh_zone_names(self) -> None:
        service_snapshot = self._snapshots.get("service_config")
        data = (
            dict(service_snapshot.data)
            if service_snapshot is not None and service_snapshot.data is not None
            else None
        )
        config = get_field(data, "config", "service_config")
        names = get_field(config, "zone_names")
        desired = self._selected_zone_id
        self._updating = True
        try:
            for index in range(6):
                zone_id = index + 1
                name = get_field(names, str(zone_id)) if isinstance(names, Mapping) else None
                label = f"{zone_id} - {name}" if isinstance(name, str) and name.strip() else f"Zone {zone_id}"
                if self.zone_combo.count() <= index:
                    self.zone_combo.addItem(label, zone_id)
                else:
                    self.zone_combo.setItemText(index, label)
            selected = self.zone_combo.findData(desired)
            if selected >= 0:
                self.zone_combo.setCurrentIndex(selected)
        finally:
            self._updating = False
        self._refresh_zone_fields(force=False)

    def _on_zone_changed(self, index: int) -> None:
        if self._updating or index < 0:
            return
        new_zone = self.zone_combo.itemData(index)
        if isinstance(new_zone, bool) or not isinstance(new_zone, int):
            return
        if new_zone == self._selected_zone_id:
            return
        if self._is_dirty():
            choice = QMessageBox.question(
                self,
                "Discard profile edits?",
                "Changing zones will discard unsaved profile edits. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if choice != QMessageBox.StandardButton.Yes:
                self._updating = True
                try:
                    previous = self.zone_combo.findData(self._selected_zone_id)
                    if previous >= 0:
                        self.zone_combo.setCurrentIndex(previous)
                finally:
                    self._updating = False
                return
        self._selected_zone_id = new_zone
        for category in ("rampsoak", "telemetry"):
            signature = self._current_signature(category)
            if signature is not None:
                self._snapshot_signatures[category] = signature
        self._awaiting_readback = False
        self._awaiting_category = None
        self._clear_dirty()
        self._refresh_zone_fields(force=True)

    def _refresh_zone_fields(self, *, force: bool) -> None:
        ramp_snapshot = self._snapshots.get("rampsoak")
        ramp_data = (
            dict(ramp_snapshot.data)
            if ramp_snapshot is not None and ramp_snapshot.data is not None
            else None
        )
        zones = get_zones(ramp_data, "rampsoak")
        zone_data = get_zone(zones, self._selected_zone_id)
        if not isinstance(zone_data, Mapping):
            zone_data = {}
        if force or (not self.profile_model.dirty and not self._awaiting_readback):
            self.profile_model.set_segments(get_field(zone_data, "segments", "profile_segments"))
        if force or not self._count_dirty:
            count = get_numeric(zone_data, "num_segments", "segment_count")
            self._updating = True
            try:
                self.segment_count.setValue(int(count) if count is not None and count.is_integer() else 0)
            finally:
                self._updating = False

        telemetry_snapshot = self._snapshots.get("telemetry")
        telemetry_data = (
            dict(telemetry_snapshot.data)
            if telemetry_snapshot is not None and telemetry_snapshot.data is not None
            else None
        )
        telemetry_zones = get_zones(telemetry_data, "telemetry")
        telemetry_zone = get_zone(telemetry_zones, self._selected_zone_id)
        telemetry_fresh = (
            telemetry_snapshot is not None
            and telemetry_snapshot.data is not None
            and telemetry_snapshot.error is None
            and not telemetry_snapshot.stale
        )
        mode = get_field(telemetry_zone, "control_mode", "control_method", "mode")
        if force or not self._mode_dirty:
            mode_index = self.control_mode.findData(str(mode).strip().upper()) if mode is not None else -1
            self._updating = True
            try:
                self.control_mode.setCurrentIndex(mode_index)
            finally:
                self._updating = False
        self._update_live_status()
        self._update_execution_status(
            zone_data,
            telemetry_zone,
            execution_readback_fresh=(
                ramp_snapshot is not None
                and ramp_snapshot.data is not None
                and ramp_snapshot.error is None
                and not ramp_snapshot.stale
                and telemetry_fresh
            ),
            telemetry_readback_fresh=telemetry_fresh,
        )
        self._refresh_action_states()

    def _update_execution_status(
        self,
        ramp_zone: Mapping[str, object],
        telemetry_zone: Mapping[str, object],
        *,
        execution_readback_fresh: bool,
        telemetry_readback_fresh: bool,
    ) -> None:
        profile_state = get_field(ramp_zone, "start_profile")
        profile_text = (
            str(profile_state).strip().upper()
            if profile_state is not None
            else "unknown"
        )
        self.profile_execution_label.setText(
            f"Profile execution readback for zone {self._selected_zone_id}: {profile_text}. "
            "This is the controller's profile-enable field, not a fabricated Run/Idle or pause state."
        )
        self._profile_readback_fresh = execution_readback_fresh
        self._profile_enable_state = profile_text
        autotune_state = get_field(telemetry_zone, "autotune_enable", "autotune_enabled")
        autotune_text = (
            str(autotune_state).strip().upper()
            if autotune_state is not None
            else "unknown"
        )
        autotune_sp = get_numeric(
            telemetry_zone,
            "autotune_sp_c",
            "autotune_sp",
            "sp_autotune_c",
            "sp_autotune",
            "SP_autotune",
        )
        self.autotune_state_label.setText(
            f"Autotune state for zone {self._selected_zone_id}: {autotune_text}."
        )
        self._autotune_readback_fresh = telemetry_readback_fresh
        self._autotune_enable_state = autotune_text
        self.autotune_setpoint_label.setText(
            "Autotune SP: "
            + (f"{autotune_sp:g} °C" if autotune_sp is not None else "unknown")
            + "; this target is distinct from the start/stop trigger."
        )

    def _refresh_uncertainty(self) -> None:
        if self._unresolved_triggers:
            self.uncertainty_label.setText(
                "\n".join(self._unresolved_triggers.values())
            )
        else:
            self.uncertainty_label.clear()

    def _update_live_status(self) -> None:
        telemetry_snapshot = self._snapshots.get("telemetry")
        if telemetry_snapshot is None or telemetry_snapshot.data is None:
            self.live_status_label.setText("Current state/segment: unavailable; telemetry snapshot is missing.")
            return
        zones = get_zones(dict(telemetry_snapshot.data), "telemetry")
        zone = get_zone(zones, self._selected_zone_id)
        segment = get_field(zone, "current_segment_index", "segment_index", "ramp_soak_segment")
        state = get_field(zone, "current_segment_state", "segment_state")
        mode = get_field(zone, "control_mode", "control_method", "mode")
        loop = get_field(zone, "loop_status", "status")
        age = snapshot_age_seconds(telemetry_snapshot)
        age_text = "unknown" if age is None else f"{age:.1f} s"
        freshness = "STALE" if telemetry_snapshot.stale else "fresh"
        self.live_status_label.setText(
            f"Current segment: {segment if segment is not None else 'N/A'} "
            f"({state if state is not None else 'N/A'}); mode: {mode if mode is not None else 'N/A'}; "
            f"loop: {loop if loop is not None else 'N/A'}; telemetry {freshness}, age {age_text}."
        )

    def _update_profile_source(self, snapshot: AdapterSnapshot) -> None:
        age = snapshot_age_seconds(snapshot)
        age_text = "unknown" if age is None else f"{age:.1f} s"
        if snapshot.error:
            detail = f"error: {snapshot.error}"
        elif snapshot.stale:
            detail = "STALE"
        elif snapshot.data is None:
            detail = "missing"
        else:
            zones = get_zones(dict(snapshot.data), "rampsoak")
            detail = (
                "available"
                if get_zone(zones, self._selected_zone_id)
                else f"missing for zone {self._selected_zone_id}"
            )
        self.profile_source_label.setText(
            f"{detail}; source time {snapshot.source_time.isoformat() if snapshot.source_time else 'unknown'}; "
            f"age {age_text}."
        )

    def _on_dirty_changed(self, _dirty: bool) -> None:
        self._refresh_action_states()

    def _on_count_edited(self, _value: int) -> None:
        if not self._updating:
            self._count_dirty = True
            self._refresh_action_states()

    def _on_mode_edited(self, _index: int) -> None:
        if not self._updating:
            self._mode_dirty = True
            self._refresh_action_states()

    def _is_dirty(self) -> bool:
        return self.profile_model.dirty or self._count_dirty or self._mode_dirty

    def _clear_dirty(self) -> None:
        self.profile_model.mark_clean()
        self._count_dirty = False
        self._mode_dirty = False

    def discard_edits(self) -> None:
        self._clear_dirty()
        self._awaiting_readback = False
        self._refresh_zone_fields(force=True)
        self.status_label.setText("Unsaved edits discarded; showing the latest cached snapshot.")

    def _reload_clicked(self) -> None:
        if self._is_dirty():
            choice = QMessageBox.question(
                self,
                "Discard profile edits?",
                "Reloading will discard unsaved profile edits. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return
            self._clear_dirty()
        self._request_read()

    def _request_read(self) -> None:
        self._submit(
            {"op": "read_rampsoak"},
            "Requesting profile readback…",
            coalesce_key="rampsoak-read",
            allow_read_only=True,
        )

    def _save_segments(self) -> None:
        try:
            segments = self.profile_model.build_payload()
        except ValueError as exc:
            self.status_label.setText(f"Invalid profile: {exc}")
            return
        if not segments:
            self.status_label.setText("No segment values to save.")
            return
        self._submit(
            {"op": "set_rampsoak_segments", "zone": self._selected_zone_id, "segments": segments},
            "Saving profile segments…",
        )

    def _apply_count(self) -> None:
        count = self.segment_count.value()
        if not 0 <= count <= SEGMENT_COUNT:
            self.status_label.setText("Active segment count must be between 0 and 20.")
            return
        self._submit(
            {"op": "set_num_segments", "zone": self._selected_zone_id, "count": count},
            "Applying active segment count…",
        )

    def _apply_mode(self) -> None:
        mode = self.control_mode.currentData()
        if mode not in {wire for _label, wire in CONTROL_MODES}:
            self.status_label.setText("Choose a supported control mode.")
            return
        self._submit(
            {"op": "set_control_mode", "zone": self._selected_zone_id, "mode": mode},
            "Applying control mode…",
        )

    def _monitored_zone_ids(self) -> list[int]:
        snapshot = self._snapshots.get("service_config")
        data = dict(snapshot.data) if snapshot is not None and snapshot.data is not None else None
        config = get_field(data, "config", "service_config")
        mode = get_field(config, "zones_mode")
        if mode == "all":
            return list(range(1, 7))
        if mode != "list":
            raise ValueError("The monitored-zone configuration is unavailable or invalid.")
        raw_zones = get_field(config, "zones_list")
        if not isinstance(raw_zones, (list, tuple)) or not raw_zones:
            raise ValueError("The monitored-zone list is empty; no profile trigger was sent.")
        zones: list[int] = []
        for zone in raw_zones:
            if isinstance(zone, bool) or not isinstance(zone, int) or not 1 <= zone <= 6:
                raise ValueError("The monitored-zone list contains an invalid zone; no trigger was sent.")
            if zone not in zones:
                zones.append(zone)
        return zones

    def _start_stop_profile(self, enable: bool, *, all_monitored: bool) -> None:
        if all_monitored:
            try:
                zones = self._monitored_zone_ids()
            except ValueError as exc:
                self.status_label.setText(str(exc))
                return
            target_description = "all monitored zones " + ", ".join(map(str, zones))
        else:
            zones = [self._selected_zone_id]
            target_description = f"selected zone {self._selected_zone_id}"
        action = "START" if enable else "STOP"
        wire_value = "ENABLE" if enable else "DISABLE"
        choice = QMessageBox.question(
            self,
            f"Confirm profile {action.lower()}",
            f"{action} the Ramp/Soak profile for {target_description}?\n\n"
            f"This sends set_start_profile with zones={zones} and enable={wire_value}. "
            "It can change controller output behavior. The GUI cannot infer pause/resume "
            "or a separate hardware Run/Idle state.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            self.status_label.setText(f"Profile {action.lower()} canceled; no command was sent.")
            return
        target = self._target()
        if target is None:
            self.status_label.setText("Controller identity is unknown; no profile trigger was sent.")
            return
        target = ZoneKey(target.bus_id, target.controller_id, zones[0])
        self._submit(
            {"op": "set_start_profile", "zones": zones, "enable": enable},
            f"Sending profile {action.lower()} to zones {zones}…",
            trigger_key=f"profile:{','.join(map(str, zones))}:{wire_value}",
            target=target,
            profile_zones=tuple(zones),
        )

    def _start_autotune(self) -> None:
        zone = self._selected_zone_id
        choice = QMessageBox.question(
            self,
            "Confirm autotune start",
            f"Start autotune for selected zone {zone}?\n\n"
            f"The controller will use that zone's configured autotune setpoint "
            f"({self._autotune_setpoint_text()}); that setpoint is not the start trigger. "
            "Autotune can change controller output while it runs.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            self.status_label.setText("Autotune start canceled; no command was sent.")
            return
        self._submit(
            {"op": "start_autotune", "zones": [zone]},
            f"Sending autotune start to zone {zone}…",
            trigger_key=f"autotune:{zone}",
        )

    def _stop_autotune(self) -> None:
        zone = self._selected_zone_id
        choice = QMessageBox.question(
            self,
            "Confirm autotune stop",
            f"Stop autotune for selected zone {zone}?\n\n"
            "This writes the controller's autotune disable command. It may change output "
            "behavior; no effect is assumed to be harmless.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            self.status_label.setText("Autotune stop canceled; no command was sent.")
            return
        self._submit(
            {"op": "stop_autotune", "zone": zone},
            f"Sending autotune stop to zone {zone}…",
            trigger_key=f"autotune:{zone}",
        )

    def _autotune_setpoint_text(self) -> str:
        snapshot = self._snapshots.get("telemetry")
        data = dict(snapshot.data) if snapshot is not None and snapshot.data is not None else None
        zone = get_zone(get_zones(data, "telemetry"), self._selected_zone_id)
        value = get_numeric(
            zone,
            "autotune_sp_c",
            "autotune_sp",
            "sp_autotune_c",
            "sp_autotune",
            "SP_autotune",
        )
        return f"{value:g} °C" if value is not None else "unknown"

    def _acknowledge_unknown(self) -> None:
        if not self._unresolved_triggers:
            return
        choice = QMessageBox.question(
            self,
            "Acknowledge unresolved trigger",
            "Clear the on-screen uncertainty warning? This does not determine whether "
            "the command executed, does not verify controller state, and does not retry it.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice == QMessageBox.StandardButton.Yes:
            self._unresolved_triggers.clear()
            self._refresh_uncertainty()
            self.status_label.setText(
                "Uncertainty warning acknowledged locally only; trigger outcome remains unknown."
            )
            self._refresh_action_states()

    def _target(self) -> ZoneKey | None:
        if self._identity is None:
            return None
        return ZoneKey(
            self._identity.key.bus_id,
            self._identity.key.controller_id,
            self._selected_zone_id,
        )

    def _submit(
        self,
        message: Mapping[str, object],
        pending_text: str,
        *,
        coalesce_key: str | None = None,
        allow_read_only: bool = False,
        trigger_key: str | None = None,
        target: ZoneKey | None = None,
        profile_zones: tuple[int, ...] = (),
    ) -> None:
        operation = message.get("op")
        if not isinstance(operation, str):
            raise ValueError("Ramp/Soak command is missing an operation.")
        if self._command_submitter is None:
            self.status_label.setText("No service adapter is configured; request was not sent.")
            return
        if self._pending_operations:
            self.status_label.setText("A Ramp/Soak request is already pending.")
            return
        if not allow_read_only and not self._writes_allowed:
            self.status_label.setText("Service is not writable; no profile change was sent.")
            return
        target = target or self._target()
        if not allow_read_only and target is None:
            self.status_label.setText("Controller identity is unknown; no profile change was sent.")
            return

        self._pending_operations.add(operation)
        self._pending_trigger_key = trigger_key
        self._pending_profile_zones = profile_zones
        self._awaiting_category = (
            "telemetry" if operation == "set_control_mode" else "rampsoak"
        )
        self._request_signature = self._current_signature(self._awaiting_category)
        self._awaiting_readback = operation in {
            "read_rampsoak",
            "set_control_mode",
            "set_num_segments",
            "set_rampsoak_segments",
        }
        self.status_label.setText(pending_text)
        self._refresh_action_states()
        try:
            result = self._command_submitter(
                (message,),
                target=target,
                coalesce_key=coalesce_key,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            self._pending_operations.discard(operation)
            self._pending_trigger_key = None
            self._pending_profile_zones = ()
            self._awaiting_readback = False
            self.status_label.setText(f"Unable to queue request: {exc}")
        else:
            if result not in {"submitted", "coalesced", "mocked"}:
                self._pending_operations.discard(operation)
                self._pending_trigger_key = None
                self._pending_profile_zones = ()
                self._awaiting_readback = False
                self.status_label.setText(f"Service request {result}; no profile command was sent.")
        self._refresh_action_states()

    def _refresh_action_states(self) -> None:
        pending = bool(self._pending_operations)
        self.zone_combo.setEnabled(not pending)
        self.profile_table.setEnabled(not pending)
        self.control_mode.setEnabled(not pending)
        self.segment_count.setEnabled(not pending)
        self.reload_button.setEnabled(not pending and self._command_submitter is not None)
        self.discard_button.setEnabled(not pending and self._is_dirty())
        self.save_button.setEnabled(
            not pending and self._writes_allowed and self._command_submitter is not None
        )
        self.apply_count_button.setEnabled(
            not pending and self._writes_allowed and self._command_submitter is not None
        )
        self.apply_mode_button.setEnabled(
            not pending and self._writes_allowed and self._command_submitter is not None
        )
        trigger_available = (
            not pending
            and self._writes_allowed
            and self._command_submitter is not None
            and not self._unresolved_triggers
        )
        profile_enabled = getattr(self, "_profile_enable_state", "unknown")
        profile_fresh = getattr(self, "_profile_readback_fresh", False)
        profile_known = profile_fresh and profile_enabled in {"ENABLE", "DISABLE"}
        self.start_selected_button.setEnabled(
            trigger_available and not (profile_known and profile_enabled == "ENABLE")
        )
        self.stop_selected_button.setEnabled(
            trigger_available and not (profile_known and profile_enabled == "DISABLE")
        )
        self.start_monitored_button.setEnabled(
            trigger_available and self._has_monitored_zones()
        )
        self.stop_monitored_button.setEnabled(
            trigger_available and self._has_monitored_zones()
        )
        autotune_enabled = getattr(self, "_autotune_enable_state", "unknown")
        autotune_fresh = getattr(self, "_autotune_readback_fresh", False)
        autotune_known = autotune_fresh and autotune_enabled in {"ENABLE", "DISABLE"}
        self.start_autotune_button.setEnabled(
            trigger_available and not (autotune_known and autotune_enabled == "ENABLE")
        )
        self.stop_autotune_button.setEnabled(
            trigger_available and not (autotune_known and autotune_enabled == "DISABLE")
        )
        self.acknowledge_unknown_button.setEnabled(
            not pending and bool(self._unresolved_triggers)
        )

    def _has_monitored_zones(self) -> bool:
        try:
            self._monitored_zone_ids()
        except ValueError:
            return False
        return True
