from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

PY_DIR = Path(__file__).resolve().parents[1] / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from cn616a_service import CN616AService, ServiceConfig  # noqa: E402
from py.qt_gui.adapter import (  # noqa: E402
    AdapterStatus,
    CommandOutcome,
    ControllerIdentity,
    ControllerKey,
    ZoneKey,
    normalize_snapshot,
)
from py.qt_gui.offline import OfflineReplaySession  # noqa: E402
from py.qt_gui.ramp_soak import CONTROL_MODES, ProfileTableModel, RampSoakPage  # noqa: E402
from py.qt_gui.window import QtMainWindow  # noqa: E402


IDENTITY = ControllerIdentity(ControllerKey("ramp-test", 2), 2)


class FakeProfileController:
    last_call: tuple[object, ...] | None = None

    def __init__(self, *, port, unit, register_map_path) -> None:
        self.port = port
        self.unit = unit
        self.register_map_path = register_map_path
        self.last_call = None

    def close(self) -> None:
        return None

    def write_rampsoak_profile(self, zone, segments) -> None:
        self.last_call = ("write_rampsoak_profile", zone, segments)

    def set_num_segments(self, zone, count) -> None:
        self.last_call = ("set_num_segments", zone, count)

    def set_control_mode(self, zone, mode) -> None:
        self.last_call = ("set_control_mode", zone, mode)

    def read_rampsoak_all(self, zones):
        return {
            "zones": {
                str(zone): {
                    "num_segments": 2,
                    "start_profile": "DISABLE",
                    "segments": {},
                }
                for zone in zones
            }
        }


class QtRampSoakTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(["qt-ramp-soak-tests"])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.requests: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.page = RampSoakPage(
            command_submitter=self._capture_request,
        )
        self.addCleanup(self.page.shutdown)
        self.page.set_adapter_status(
            AdapterStatus(
                mode="service",
                connected=True,
                read_only=False,
                provenance="test service",
                message="connected",
                target=IDENTITY,
            ),
            writes_allowed=True,
        )

    def _capture_request(self, messages, **kwargs):
        self.requests.append((tuple(messages), kwargs))
        return "submitted"

    def _snapshot(self, category: str, data: dict[str, object]):
        return normalize_snapshot(
            category,  # type: ignore[arg-type]
            data,
            age_s=0.0,
            stale=False,
            error=None,
            modified_at=1.0,
            read_at=1.0,
            generation=IDENTITY.key.controller_id,
            identity=IDENTITY,
        )

    def _load_zone_data(self, zone1=None, zone2=None) -> None:
        ramp = {
            "ts": "2026-11-01T12:00:00+00:00",
            "unit": 2,
            "port": "ramp-test",
            "rampsoak": {
                "zones": {
                    "1": zone1 or {
                        "num_segments": 2,
                        "segments": {"1": {"sp_c": 0.0, "slope_c_per_min": 2.5, "time_h": 0.0}},
                    },
                    "2": zone2 or {
                        "num_segments": 1,
                        "segments": {"1": {"sp_c": 45.0, "slope_c_per_min": 0.0, "time_h": 1.0}},
                    },
                }
            },
        }
        telemetry = {
            "ts": "2026-11-01T12:00:00+00:00",
            "unit": 2,
            "port": "ramp-test",
            "telemetry": {
                "zones": {
                    "1": {
                        "current_segment_index": 3,
                        "current_segment_state": "RAMPING",
                        "control_mode": "RAMP_SOAK_1_STOP",
                        "loop_status": "STANDARD",
                    }
                }
            },
        }
        service_config = {
            "ts": "2026-11-01T12:00:00+00:00",
            "config": {"zone_names": {"1": "Feed", "2": "Return"}},
        }
        self.page.set_snapshot(self._snapshot("rampsoak", ramp))
        self.page.set_snapshot(self._snapshot("telemetry", telemetry))
        self.page.set_snapshot(self._snapshot("service_config", service_config))

    def test_table_has_20_rows_count_boundaries_and_supported_modes(self) -> None:
        model = ProfileTableModel()
        self.assertEqual(model.rowCount(), 20)
        self.assertEqual(model.columnCount(), 4)
        self.assertEqual(model.headerData(1, Qt.Orientation.Horizontal), "Setpoint (°C)")
        self.assertEqual(self.page.segment_count.minimum(), 0)
        self.assertEqual(self.page.segment_count.maximum(), 20)
        self.assertEqual(
            [self.page.control_mode.itemData(i) for i in range(self.page.control_mode.count())],
            [mode for _label, mode in CONTROL_MODES],
        )
        self.assertFalse(hasattr(self.page, "start_profile_button"))

    def test_serializes_rows_indices_values_zero_and_count_and_mode_payloads(self) -> None:
        self.page.profile_model.setData(self.page.profile_model.index(0, 1), "0")
        self.page.profile_model.setData(self.page.profile_model.index(0, 2), "2.5")
        self.page.profile_model.setData(self.page.profile_model.index(19, 4 - 1), "1.25")
        self.page._save_segments()
        message = self.requests[-1][0][0]
        self.assertEqual(
            message,
            {
                "op": "set_rampsoak_segments",
                "zone": 1,
                "segments": {
                    "1": {"sp_c": 0.0, "slope_c_per_min": 2.5, "time_h": 0.0},
                    "20": {"sp_c": 0.0, "slope_c_per_min": 0.0, "time_h": 1.25},
                },
            },
        )
        self.assertEqual(self.requests[-1][1]["target"], ZoneKey("ramp-test", 2, 1))
        self.page.set_command_outcome(
            CommandOutcome(
                operation="set_rampsoak_segments",
                result="confirmed_success",
                message="accepted",
            )
        )

        self.page.segment_count.setValue(0)
        self.page._apply_count()
        self.assertEqual(
            self.requests[-1][0][0],
            {"op": "set_num_segments", "zone": 1, "count": 0},
        )
        self.page.set_command_outcome(
            CommandOutcome(
                operation="set_num_segments",
                result="confirmed_success",
                message="accepted",
            )
        )
        self.page.segment_count.setValue(20)
        self.page._apply_count()
        self.assertEqual(
            self.requests[-1][0][0],
            {"op": "set_num_segments", "zone": 1, "count": 20},
        )
        self.page.set_command_outcome(
            CommandOutcome(
                operation="set_num_segments",
                result="confirmed_success",
                message="accepted",
            )
        )
        self.page.control_mode.setCurrentIndex(2)
        self.page._apply_mode()
        self.assertEqual(
            self.requests[-1][0][0],
            {"op": "set_control_mode", "zone": 1, "mode": "RAMP_SOAK_2_HOLD"},
        )

    def test_invalid_nonfinite_and_float_overflow_are_not_submitted(self) -> None:
        self.page.profile_model.setData(self.page.profile_model.index(0, 1), "nan")
        self.page._save_segments()
        self.assertEqual(self.requests, [])
        self.assertIn("finite", self.page.status_label.text())

        self.page.profile_model.setData(self.page.profile_model.index(0, 1), "1e100")
        self.page._save_segments()
        self.assertEqual(self.requests, [])
        self.assertIn("float range", self.page.status_label.text())

    def test_dirty_rows_survive_snapshot_refresh_and_explicit_zone_discard(self) -> None:
        self._load_zone_data()
        self.assertEqual(self.page.profile_model.data(self.page.profile_model.index(0, 1)), "0")
        self.assertIn("Current segment: 3", self.page.live_status_label.text())
        self.assertIn("age", self.page.profile_source_label.text())

        self.page.profile_model.setData(self.page.profile_model.index(0, 1), "81")
        self.page.segment_count.setValue(9)
        self.page.control_mode.setCurrentIndex(2)
        self.page.set_snapshot(
            self._snapshot(
                "rampsoak",
                {
                    "ts": "2026-11-01T12:01:00+00:00",
                    "unit": 2,
                    "port": "ramp-test",
                    "rampsoak": {
                        "zones": {
                            "1": {"num_segments": 3, "segments": {"1": {"sp_c": 50.0}}},
                            "2": {"num_segments": 1, "segments": {"1": {"sp_c": 45.0}}},
                        }
                    },
                },
            )
        )
        self.page.set_snapshot(
            self._snapshot(
                "telemetry",
                {
                    "ts": "2026-11-01T12:01:00+00:00",
                    "unit": 2,
                    "port": "ramp-test",
                    "telemetry": {
                        "zones": {
                            "1": {"control_mode": "STANDARD_CONTROL"},
                            "2": {},
                        }
                    },
                },
            )
        )
        self.assertEqual(self.page.profile_model.data(self.page.profile_model.index(0, 1)), "81")
        self.assertEqual(self.page.segment_count.value(), 9)
        self.assertEqual(self.page.control_mode.currentData(), "RAMP_SOAK_2_HOLD")

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.page.zone_combo.setCurrentIndex(self.page.zone_combo.findData(2))
        self.assertEqual(self.page.zone_combo.currentData(), 1)

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.page.zone_combo.setCurrentIndex(self.page.zone_combo.findData(2))
        self.assertEqual(self.page._selected_zone_id, 2)
        self.assertEqual(self.page.profile_model.data(self.page.profile_model.index(0, 1)), "45")
        self.assertEqual(self.page.segment_count.value(), 1)
        self.assertIsNone(self.page.control_mode.currentData())

    def test_reload_prompts_before_discard_and_tab_entry_read_ignores_poll_cadence(self) -> None:
        self.page.profile_model.setData(self.page.profile_model.index(0, 1), "10")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.page._reload_clicked()
        self.assertEqual(self.requests, [])
        self.page.on_tab_selected()
        self.assertEqual(self.requests[-1][0][0], {"op": "read_rampsoak"})
        self.assertEqual(
            self.requests[-1][1]["coalesce_key"],
            "rampsoak-read",
        )

    def test_missing_data_and_failure_unknown_outcomes_are_explicit(self) -> None:
        missing = self._snapshot(
            "rampsoak",
            {"ts": "2026-11-01T12:00:00+00:00", "rampsoak": {"zones": {}}},
        )
        self.page.set_snapshot(missing)
        self.assertEqual(self.page.profile_model.data(self.page.profile_model.index(0, 1)), "")
        self.assertIn("missing for zone 1", self.page.profile_source_label.text())

        self.page._request_read()
        self.page.set_command_outcome(
            CommandOutcome(
                operation="read_rampsoak",
                result="confirmed_failure",
                message="controller read failed",
                generation=1,
            )
        )
        self.assertIn("confirmed_failure", self.page.status_label.text())

        self.page.profile_model.setData(self.page.profile_model.index(0, 1), "15")
        self.page._save_segments()
        self.page.set_command_outcome(
            CommandOutcome(
                operation="set_rampsoak_segments",
                result="unknown",
                message="timeout",
                generation=1,
            )
        )
        self.assertIn("unknown", self.page.status_label.text())
        self.assertTrue(self.page.profile_model.dirty)

    def test_partial_user_workflow_keeps_prior_ack_and_later_failure_distinct(self) -> None:
        self.page.segment_count.setValue(1)
        self.page._apply_count()
        self.page.set_command_outcome(
            CommandOutcome(
                operation="set_num_segments",
                result="confirmed_success",
                message="accepted",
            )
        )
        self.assertIn("acknowledged", self.page.status_label.text())

        self.page.profile_model.setData(self.page.profile_model.index(0, 1), "25")
        self.page._save_segments()
        self.page.set_command_outcome(
            CommandOutcome(
                operation="set_rampsoak_segments",
                result="confirmed_failure",
                message="profile write rejected",
            )
        )
        self.assertIn("confirmed_failure", self.page.status_label.text())
        self.assertTrue(self.page.profile_model.dirty)

    def test_profile_payload_passes_service_handler_with_exact_segment_indices(self) -> None:
        with patch("cn616a_service.CN616A", new=FakeProfileController):
            service = CN616AService(
                port="COM_FAKE",
                unit=2,
                map_path=str(Path(__file__).resolve().parents[1] / "cn616a_register_map.json"),
                out_dir=Path(self.temporary.name),
                tcp_host="127.0.0.1",
                tcp_port=8765,
                cfg=ServiceConfig(),
                verbose=False,
            )
        self.addCleanup(service.ctl.close)

        def service_submitter(messages, **_kwargs):
            for message in messages:
                service.handle_command({"id": "test-profile", **message})
            return "submitted"

        page = RampSoakPage(command_submitter=service_submitter)
        self.addCleanup(page.shutdown)
        page.set_adapter_status(
            AdapterStatus(
                mode="service",
                connected=True,
                read_only=False,
                provenance="fake handler",
                message="connected",
                target=IDENTITY,
            ),
            writes_allowed=True,
        )
        page.profile_model.setData(page.profile_model.index(0, 1), "0")
        page.profile_model.setData(page.profile_model.index(0, 2), "2.5")
        page.profile_model.setData(page.profile_model.index(19, 3), "0.75")
        page._save_segments()
        self.assertEqual(
            service.ctl.last_call,
            (
                "write_rampsoak_profile",
                1,
                {
                    1: {"sp_c": 0.0, "slope_c_per_min": 2.5, "time_h": 0.0},
                    20: {"sp_c": 0.0, "slope_c_per_min": 0.0, "time_h": 0.75},
                },
            ),
        )

    def test_offline_tab_read_and_profile_write_are_simulated_with_fake_readback(self) -> None:
        session = OfflineReplaySession()
        window = QtMainWindow(session)
        try:
            window.tabs.setCurrentWidget(window.ramp_soak)
            self.assertEqual(window.command_outcomes[-1].operation, "read_rampsoak")
            self.assertIn("Profile snapshot refreshed", window.ramp_soak.status_label.text())
            window.ramp_soak.profile_model.setData(
                window.ramp_soak.profile_model.index(0, 1),
                "0",
            )
            window.ramp_soak.profile_model.setData(
                window.ramp_soak.profile_model.index(0, 2),
                "4.5",
            )
            window.ramp_soak._save_segments()
            ramp_snapshot = session.snapshots()["rampsoak"]
            zone = ramp_snapshot.zone_values[1]
            segments = zone["segments"]
            self.assertEqual(segments["1"]["sp_c"], 0.0)
            self.assertEqual(segments["1"]["slope_c_per_min"], 4.5)
            self.assertTrue(any("SIMULATED ONLY" in outcome.message for outcome in session.outcomes()))
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
