from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication  # noqa: E402

from py.gui.run_schedule import parse_pacific_schedule  # noqa: E402
from py.qt_gui.adapter import (  # noqa: E402
    AdapterStatus,
    CommandOutcome,
    ControllerIdentity,
    ControllerKey,
)
from py.qt_gui.offline import OfflineReplaySession  # noqa: E402
from py.qt_gui.run_recording import RunRecordingCard  # noqa: E402
from py.qt_gui.window import QtMainWindow  # noqa: E402


IDENTITY = ControllerIdentity(ControllerKey("TEST_BUS", 1), 1)


def _status(*, offline: bool = False) -> AdapterStatus:
    return AdapterStatus(
        mode="offline-replay" if offline else "service",
        connected=not offline,
        read_only=offline,
        provenance="test source",
        message="test source",
        target=IDENTITY,
        generation=7,
        controller_connected=not offline,
    )


def _outcome(
    operation: str,
    result: str,
    response: dict[str, object] | None = None,
    message: str = "test outcome",
) -> CommandOutcome:
    return CommandOutcome(
        operation=operation,
        result=result,
        message=message,
        response=MappingProxyType(response) if response is not None else None,
        generation=7,
        endpoint="test",
        created_at=datetime.now(timezone.utc),
    )


class QtRecordingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(["qt-recording-tests"])

    def _wait_for(self, predicate, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.002)
        self.app.processEvents()
        self.assertTrue(predicate(), "Qt recording UI did not reach expected state")

    def test_schedule_conversion_preserves_pacific_dst_offsets(self) -> None:
        winter = parse_pacific_schedule("2026-01-15 12:30")
        summer = parse_pacific_schedule("2026-07-15 12:30:45")

        self.assertIsNotNone(winter)
        self.assertIsNotNone(summer)
        self.assertEqual(winter.isoformat(), "2026-01-15T12:30:00-08:00")
        self.assertEqual(summer.isoformat(), "2026-07-15T12:30:45-07:00")
        self.assertIsNone(parse_pacific_schedule("invalid"))

    def test_directory_picker_populates_manual_entry_and_reports_dialog_failure(self) -> None:
        card = RunRecordingCard(lambda _messages, _key: "submitted")
        self.addCleanup(card.shutdown)
        with patch(
            "py.qt_gui.run_recording.QFileDialog.getExistingDirectory",
            return_value="C:\\recordings",
        ):
            card.browse_button.click()
        self.assertEqual(card.directory_entry.text(), "C:\\recordings")

        with patch(
            "py.qt_gui.run_recording.QFileDialog.getExistingDirectory",
            side_effect=RuntimeError("picker unavailable"),
        ):
            card.browse_button.click()
        self.assertIn("enter a path manually", card.feedback_label.text())

    def test_requests_are_exact_and_active_state_waits_for_status(self) -> None:
        requests: list[tuple[tuple[dict[str, object], ...], str | None]] = []

        def submitter(messages, coalesce_key):
            requests.append((tuple(dict(message) for message in messages), coalesce_key))
            return "submitted"

        card = RunRecordingCard(submitter)
        card.set_adapter_status(_status())
        self.addCleanup(card.shutdown)
        card.set_command_outcome(
            _outcome("get_run_status", "confirmed_success", {"ok": True, "run_active": False})
        )
        with tempfile.TemporaryDirectory() as temporary:
            target_dir = Path(temporary) / "recording"
            card.directory_entry.setText(str(target_dir))
            card.name_entry.setText("Batch A")
            card.start_button.click()

            self.assertEqual(
                requests[-1][0],
                (
                    {
                        "op": "start_run",
                        "run_dir": str(target_dir),
                        "run_name": "Batch A",
                    },
                ),
            )
            self.assertIn("INACTIVE", card.status_label.text())
            self.assertIn("state is not assumed", card.feedback_label.text())
            card.set_command_outcome(
                _outcome("start_run", "confirmed_success", {"ok": True, "run_active": True})
            )
            self.assertIn("INACTIVE", card.status_label.text())
            self.assertFalse(target_dir.exists())

            card.set_command_outcome(
                _outcome(
                    "get_run_status",
                    "confirmed_success",
                    {
                        "ok": True,
                        "run_active": True,
                        "run_name": "Batch A",
                        "run_elapsed_s": 2.0,
                        "run_last_data_ts": "2026-01-15T20:30:00+00:00",
                        "run_schedule_enabled": False,
                    },
                )
            )
            self.assertIn("Recording ACTIVE", card.status_label.text())
            self.assertIn("Last data:", card.status_label.text())

            card.schedule_entry.setText("2099-01-15 12:30")
            card.enable_schedule_button.click()
            self.assertEqual(
                requests[-1][0],
                (
                    {
                        "op": "set_run_schedule",
                        "enabled": True,
                        "stop_at_iso": "2099-01-15T12:30:00-08:00",
                    },
                ),
            )
            card.set_command_outcome(
                _outcome("set_run_schedule", "confirmed_success")
            )
            card.schedule_entry.setText("not a date")
            previous_count = len(requests)
            card.enable_schedule_button.click()
            self.assertEqual(len(requests), previous_count)

            card.set_command_outcome(
                _outcome("stop_run", "unknown", message="timed out")
            )
            self.assertIn("No automatic retry", card.feedback_label.text())
            self.assertEqual(
                [messages[0]["op"] for messages, _key in requests],
                ["start_run", "get_run_status", "set_run_schedule", "get_run_status"],
            )

    def test_offline_recording_recovery_and_files_stay_in_temporary_session(self) -> None:
        session = OfflineReplaySession()
        self.addCleanup(session.shutdown)
        window = QtMainWindow(session)
        self.addCleanup(window.close)
        card = window.overview.run_recording_card
        self.assertIn("SIMULATED SOURCE", card.source_label.text())
        self._wait_for(lambda: card.start_button.isEnabled())

        requested_directory = session.logs_dir.parent / f"{session.logs_dir.name}-must-not-be-created"
        card.directory_entry.setText(str(requested_directory))
        card.name_entry.setText("Synthetic test")
        card.start_button.click()
        self._wait_for(lambda: "Recording ACTIVE" in card.status_label.text())

        simulated_directory = Path(str(session.outcomes()[-1].response["run_dir"]))
        self.assertTrue(simulated_directory.is_relative_to(session.logs_dir))
        self.assertNotEqual(simulated_directory, requested_directory)
        self.assertFalse(requested_directory.exists())
        telemetry_path = simulated_directory / "cn616a_telemetry_log.jsonl"
        records = [line for line in telemetry_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 1)
        self.assertIn('"port":"offline-replay"', records[0])

        card.recovery_button.click()
        self.assertIn("Recovered after a 30.0 s interruption", card.status_label.text())
        self.assertIn("SIMULATED ONLY", card.feedback_label.text())
        run_info = (simulated_directory / "cn616a_run_info.json").read_text(encoding="utf-8")
        self.assertIn('"event": "recovered"', run_info)
        self.assertIn('"recovered": true', run_info)

        card.stop_button.click()
        self._wait_for(lambda: "Recording INACTIVE" in card.status_label.text())
        stopped_info = (simulated_directory / "cn616a_run_info.json").read_text(encoding="utf-8")
        self.assertIn('"event": "stopped"', stopped_info)
        window.close()
        self.assertFalse(session.logs_dir.exists())


if __name__ == "__main__":
    unittest.main()
