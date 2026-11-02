from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import time
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402

from py.gui.state_values import get_numeric, get_zone, get_zones  # noqa: E402
from py.qt_gui.__main__ import build_parser, main  # noqa: E402
from py.qt_gui.adapter import ControllerIdentity, ControllerKey, ZoneKey  # noqa: E402
from py.qt_gui.offline import BASELINE_DIR, OfflineReplaySession, _mock_analysis  # noqa: E402
from py.qt_gui.window import QtMainWindow  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ARCHIVE = BASELINE_DIR / "modbus-reference.json.gz"
FIXTURE_MANIFEST = BASELINE_DIR / "manifest.json"


class QtGuiFoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(["qt-gui-tests"])

    def test_parser_accepts_offline_replay_and_help_without_importing_qt(self) -> None:
        self.assertTrue(build_parser().parse_args(["--offline-replay"]).offline_replay)
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        result = subprocess.run(
            [sys.executable, "-S", "-m", "py.qt_gui", "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--offline-replay", result.stdout)
        self.assertIn("--read-only", result.stdout)

    def test_service_mode_requires_explicit_endpoint_before_qt_import(self) -> None:
        valid = build_parser().parse_args(
            [
                "--service",
                "--host",
                "example.invalid",
                "--port",
                "12345",
                "--logs-dir",
                str(self.id()),
            ]
        )
        self.assertTrue(valid.service)
        read_only = build_parser().parse_args(
            [
                "--service",
                "--read-only",
                "--host",
                "example.invalid",
                "--port",
                "12345",
                "--logs-dir",
                str(self.id()),
            ]
        )
        self.assertTrue(read_only.read_only)
        invalid_arguments = (
            ["--service"],
            ["--host", "example.invalid", "--port", "12345", "--logs-dir", "."],
            [
                "--service",
                "--host",
                "example.invalid",
                "--port",
                "0",
                "--logs-dir",
                ".",
            ],
            ["--offline-replay", "--service"],
            ["--read-only"],
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        main(arguments)
                self.assertEqual(raised.exception.code, 2)

    def test_missing_qt_dependency_has_actionable_diagnostic(self) -> None:
        original_import = __import__

        def missing_qt(name, *args, **kwargs):
            if name == "PySide6" or name.startswith("PySide6."):
                raise ModuleNotFoundError("missing test Qt", name="PySide6")
            return original_import(name, *args, **kwargs)

        error_output = io.StringIO()
        with patch("builtins.__import__", side_effect=missing_qt):
            with redirect_stderr(error_output):
                result = main(["--offline-replay"])
        self.assertEqual(result, 2)
        self.assertIn("requirements-qt.txt", error_output.getvalue())

    def test_four_tabs_mode_labels_and_read_only_default(self) -> None:
        window = QtMainWindow()
        self.assertEqual(
            [window.tabs.tabText(index) for index in range(window.tabs.count())],
            ["Overview", "Configuration", "Ramp/Soak", "Logs"],
        )
        self.assertIn("DISCONNECTED / READ-ONLY", window.mode_banner.text())
        self.assertIn("no endpoint is configured or probed", window.mode_banner.text().lower())
        window.close()

    def test_offline_session_generates_service_shaped_data_and_cleans_up(self) -> None:
        before_archive = hashlib.sha256(FIXTURE_ARCHIVE.read_bytes()).hexdigest()
        before_manifest = FIXTURE_MANIFEST.read_bytes()
        manifest = json.loads(before_manifest)
        session = OfflineReplaySession()
        temporary_dir = session.logs_dir
        try:
            self.assertTrue(temporary_dir.is_dir())
            self.assertEqual(session.status.mode, "offline-replay")
            self.assertFalse(session.status.connected)
            self.assertTrue(session.status.read_only)
            self.assertIn("mock-derived", session.status.provenance)
            self.assertEqual(session.status.target.key.controller_id, 1)
            self.assertEqual(session.status.target.modbus_address, 1)
            snapshots = session.snapshots()
            self.assertEqual(set(snapshots), {"telemetry", "config", "rampsoak", "analysis", "service_config"})
            self.assertIn("telemetry", snapshots["telemetry"].data)
            self.assertIn("analysis", snapshots["analysis"].data)
            self.assertTrue(snapshots["telemetry"].keyed_values)
            self.assertTrue(session.paths["telemetry_log"].is_file())
            with session.paths["telemetry_log"].open(encoding="utf-8") as stream:
                telemetry_records = [json.loads(line) for line in stream]
            self.assertEqual(len(telemetry_records), 16)
            self.assertEqual(
                [record["ts"] for record in telemetry_records],
                sorted(record["ts"] for record in telemetry_records),
            )
            self.assertEqual(session.outcomes(), ())
            self.assertEqual(manifest["archive_sha256"], before_archive)
        finally:
            session.shutdown()
        self.assertFalse(temporary_dir.exists())
        self.assertEqual(hashlib.sha256(FIXTURE_ARCHIVE.read_bytes()).hexdigest(), before_archive)
        self.assertEqual(FIXTURE_MANIFEST.read_bytes(), before_manifest)

    def test_offline_backend_returns_mock_outcome_without_network_fallback(self) -> None:
        with OfflineReplaySession() as session:
            target = ZoneKey("offline-replay", 1, 1)
            with patch(
                "socket.create_connection",
                side_effect=AssertionError("offline replay attempted a service connection"),
            ) as connect:
                status = session.submit(
                    [{"op": "set_sp_abs", "zone": 1, "value_c": 70.0}],
                    target=target,
                )
            self.assertEqual(status, "mocked")
            self.assertFalse(connect.called)
            self.assertEqual(session.outcomes()[-1].result, "mock")
            self.assertIn("SIMULATED ONLY", session.outcomes()[-1].message)

    def test_offline_zone_update_scenarios_are_deterministic_and_never_use_network(self) -> None:
        messages = [
            {"op": "set_sp_abs", "zone": 1, "value_c": 0.0},
            {"op": "set_control_method", "zone": 1, "method": "PID_CONTROL"},
            {"op": "set_autotune_setpoint", "zone": 1, "value_c": -1.0},
        ]
        expected = {
            "success": ["confirmed_success"] * 3,
            "rejection": ["confirmed_failure", "not_sent", "not_sent"],
            "partial": ["confirmed_success", "confirmed_failure", "not_sent"],
            "unknown": ["unknown", "not_sent", "not_sent"],
        }
        with OfflineReplaySession() as session:
            target = ZoneKey("offline-replay", 1, 1)
            for scenario, results in expected.items():
                session.set_mock_command_scenario(scenario)
                before = len(session.outcomes())
                with patch(
                    "socket.create_connection",
                    side_effect=AssertionError("offline replay attempted a service connection"),
                ) as connect:
                    self.assertEqual(session.submit(messages, target=target), "mocked")
                outcomes = session.outcomes()[before:]
                self.assertEqual([outcome.result for outcome in outcomes], results)
                self.assertTrue(all("SIMULATED ONLY" in outcome.message for outcome in outcomes))
                self.assertFalse(connect.called)

    def test_offline_zone_update_controls_show_simulated_partial_result(self) -> None:
        session = OfflineReplaySession()
        window = QtMainWindow(session)
        try:
            page = window.overview
            self.assertEqual(page.selected_zone_key, ZoneKey("offline-replay", 1, 1))
            self.assertTrue(page.sp_abs_entry.isEnabled())
            page.mock_scenario_combo.setCurrentIndex(page.mock_scenario_combo.findData("partial"))
            page.sp_abs_entry.setText("0")
            page.method_combo.setCurrentIndex(page.method_combo.findData("PID_CONTROL"))
            page.autotune_sp_entry.setText("-1")
            page._control_form_edited()
            with patch(
                "socket.create_connection",
                side_effect=AssertionError("offline UI attempted a service connection"),
            ) as connect:
                page.apply_controls_button.click()
            self.assertFalse(connect.called)
            self.assertEqual(
                [outcome.result for outcome in session.outcomes()],
                ["confirmed_success", "confirmed_failure", "not_sent"],
            )
            self.assertIn("SIMULATED ONLY", page.control_status_label.text())
            self.assertIn("No service/hardware request", page.control_status_label.text())

            page.mock_scenario_combo.setCurrentIndex(page.mock_scenario_combo.findData("unknown"))
            page.apply_controls_button.click()
            self.assertEqual(
                [outcome.result for outcome in session.outcomes()[-3:]],
                ["unknown", "not_sent", "not_sent"],
            )
            self.assertIn("unknown after send", page.control_status_label.text())
            self.assertIn("do not retry blindly", page.control_status_label.text())
        finally:
            window.close()

    def test_mock_analysis_matches_the_replay_corpus_and_numeric_aliases(self) -> None:
        with OfflineReplaySession() as session:
            telemetry = [
                json.loads(line)
                for line in session.paths["telemetry_log"].read_text(encoding="utf-8").splitlines()
            ]
            analysis = [
                json.loads(line)
                for line in session.paths["analysis_log"].read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual(len(telemetry), len(analysis))
        for sample, result in zip(telemetry, analysis):
            zones = get_zones(sample, "telemetry")
            for zone_id in range(1, 7):
                zone = get_zone(zones, zone_id)
                pv = get_numeric(zone, "pv_c", "pv", "PV", "process_value_c", "process_value")
                sp = get_numeric(
                    zone, "sp_abs_c", "sp_abs", "sp_c", "sp", "SP_abs", "SP", "setpoint_c", "setpoint"
                )
                expected = round(abs(pv - sp), 3) if pv is not None and sp is not None else None
                self.assertEqual(result["analysis"][str(zone_id)]["avg_abs_error_c"], expected)

        alternate = _mock_analysis({"zones": {"z1": {"PV": 0, "SP": -1}}})
        self.assertEqual(alternate["1"]["avg_abs_error_c"], 1.0)

    def test_offline_startup_never_constructs_serial_or_calls_an_endpoint(self) -> None:
        serial_module = __import__("pymodbus.client.sync", fromlist=["ModbusSerialClient"])
        driver_module = sys.modules.get("cn616a")
        serial_client_guard = patch.object(
            serial_module,
            "ModbusSerialClient",
            side_effect=AssertionError("serial client construction attempted"),
        )
        serial_guard = patch(
            "serial.Serial",
            side_effect=AssertionError("serial port construction attempted"),
        )
        create_connection_guard = patch(
            "socket.create_connection",
            side_effect=AssertionError("endpoint connection attempted"),
        )
        connect_guard = patch.object(
            socket.socket,
            "connect",
            side_effect=AssertionError("endpoint connection attempted"),
        )
        connect_ex_guard = patch.object(
            socket.socket,
            "connect_ex",
            side_effect=AssertionError("endpoint probe attempted"),
        )
        driver_guard = (
            patch.object(
                driver_module,
                "ModbusSerialClient",
                side_effect=AssertionError("serial client construction attempted"),
            )
            if driver_module is not None
            else nullcontext()
        )
        captured: dict[str, object] = {}

        class AutoCloseWindow(QtMainWindow):
            def show(self) -> None:
                captured["logs_dir"] = self.adapter.logs_dir
                captured["status"] = self.adapter.status.message
                super().show()
                QTimer.singleShot(0, self.close)

        with (
            serial_client_guard,
            serial_guard,
            create_connection_guard,
            connect_guard,
            connect_ex_guard,
            driver_guard,
        ):
            with patch("py.qt_gui.window.QtMainWindow", AutoCloseWindow):
                result = main(["--offline-replay"])
        self.assertEqual(result, 0)
        self.assertEqual(captured["status"], "OFFLINE REPLAY / NO HARDWARE")
        self.assertFalse(Path(captured["logs_dir"]).exists())

    def test_window_close_is_bounded(self) -> None:
        session = OfflineReplaySession()
        window = QtMainWindow(session)
        started = time.monotonic()
        window.close()
        self.assertLess(time.monotonic() - started, 1.0)

    def test_existing_tk_and_service_imports_do_not_load_qt(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, 'py'); "
                "import py.gui.main_gui, py.cn616a_service; "
                "assert not any(name == 'PySide6' or name.startswith('PySide6.') "
                "for name in sys.modules)",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_identity_records_are_immutable_and_explicit(self) -> None:
        controller = ControllerKey(bus_id="loop-1", controller_id=3)
        zone = ZoneKey(bus_id="loop-1", controller_id=3, zone_id=2)
        identity = ControllerIdentity(key=controller, modbus_address=17)
        self.assertEqual((controller.controller_id, zone.zone_id), (3, 2))
        self.assertEqual(identity.modbus_address, 17)
        with self.assertRaises((AttributeError, TypeError)):
            controller.controller_id = 4
        with self.assertRaises((AttributeError, TypeError)):
            identity.modbus_address = 18

    def test_qt_batch_forwards_arguments_without_starting_service(self) -> None:
        launcher = (REPO_ROOT / "bat" / "gui_qt_start.bat").read_text(encoding="utf-8")
        self.assertIn('"%PY%" -m py.qt_gui %*', launcher)
        self.assertNotIn("cn616a_service.py", launcher)
        self.assertNotIn("service_start.bat", launcher)


if __name__ == "__main__":
    unittest.main()
