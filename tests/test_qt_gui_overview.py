from __future__ import annotations

import json
import os
import time
import unittest
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch


os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QObject, Qt, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QLineEdit, QSpinBox  # noqa: E402

from py.gui.state_values import get_numeric, get_zone, get_zones  # noqa: E402
from py.qt_gui.adapter import (  # noqa: E402
    AdapterSnapshot,
    AdapterStatus,
    CommandOutcome,
    ControllerIdentity,
    ControllerKey,
    SnapshotCategory,
    SubmissionStatus,
    ZoneKey,
    normalize_snapshot,
)
from py.qt_gui.offline import OfflineReplaySession  # noqa: E402
from py.qt_gui.overview import (  # noqa: E402
    SORT_ROLE,
    ZONE_KEY_ROLE,
    Column,
    OverviewPage,
)
from py.qt_gui.window import QtMainWindow  # noqa: E402


GENERATION = 101
IDENTITY_A = ControllerIdentity(ControllerKey("TEST_BUS_A", 3), 3)
IDENTITY_B = ControllerIdentity(ControllerKey("TEST_BUS_B", 4), 4)


def _status(identity: ControllerIdentity | None = IDENTITY_A) -> AdapterStatus:
    return AdapterStatus(
        mode="service",
        connected=True,
        read_only=True,
        provenance="test-owned cached data",
        message="TEST SERVICE",
        target=identity,
        generation=GENERATION,
        controller_connected=True,
        identity_warning="Identity unverified" if identity is None else None,
    )


def _snapshot(
    category: SnapshotCategory,
    content: dict[str, Any] | None,
    *,
    identity: ControllerIdentity | None = IDENTITY_A,
    timestamp: datetime | None = None,
    include_timestamp: bool = True,
    modified_at: float | None = None,
    stale: bool = False,
    error: str | None = None,
    generation: int = GENERATION,
) -> AdapterSnapshot:
    payload: dict[str, Any] | None = None
    if content is not None:
        section = "config" if category == "service_config" else category
        payload = {section: content}
        if include_timestamp:
            payload["ts"] = (timestamp or datetime.now(timezone.utc)).isoformat()
        if identity is not None:
            payload.update(
                bus_id=identity.key.bus_id,
                controller_id=identity.key.controller_id,
                modbus_address=identity.modbus_address,
            )
    return normalize_snapshot(
        category,
        payload,
        age_s=0.0,
        stale=stale,
        error=error,
        modified_at=modified_at,
        read_at=None,
        generation=generation,
        identity=identity,
    )


class CachedTestAdapter(QObject):
    status_changed = Signal(object)
    snapshot_updated = Signal(object)

    def __init__(self, snapshots: dict[str, AdapterSnapshot]) -> None:
        super().__init__()
        self.generation = GENERATION
        self.status = _status()
        self.cached_snapshots = snapshots
        self.snapshot_calls = 0
        self.submissions: list[Sequence[Mapping[str, object]]] = []
        self.closed = False

    def snapshots(self) -> dict[str, AdapterSnapshot]:
        self.snapshot_calls += 1
        return self.cached_snapshots

    def outcomes(self) -> Sequence[CommandOutcome]:
        return ()

    def submit(
        self,
        messages: Sequence[Mapping[str, object]],
        *,
        target: ZoneKey | None = None,
        coalesce_key: str | None = None,
    ) -> SubmissionStatus:
        self.submissions.append(messages)
        return "rejected"

    def is_current_generation(self, generation: int) -> bool:
        return not self.closed and generation == self.generation

    def shutdown(self) -> None:
        self.closed = True


class QtOverviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(["qt-overview-tests"])

    def setUp(self) -> None:
        for target in ("socket.create_connection", "socket.socket.connect", "serial.Serial"):
            guard = patch(target, side_effect=AssertionError(f"{target} is forbidden in overview tests"))
            guard.start()
            self.addCleanup(guard.stop)

    def _page(self, *, stale_after_s: float = 5.0) -> OverviewPage:
        page = OverviewPage(stale_after_s=stale_after_s)
        self.addCleanup(page.shutdown)
        return page

    def _writable_page(self, submitter) -> OverviewPage:
        page = OverviewPage(command_submitter=submitter)
        self.addCleanup(page.shutdown)
        page.set_status(
            AdapterStatus(
                mode="service",
                connected=True,
                read_only=False,
                provenance="test-owned cached data",
                message="TEST SERVICE",
                target=IDENTITY_A,
                generation=GENERATION,
                controller_connected=True,
            )
        )
        page.set_snapshot(
            _snapshot(
                "telemetry",
                {
                    "zones": {
                        "1": {
                            "sp_abs_c": 0.0,
                            "autotune_sp_c": 0.0,
                            "control_method": "PID_CONTROL",
                            "control_mode": "RAMP_SOAK",
                        },
                        "2": {
                            "sp_abs_c": -5.0,
                            "autotune_sp_c": 0.0,
                            "control_method": "ON_OFF_CONTROL",
                        },
                    }
                },
            )
        )
        return page

    def test_model_roles_and_detail_text_preserve_aliases_zero_and_missing_values(self) -> None:
        page = self._page()
        page.set_status(_status())
        page.set_snapshot(
            _snapshot(
                "service_config",
                {"zone_names": {"1": "Upper_Rack", "2": "Bake <zone>"}},
            )
        )
        page.set_snapshot(
            _snapshot(
                "config",
                {
                    "zones": {
                        "1": {
                            "sensor_status": "FAULT_SENSOR",
                            "alarms": {
                                "alarm1": {"status": "ACTIVE"},
                                "alarm2": {"status": "CLEAR"},
                            },
                            "pid_parameters": {"deadband": 0.0, "cycle_time_s": 0.0},
                        }
                    }
                },
            )
        )
        page.set_snapshot(
            _snapshot(
                "analysis",
                {
                    "zones": {
                        "1": {
                            "mae": 0.0,
                            "equilibrium": False,
                            "point_count": 0,
                            "equilibrium_threshold_c": 0.0,
                            "window_s": 0.0,
                        },
                        "2": {"equilibrium": "unknown"},
                    }
                },
            )
        )
        page.set_snapshot(
            _snapshot(
                "telemetry",
                {
                    "zones": {
                        "z1": {
                            "PV": 0.0,
                            "SP": -12.5,
                            "output_pct": 0,
                            "kp": 0,
                            "ki": -0.25,
                            "derivative_gain": 1.5,
                            "autotune_enabled": False,
                            "SP_autotune": 0,
                            "sensor_type": "K_TYPE",
                            "sensor_subtype": 4,
                            "control_method": "PID_CONTROL",
                            "control_mode": "STANDARD_CONTROL",
                            "loop_status": "ACTIVE",
                            "current_segment_index": 0,
                            "current_segment_state": "IDLE",
                            "ramp_soak_remaining": -0.5,
                        },
                        "Z2": {
                            "process_value": -4.5,
                            "setpoint": -5.0,
                            "OUT": -1.5,
                            "mode": "MANUAL",
                            "status": "OFF",
                        },
                        "3": {"pv_c": float("nan"), "sp_abs_c": False, "out_pct": None},
                    }
                },
            )
        )

        model = page.model
        self.assertEqual(model.rowCount(), 6)
        self.assertEqual(model.headerData(Column.PV, Qt.Orientation.Horizontal), "PV (°C)")
        self.assertEqual(model.headerData(Column.SETPOINT, Qt.Orientation.Horizontal), "SP (°C)")
        self.assertEqual(model.headerData(Column.OUTPUT, Qt.Orientation.Horizontal), "Output (%)")
        self.assertEqual(model.index(0, Column.ZONE).data(), "1 / Upper_Rack")
        self.assertEqual(model.index(1, Column.ZONE).data(), "2 / Bake <zone>")
        self.assertEqual(model.index(0, Column.PV).data(), "0.00")
        self.assertEqual(model.index(0, Column.SETPOINT).data(), "-12.50")
        self.assertEqual(model.index(0, Column.OUTPUT).data(), "0.0")
        self.assertEqual(model.index(0, Column.ERROR).data(), "0.000")
        self.assertEqual(model.index(0, Column.EQUILIBRIUM).data(), "No")
        self.assertEqual(model.index(2, Column.PV).data(), "N/A")
        self.assertEqual(model.index(2, Column.SETPOINT).data(), "N/A")
        self.assertIsNone(model.index(2, Column.PV).data(Qt.ItemDataRole.ForegroundRole))
        self.assertEqual(model.index(0, Column.PV).data(Qt.ItemDataRole.ForegroundRole).name(), "#1264bd")
        self.assertEqual(model.index(0, Column.PV).data(Qt.ItemDataRole.AccessibleTextRole), "PV (°C): 0.00")
        self.assertEqual(model.index(0, Column.PV).data(SORT_ROLE), 0.0)
        self.assertEqual(model.index(0, Column.ZONE).data(ZONE_KEY_ROLE), ZoneKey("TEST_BUS_A", 3, 1))
        self.assertFalse(model.flags(model.index(0, Column.PV)) & Qt.ItemFlag.ItemIsEditable)

        self.assertEqual(page.selected_zone_id, 1)
        self.assertEqual(page.focus_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn("Upper_Rack", page.focus_label.text())
        self.assertEqual(
            page.measurement_label.text(), "PV: 0.00 °C    SP: -12.50 °C    Output: 0.0 %"
        )
        self.assertEqual(page.detail_labels["sensor"].text(), "K TYPE · subtype 4")
        self.assertEqual(page.detail_labels["sensor_status"].text(), "FAULT SENSOR")
        self.assertEqual(page.detail_labels["alarms"].text(), "ACTIVE / CLEAR")
        self.assertEqual(page.detail_labels["autotune"].text(), "Off · SP 0.0 °C")
        self.assertEqual(page.detail_labels["pid"].text(), "0.0000 / -0.2500 / 1.5000")
        self.assertEqual(page.detail_labels["timing"].text(), "0.00 °C / 0.0 s")
        self.assertIn("Remaining: -0.5 s", page.detail_labels["segment"].text())
        self.assertIn("Equilibrium: No", page.detail_labels["analysis"].text())
        self.assertIn("MAE: 0.000 °C", page.detail_labels["analysis"].text())
        self.assertIn("samples: 0", page.detail_labels["analysis"].text())

        page._select_zone(2)
        self.assertEqual(page.measurement_label.text(), "PV: -4.50 °C    SP: -5.00 °C    Output: -1.5 %")
        self.assertEqual(page.detail_labels["pid"].text(), "N/A / N/A / N/A")
        self.assertIn("Method: MANUAL", page.detail_labels["control"].text())
        self.assertEqual(model.index(1, Column.METHOD_LOOP).data(), "MANUAL / OFF")
        self.assertEqual(model.index(1, Column.EQUILIBRIUM).data(), "N/A")
        self.assertIsNone(model.index(1, Column.EQUILIBRIUM).data(SORT_ROLE))

    def test_all_replay_samples_match_existing_state_helpers(self) -> None:
        page = self._page()
        with OfflineReplaySession() as session:
            page.set_status(session.status)
            page.set_snapshot(session.snapshots()["service_config"])
            telemetry_records = [
                json.loads(line)
                for line in session.paths["telemetry_log"].read_text(encoding="utf-8").splitlines()
            ]
            analysis_records = [
                json.loads(line)
                for line in session.paths["analysis_log"].read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(telemetry_records), 16)
            self.assertEqual(len(analysis_records), 16)
            for telemetry, analysis in zip(telemetry_records, analysis_records):
                normalized = {}
                for category, data in (("telemetry", telemetry), ("analysis", analysis)):
                    normalized[category] = normalize_snapshot(
                        category,
                        data,
                        age_s=0.0,
                        stale=False,
                        error=None,
                        modified_at=None,
                        read_at=None,
                        generation=session.generation,
                        identity=session.status.target,
                    )
                page.set_snapshot(normalized["telemetry"])
                page.set_snapshot(normalized["analysis"])
                telemetry_zones = get_zones(telemetry, "telemetry")
                analysis_zones = get_zones(analysis, "analysis")
                for zone_id in range(1, 7):
                    zone = get_zone(telemetry_zones, zone_id)
                    result = get_zone(analysis_zones, zone_id)
                    fields = (
                        (Column.PV, get_numeric(zone, "pv_c", "pv", "PV", "process_value_c", "process_value")),
                        (
                            Column.SETPOINT,
                            get_numeric(
                                zone, "sp_abs_c", "sp_abs", "sp_c", "sp", "SP_abs", "SP", "setpoint_c", "setpoint"
                            ),
                        ),
                        (Column.OUTPUT, get_numeric(zone, "out_pct", "output_pct", "output", "OUT")),
                        (Column.ERROR, get_numeric(result, "avg_abs_error_c", "mae_c", "mae")),
                    )
                    for column, expected in fields:
                        self.assertEqual(page.model.index(zone_id - 1, column).data(SORT_ROLE), expected)
                        decimals = 1 if column == Column.OUTPUT else 3 if column == Column.ERROR else 2
                        expected_text = f"{expected:.{decimals}f}" if expected is not None else "N/A"
                        self.assertEqual(page.model.index(zone_id - 1, column).data(), expected_text)
                    row = page.model.row_for_zone(zone_id)
                    for canonical, aliases in (
                        ("p_gain", ("p_gain", "kp", "proportional_gain")),
                        ("i_gain", ("i_gain", "ki", "integral_gain")),
                        ("d_gain", ("d_gain", "kd", "derivative_gain")),
                    ):
                        self.assertEqual(row.telemetry[canonical], get_numeric(zone, *aliases))
            self.assertIn("Mock-derived", page.detail_labels["analysis"].text())
            self.assertIn("OFFLINE REPLAY", page.health_label.text())

    def test_sorting_and_updates_keep_the_selected_zone_key_without_model_resets(self) -> None:
        page = self._page()
        page.set_status(_status())
        page.set_snapshot(
            _snapshot("telemetry", {"zones": {str(zone): {"pv_c": zone} for zone in range(1, 7)}})
        )
        page.set_snapshot(
            _snapshot(
                "analysis",
                {"zones": {"1": {"equilibrium": True}, "2": {"equilibrium": "unknown"}, "3": {"equilibrium": False}}},
            )
        )
        page._select_zone(4)
        selected = ZoneKey("TEST_BUS_A", 3, 4)
        self.assertEqual(page.selected_zone_key, selected)
        resets: list[bool] = []
        page.model.modelReset.connect(lambda: resets.append(True))

        page.zone_table.sortByColumn(Column.PV, Qt.SortOrder.DescendingOrder)
        self.app.processEvents()
        self.assertEqual(page.selected_zone_key, selected)
        self.assertIn("PV: 4.00 °C", page.measurement_label.text())
        page.set_snapshot(
            _snapshot(
                "telemetry",
                {"zones": {str(zone): {"pv_c": 99 if zone == 4 else zone} for zone in range(1, 7)}},
            )
        )
        self.app.processEvents()
        self.assertEqual(page.selected_zone_key, selected)
        self.assertIn("PV: 99.00 °C", page.measurement_label.text())
        self.assertEqual(page.zone_table.model().index(0, Column.ZONE).data(ZONE_KEY_ROLE), selected)

        page.zone_table.sortByColumn(Column.EQUILIBRIUM, Qt.SortOrder.AscendingOrder)
        self.app.processEvents()
        self.assertEqual(page.selected_zone_key, selected)
        self.assertEqual(resets, [])

    def test_identity_changes_withhold_foreign_sources_and_require_reselection(self) -> None:
        page = self._page()
        page.set_status(_status())
        page.set_snapshot(_snapshot("telemetry", {"zones": {"1": {"pv_c": 10}}}))
        page.set_snapshot(_snapshot("config", {"zones": {"1": {"sensor_status": "FOREIGN"}}}, identity=IDENTITY_B))
        page.set_snapshot(_snapshot("analysis", {"zones": {"1": {"mae": 999}}}, identity=IDENTITY_B))
        page.set_snapshot(_snapshot("service_config", {"zone_names": {"1": "Foreign name"}}, identity=IDENTITY_B))
        self.assertEqual(page.model.index(0, Column.ZONE).data(), "1 / Zone 1")
        self.assertEqual(page.model.index(0, Column.PV).data(), "10.00")
        self.assertEqual(page.model.index(0, Column.ERROR).data(), "N/A")
        self.assertEqual(page.detail_labels["sensor_status"].text(), "N/A")
        for name in ("Analysis", "Config", "Names"):
            self.assertIn(f"{name}: IDENTITY MISMATCH — withheld", page.source_label.text())

        page.set_status(_status(IDENTITY_B))
        self.assertIsNone(page.selected_zone_id)
        self.assertIn("select a zone again", page.focus_label.text())
        page.set_snapshot(_snapshot("telemetry", {"zones": {"1": {"pv_c": 20}}}, identity=IDENTITY_B))
        self.assertIsNone(page.selected_zone_id)
        page._select_zone(1)
        self.assertEqual(page.selected_zone_key, ZoneKey("TEST_BUS_B", 4, 1))
        self.assertEqual(page.model.index(0, Column.ZONE).data(), "1 / Foreign name")
        self.assertIn("PV: 20.00 °C", page.measurement_label.text())

    def test_unknown_identity_and_temporary_errors_do_not_retarget_the_selection(self) -> None:
        page = self._page()
        page.set_status(_status(None))
        page.set_snapshot(_snapshot("telemetry", {"zones": {"4": {"pv_c": 12}}}))
        page._select_zone(4)
        page.set_snapshot(_snapshot("telemetry", None, identity=None, error="synthetic read failure"))
        self.assertEqual(page.selected_zone_id, 4)
        self.assertIn("ERROR — synthetic read failure", page.source_label.text())
        self.assertIn("PV: N/A", page.measurement_label.text())

        page.set_snapshot(_snapshot("telemetry", {"zones": {"4": {"pv_c": 13}}}))
        self.assertEqual(page.selected_zone_key, ZoneKey("TEST_BUS_A", 3, 4))
        self.assertIn("PV: 13.00 °C", page.measurement_label.text())
        page.set_snapshot(_snapshot("telemetry", {"zones": {"4": {"pv_c": 14}}}, identity=None))
        self.assertEqual(page.selected_zone_id, 4)
        self.assertIsNone(page.selected_zone_key)
        self.assertIn("Identity unknown / read-only", page.focus_label.text())
        self.assertIn("PV: 14.00 °C", page.measurement_label.text())
        self.assertIn("Identity unverified", page.source_label.text())

        page.set_snapshot(_snapshot("telemetry", {"zones": {"4": {"pv_c": 15}}}, identity=IDENTITY_B))
        self.assertIsNone(page.selected_zone_id)
        self.assertIn("select a zone again", page.focus_label.text())

    def test_initially_unknown_telemetry_requires_reselection_when_identity_arrives(self) -> None:
        page = self._page()
        page.set_status(_status(None))
        page.set_snapshot(_snapshot("telemetry", {"zones": {"2": {"pv_c": 0}}}, identity=None))
        page._select_zone(2)
        self.assertIsNone(page.selected_zone_key)
        self.assertIn("PV: 0.00 °C", page.measurement_label.text())
        self.assertIn("identity unknown", page.identity_label.text().lower())

        page.set_snapshot(_snapshot("telemetry", {"zones": {"2": {"pv_c": 1}}}))
        self.assertIsNone(page.selected_zone_id)
        self.assertIn("select a zone again", page.focus_label.text())
        page._select_zone(2)
        self.assertEqual(page.selected_zone_key, ZoneKey("TEST_BUS_A", 3, 2))

    def test_freshness_ages_from_cached_source_time_without_changing_rows(self) -> None:
        page = self._page(stale_after_s=5.0)
        page.set_status(_status())
        source_time = datetime.now(timezone.utc)
        page.set_snapshot(_snapshot("telemetry", {"zones": {"1": {"pv_c": 10}}}, timestamp=source_time))
        self.assertIn("Telemetry: CURRENT", page.source_label.text())
        changes: list[bool] = []
        page.model.dataChanged.connect(lambda *_args: changes.append(True))
        with patch("py.qt_gui.adapter.datetime") as clock:
            clock.now.return_value = source_time + timedelta(seconds=5)
            page._age_timer.timeout.emit()
            self.assertIn("Telemetry: CURRENT", page.source_label.text())
            clock.now.return_value = source_time + timedelta(seconds=5, milliseconds=1)
            page._age_timer.timeout.emit()
            self.assertIn("Telemetry: STALE", page.source_label.text())
            clock.now.return_value = source_time + timedelta(seconds=12)
            page._age_timer.timeout.emit()
        self.assertIn("Telemetry: STALE", page.source_label.text())
        self.assertIn("12 s old", page.source_label.text())
        self.assertEqual(page.model.index(0, Column.PV).data(), "10.00")
        self.assertEqual(changes, [])

        page.set_snapshot(
            _snapshot(
                "telemetry",
                {"zones": {"1": {"pv_c": 10}}},
                include_timestamp=False,
                modified_at=time.time() - 1,
            )
        )
        self.assertIn("Telemetry: TIME UNKNOWN", page.source_label.text())
        self.assertIn("source time unavailable", page.source_label.text())
        self.assertIn("file 1 s old", page.source_label.text())
        page.set_snapshot(_snapshot("telemetry", None, identity=None, error="invalid test JSON"))
        self.assertIn("Telemetry: ERROR — invalid test JSON", page.source_label.text())

    def test_dirty_readbacks_are_scoped_by_target_and_field(self) -> None:
        page = self._page()
        first = ZoneKey("TEST_BUS_A", 3, 1)
        other_zone = ZoneKey("TEST_BUS_A", 3, 2)
        other_controller = ZoneKey("TEST_BUS_B", 4, 1)
        applied: list[str] = []
        page.dirty_readbacks.set_dirty(first, "setpoint")
        self.assertFalse(page.dirty_readbacks.apply_readback(first, "setpoint", lambda: applied.append("dirty")))
        self.assertTrue(page.dirty_readbacks.apply_readback(first, "gain", lambda: applied.append("gain")))
        self.assertTrue(page.dirty_readbacks.apply_readback(other_zone, "setpoint", lambda: applied.append("zone")))
        self.assertTrue(
            page.dirty_readbacks.apply_readback(other_controller, "setpoint", lambda: applied.append("controller"))
        )
        page.dirty_readbacks.set_dirty(first, "setpoint", False)
        self.assertTrue(page.dirty_readbacks.apply_readback(first, "setpoint", lambda: applied.append("clean")))
        self.assertEqual(applied, ["gain", "zone", "controller", "clean"])

    def test_zone_update_controls_are_target_bound_and_preserve_per_zone_drafts(self) -> None:
        submissions = []

        def submitter(messages, target):
            submissions.append((messages, target))
            return "submitted"

        page = self._writable_page(submitter)
        self.assertEqual(page.selected_zone_key, ZoneKey("TEST_BUS_A", 3, 1))
        self.assertEqual(page.method_combo.currentData(), "PID_CONTROL")
        self.assertTrue(page.sp_abs_entry.isEnabled())
        self.assertTrue(page.apply_controls_button.isEnabled() is False)

        page.sp_abs_entry.setText("-12.5")
        page.method_combo.setCurrentIndex(page.method_combo.findData("ON_OFF_CONTROL"))
        page.autotune_sp_entry.setText("0")
        page._control_form_edited()
        self.assertTrue(page.apply_controls_button.isEnabled())
        page.set_snapshot(
            _snapshot(
                "telemetry",
                {
                    "zones": {
                        "1": {"sp_abs_c": 99.0, "autotune_sp_c": 99.0, "control_method": "PID_CONTROL"},
                        "2": {"sp_abs_c": -5.0, "autotune_sp_c": 0.0, "control_method": "ON_OFF_CONTROL"},
                    }
                },
            )
        )
        self.assertEqual(page.sp_abs_entry.text(), "-12.5")
        self.assertEqual(page.autotune_sp_entry.text(), "0")
        page._select_zone(2)
        self.assertEqual(page.selected_zone_key, ZoneKey("TEST_BUS_A", 3, 2))
        self.assertEqual(page.sp_abs_entry.text(), "-5")
        self.assertEqual(page.method_combo.currentData(), "ON_OFF_CONTROL")

        page._select_zone(1)
        self.assertEqual(page.sp_abs_entry.text(), "-12.5")
        self.assertEqual(page.method_combo.currentData(), "ON_OFF_CONTROL")
        page.discard_controls_button.click()
        self.assertNotIn(ZoneKey("TEST_BUS_A", 3, 1), page._control_drafts)
        self.assertEqual(page.sp_abs_entry.text(), "99")
        self.assertEqual(page.method_combo.currentData(), "PID_CONTROL")
        page.sp_abs_entry.setText("-12.5")
        page.method_combo.setCurrentIndex(page.method_combo.findData("ON_OFF_CONTROL"))
        page.autotune_sp_entry.setText("0")
        page._control_form_edited()
        page.apply_controls_button.click()
        self.assertEqual(len(submissions), 1)
        messages, target = submissions[0]
        self.assertEqual(target, ZoneKey("TEST_BUS_A", 3, 1))
        self.assertEqual(
            list(messages),
            [
                {"op": "set_sp_abs", "zone": 1, "value_c": -12.5},
                {"op": "set_control_method", "zone": 1, "method": "ON_OFF_CONTROL"},
                {"op": "set_autotune_setpoint", "zone": 1, "value_c": 0.0},
            ],
        )
        self.assertTrue(all("unit" not in message for message in messages))
        self.assertEqual(page._pending_target, target)
        self.assertFalse(page.sp_abs_entry.isEnabled())
        self.assertFalse(page.apply_controls_button.isEnabled())

    def test_zone_update_rejects_nonfinite_values_and_disables_unverified_targets(self) -> None:
        submissions = []
        page = self._writable_page(
            lambda messages, target: submissions.append((messages, target)) or "submitted"
        )
        for invalid in ("nan", "inf", "-inf"):
            page.sp_abs_entry.setText(invalid)
            page._control_form_edited()
            page._apply_zone_updates()
            self.assertIn("No command was sent", page.control_status_label.text())
        self.assertEqual(submissions, [])

        page.set_status(_status())
        self.assertFalse(page.sp_abs_entry.isEnabled())
        self.assertFalse(page.apply_controls_button.isEnabled())
        page._apply_zone_updates()
        self.assertEqual(submissions, [])

    def test_zone_update_outcomes_require_fresh_matching_readback(self) -> None:
        submissions = []
        page = self._writable_page(
            lambda messages, target: submissions.append((messages, target)) or "submitted"
        )
        page.sp_abs_entry.setText("20")
        page.autotune_sp_entry.setText("-1")
        page._control_form_edited()
        page._apply_zone_updates()
        target = ZoneKey("TEST_BUS_A", 3, 1)
        acknowledged_at = datetime.now(timezone.utc)
        for operation in ("set_sp_abs", "set_control_method", "set_autotune_setpoint"):
            page.set_command_outcome(
                CommandOutcome(
                    operation=operation,
                    result="confirmed_success",
                    message="service acknowledged",
                    target=target,
                    generation=GENERATION,
                    endpoint="test-service",
                    created_at=acknowledged_at,
                )
            )
        self.assertIn("awaiting fresh controller readback", page.control_status_label.text())
        self.assertIn(target, page._control_drafts)
        self.assertFalse(page.apply_controls_button.isEnabled())

        page.set_snapshot(
            _snapshot(
                "telemetry",
                {
                    "zones": {
                        "1": {
                            "sp_abs_c": 20.0,
                            "autotune_sp_c": -1.0,
                            "control_method": "PID_CONTROL",
                        }
                    }
                },
                timestamp=acknowledged_at + timedelta(seconds=2),
            )
        )
        self.assertNotIn(target, page._control_drafts)
        self.assertIn("Fresh controller readback matches", page.control_status_label.text())

        self.assertEqual(len(submissions), 1)

    def test_zone_update_mismatch_retains_draft_and_requires_explicit_next_action(self) -> None:
        submissions = []
        page = self._writable_page(
            lambda messages, target: submissions.append((messages, target)) or "submitted"
        )
        page.sp_abs_entry.setText("20")
        page.autotune_sp_entry.setText("-1")
        page._control_form_edited()
        page._apply_zone_updates()
        target = ZoneKey("TEST_BUS_A", 3, 1)
        acknowledged_at = datetime.now(timezone.utc)
        for operation in ("set_sp_abs", "set_control_method", "set_autotune_setpoint"):
            page.set_command_outcome(
                CommandOutcome(
                    operation=operation,
                    result="confirmed_success",
                    message="service acknowledged",
                    target=target,
                    generation=GENERATION,
                    endpoint="test-service",
                    created_at=acknowledged_at,
                )
            )
        page.set_snapshot(
            _snapshot(
                "telemetry",
                {
                    "zones": {
                        "1": {
                            "sp_abs_c": 19.0,
                            "autotune_sp_c": -1.0,
                            "control_method": "PID_CONTROL",
                        }
                    }
                },
                timestamp=acknowledged_at + timedelta(seconds=1),
            )
        )
        self.assertIn(target, page._control_drafts)
        self.assertIsNone(page._awaiting_readback)
        self.assertTrue(page.apply_controls_button.isEnabled())
        self.assertIn("readback differs", page.control_status_label.text())
        self.assertEqual(len(submissions), 1)

    def test_tab_and_row_selection_are_local_and_hidden_overview_keeps_updating(self) -> None:
        initial = _snapshot("telemetry", {"zones": {"1": {"pv_c": 1}, "2": {"pv_c": 2}}})
        adapter = CachedTestAdapter({"telemetry": initial})
        window = QtMainWindow(adapter)
        self.addCleanup(window.close)
        window.show()
        self.app.processEvents()
        self.assertEqual(adapter.snapshot_calls, 1)
        self.assertEqual(window.overview.model.index(0, Column.PV).data(), "1.00")
        self.assertEqual(window.overview.findChildren(QSpinBox), [])
        self.assertEqual(len(window.overview.findChildren(QLineEdit)), 5)
        self.assertFalse(window.overview.sp_abs_entry.isEnabled())
        self.assertFalse(window.overview.autotune_sp_entry.isEnabled())

        with patch.object(adapter, "snapshots", side_effect=AssertionError("synchronous reread")), patch(
            "pathlib.Path.open", side_effect=AssertionError("synchronous file read")
        ):
            window.tabs.setCurrentIndex(1)
            updated = _snapshot("telemetry", {"zones": {"1": {"pv_c": 7}, "2": {"pv_c": 8}}})
            adapter.snapshot_updated.emit(updated)
            self.app.processEvents()
            self.assertEqual(window.overview.model.index(0, Column.PV).data(), "7.00")
            window.overview._age_timer.timeout.emit()
            window.tabs.setCurrentIndex(0)
            window.overview._select_zone(2)
            window.tabs.setCurrentIndex(3)
            stale = _snapshot("telemetry", {"zones": {"1": {"pv_c": 999}}}, generation=GENERATION - 1)
            adapter.snapshot_updated.emit(stale)
            self.app.processEvents()
        self.assertEqual(window.overview.model.index(0, Column.PV).data(), "7.00")
        self.assertEqual(window.overview.selected_zone_key, ZoneKey("TEST_BUS_A", 3, 2))
        self.assertTrue(adapter.submissions)
        self.assertTrue(
            all(
                tuple(message.get("op") for message in messages) == ("get_run_status",)
                for messages in adapter.submissions
            )
        )
        window.close()
        self.assertTrue(adapter.closed)
        self.assertFalse(window.overview._age_timer.isActive())


if __name__ == "__main__":
    unittest.main()
