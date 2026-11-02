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
        else:
            self._awaiting_readback = False
            self.status_label.setText(f"{operation}: {outcome.result} — {outcome.message}")
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
        mode = get_field(telemetry_zone, "control_mode", "control_method", "mode")
        if force or not self._mode_dirty:
            mode_index = self.control_mode.findData(str(mode).strip().upper()) if mode is not None else -1
            self._updating = True
            try:
                self.control_mode.setCurrentIndex(mode_index)
            finally:
                self._updating = False
        self._update_live_status()
        self._refresh_action_states()

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
        target = self._target()
        if not allow_read_only and target is None:
            self.status_label.setText("Controller identity is unknown; no profile change was sent.")
            return

        self._pending_operations.add(operation)
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
            self._awaiting_readback = False
            self.status_label.setText(f"Unable to queue request: {exc}")
        else:
            if result not in {"submitted", "coalesced", "mocked"}:
                self._pending_operations.discard(operation)
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
