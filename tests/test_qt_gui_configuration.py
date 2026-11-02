from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from py.gui.config_values import parse_retention_seconds  # noqa: E402
from py.qt_gui.adapter import (  # noqa: E402
    AdapterSnapshot,
    AdapterStatus,
    CommandOutcome,
    ControllerIdentity,
    ControllerKey,
    normalize_snapshot,
)
from py.qt_gui.configuration import ConfigurationPage  # noqa: E402
from py.qt_gui.offline import OfflineReplaySession  # noqa: E402
from py.qt_gui.window import QtMainWindow  # noqa: E402


IDENTITY = ControllerIdentity(ControllerKey("config-test", 2), 2)


class QtGuiConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(["qt-configuration-tests"])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.requests: list[tuple[object, ...]] = []
        self.page = ConfigurationPage(
            logs_dir=Path(self.temporary.name),
            command_submitter=lambda messages: self.requests.append(messages) or "submitted",
        )
        self.addCleanup(self.page.shutdown)

    def _service_snapshot(self, config: dict[str, object]) -> AdapterSnapshot:
        return normalize_snapshot(
            "service_config",
            {"config": config, "ts": "2026-11-01T12:00:00+00:00"},
            age_s=0,
            stale=False,
            error=None,
            modified_at=None,
            read_at=None,
            generation=1,
        )

    def test_patch_preserves_v1_settings_keys_and_nested_and_legacy_viewer_values(self) -> None:
        self.page.set_snapshot(
            self._service_snapshot(
                {
                    "viewer": {"history_hours": 0.5, "line_width": 2.5},
                    "viewer_history_hours": 0.5,
                    "zone_names": {"1": "Feed"},
                }
            )
        )
        self.page._line_edits["telemetry_hz"].setText("3.5")
        self.page._combos["zones_mode"].setCurrentText("list")
        self.page._line_edits["zones_list"].setText("1, 3, 5")
        self.page._line_edits["telemetry_retention"].setText("1 week")
        self.page._line_edits["viewer_history_seconds"].setText("900")
        self.page._checks["viewer_show_sp_abs"].setChecked(False)
        patch = self.page._build_patch()

        self.assertEqual(patch["telemetry_hz"], 3.5)
        self.assertEqual(patch["zones_mode"], "list")
        self.assertEqual(patch["zones_list"], [1, 3, 5])
        self.assertEqual(patch["zone_names"]["1"], "Feed")
        self.assertEqual(patch["telemetry_retention_s"], 604800)
        self.assertEqual(patch["viewer"]["history_hours"], 0.25)
        self.assertEqual(patch["viewer_history_hours"], 0.25)
        self.assertFalse(patch["viewer"]["show_sp_abs"])
        self.assertFalse(patch["viewer_show_sp_abs"])
        self.assertEqual(patch["last_serial_params"]["parity"], "N")
        self.assertEqual(patch["last_tcp_port"], 8765)

    def test_invalid_input_is_visible_and_never_submitted(self) -> None:
        self.page._line_edits["gui_refresh_hz"].setText("nan")
        self.page._apply_settings()
        self.assertEqual(self.requests, [])
        self.assertIn("finite", self.page.status_label.text())

        self.page._line_edits["gui_refresh_hz"].setText("2")
        self.page._combos["zones_mode"].setCurrentText("list")
        self.page._line_edits["zones_list"].setText("1,seven")
        self.page._apply_settings()
        self.assertEqual(self.requests, [])
        self.assertIn("comma-separated", self.page.status_label.text())

    def test_dirty_edits_survive_snapshots_and_delayed_settings_ack(self) -> None:
        self.page.set_snapshot(self._service_snapshot({"telemetry_hz": 2.0}))
        self.page._line_edits["telemetry_hz"].setText("3")
        self.page._on_form_edited()
        self.page.set_snapshot(self._service_snapshot({"telemetry_hz": 4.0}))
        self.assertEqual(self.page._line_edits["telemetry_hz"].text(), "3")

        self.page.set_adapter_status(
            AdapterStatus(
                mode="service",
                connected=True,
                read_only=False,
                provenance="fake endpoint",
                message="service reachable",
                endpoint="127.0.0.1:43210",
            ),
            service_writes_allowed=True,
        )
        self.page._apply_settings()
        self.assertEqual(self.requests[-1][0]["op"], "set_service_config")
        self.page._line_edits["telemetry_hz"].setText("9")
        self.page._on_form_edited()
        self.page.set_command_outcome(
            CommandOutcome(
                operation="set_service_config",
                result="confirmed_success",
                message="acknowledged",
                response={"service_config": {"telemetry_hz": 3.0}},
            )
        )
        self.assertEqual(self.page._line_edits["telemetry_hz"].text(), "9")
        self.assertTrue(self.page.form_dirty)

    def test_partial_connection_transition_reports_stored_settings_separately(self) -> None:
        self.page.set_adapter_status(
            AdapterStatus(
                mode="service",
                connected=True,
                read_only=False,
                provenance="fake endpoint",
                message="service reachable",
                endpoint="127.0.0.1:43210",
            ),
            service_writes_allowed=True,
        )
        self.page._refresh_connection()
        self.assertEqual(
            [message["op"] for message in self.requests[-1]],
            ["set_service_config", "refresh_connection"],
        )
        self.page.set_command_outcome(
            CommandOutcome(
                operation="set_service_config",
                result="confirmed_success",
                message="stored",
                response={"service_config": {"last_tcp_host": "192.0.2.77", "last_tcp_port": 5544}},
            )
        )
        self.page.set_command_outcome(
            CommandOutcome(
                operation="refresh_connection",
                result="confirmed_failure",
                message="simulated reconnect failure",
            )
        )
        self.assertIn("not fully confirmed", self.page.status_label.text())
        self.assertIn("127.0.0.1:43210", self.page.active_endpoint_label.text())
        self.assertIn("192.0.2.77:5544", self.page.stored_vs_active_label.text())

    def test_readbacks_include_system_alarm_scaling_and_pid_values(self) -> None:
        config = normalize_snapshot(
            "config",
            {
                "config": {
                    "system": {"temperature_scale": "DEGREE_C"},
                    "zones": {
                        "z1": {
                            "alarms": {"sp_high": 120.0, "sp_low": -10.0},
                            "scaling": {"decimal_point": 1},
                        }
                    },
                },
                "bus_id": "config-test",
                "controller_id": 2,
                "modbus_address": 2,
            },
            age_s=0,
            stale=False,
            error=None,
            modified_at=None,
            read_at=None,
            generation=1,
        )
        telemetry = normalize_snapshot(
            "telemetry",
            {
                "telemetry": {"zones": {"1": {"p_gain": 2.5, "i_gain": 0.1, "d_gain": 0.0}}},
                "bus_id": "config-test",
                "controller_id": 2,
                "modbus_address": 2,
            },
            age_s=0,
            stale=False,
            error=None,
            modified_at=None,
            read_at=None,
            generation=1,
        )
        self.page.set_snapshot(config)
        self.page.set_snapshot(telemetry)
        self.page.set_snapshot(self._service_snapshot({"zone_names": {"1": "Feed"}}))

        self.assertEqual(self.page.system_table.item(0, 0).text(), "temperature_scale")
        labels = [
            self.page.zone_tabs.tabText(index)
            for index in range(self.page.zone_tabs.count())
        ]
        self.assertEqual(labels[0], "1: Feed")
        table = self.page.zone_tabs.widget(0).findChild(type(self.page.system_table))
        rows = {
            table.item(row, 0).text(): table.item(row, 1).text()
            for row in range(table.rowCount())
        }
        self.assertEqual(rows["alarms.sp_high"], "120.0")
        self.assertEqual(rows["scaling.decimal_point"], "1")
        self.assertEqual(rows["PID P gain"], "2.5")

    def test_offline_connection_actions_are_simulated_and_do_not_change_endpoint(self) -> None:
        session = OfflineReplaySession()
        window = QtMainWindow(session)
        self.addCleanup(window.close)
        original_endpoint = session.status.endpoint
        configuration = window.configuration
        configuration._line_edits["last_tcp_host"].setText("192.0.2.10")
        configuration._line_edits["last_tcp_port"].setText("5555")
        configuration._connect_serial()

        self.assertEqual(session.status.endpoint, original_endpoint)
        self.assertEqual(session.status.mode, "offline-replay")
        self.assertIn("SIMULATED ONLY", configuration.status_label.text())
        current = session.snapshots()["service_config"].data
        self.assertEqual(current["config"]["last_tcp_host"], "192.0.2.10")
        self.assertEqual(current["config"]["last_tcp_port"], 5555)

    def test_offline_applied_viewer_settings_reach_the_chart(self) -> None:
        session = OfflineReplaySession()
        window = QtMainWindow(session)
        self.addCleanup(window.close)
        configuration = window.configuration
        configuration._combos["viewer_pv_color"].setCurrentText("green")
        configuration._checks["viewer_show_sp_abs"].setChecked(False)
        configuration._apply_settings()
        deadline = 3.0
        while window.overview.chart_panel.chart._colors["pv"] != "green" and deadline > 0:
            self.app.processEvents()
            QTest.qWait(20)
            deadline -= 0.02

        service_config = session.snapshots()["service_config"].data["config"]
        self.assertEqual(service_config["viewer"]["pv_color"], "green")
        self.assertEqual(service_config["viewer_pv_color"], "green")
        self.assertEqual(window.overview.chart_panel.chart._colors["pv"], "green")
        self.assertFalse(window.overview.chart_panel.chart.show_sp.isChecked())

    def test_retention_estimates_are_published_asynchronously_in_the_form(self) -> None:
        self.page.set_snapshot(self._service_snapshot({"telemetry_hz": 2.0}))
        deadline = 3.0
        while "Estimated retention" not in self.page.retention_estimate_label.text() and deadline > 0:
            self.app.processEvents()
            QTest.qWait(20)
            deadline -= 0.02
        self.assertIn("telemetry:", self.page.retention_estimate_label.text())
        self.assertIn("analysis:", self.page.retention_estimate_label.text())

    def test_retention_duration_parser_is_shared_with_existing_tk_configuration(self) -> None:
        from py.gui.display_panels import parse_retention_seconds as tk_parser

        self.assertEqual(parse_retention_seconds("1 week"), 604800)
        self.assertEqual(parse_retention_seconds("30m"), 1800)
        self.assertIsNone(parse_retention_seconds("forever"))
        self.assertIs(tk_parser, parse_retention_seconds)


if __name__ == "__main__":
    unittest.main()
