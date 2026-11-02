from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication  # noqa: E402

from py.qt_gui.logs import (  # noqa: E402
    GUI_EXCEPTION_BUFFER,
    MAX_DISPLAY_ROWS,
    MAX_LINE_BYTES,
    BoundedLogTail,
    LogsPage,
    install_gui_exception_hooks,
)
from py.qt_gui import logs as logs_module  # noqa: E402
from py.qt_gui.offline import OfflineReplaySession  # noqa: E402
from py.qt_gui.window import QtMainWindow  # noqa: E402


class QtLogsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(["qt-logs-tests"])

    def _wait_for(self, predicate, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.app.processEvents()
        self.assertTrue(predicate(), "Qt logs page did not reach expected state")

    def test_append_and_partial_jsonl_rows_are_incremental(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cn616a_telemetry_log.jsonl"
            path.write_text('{"ts":"2026-11-01T10:00:00Z","value":1}\n{"ts":"', encoding="utf-8")
            reader = BoundedLogTail()
            first = reader.poll(path, jsonl=True)
            self.assertTrue(first.reset)
            self.assertEqual(len(first.entries), 1)
            self.assertIn("2026-11-01T10:00:00Z", first.entries[0])

            appended = '2026-11-01T10:01:00Z","value":2}\n'.encode("utf-8")
            with path.open("ab") as stream:
                stream.write(appended)
            second = reader.poll(path, jsonl=True)
            self.assertFalse(second.reset)
            self.assertEqual(len(second.entries), 1)
            self.assertIn('"value":2', second.entries[0])
            self.assertEqual(reader.last_read_bytes, len(appended))

    def test_rotation_replacement_and_truncation_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cn616a_analysis_log.jsonl"
            path.write_text('{"ts":"2026-11-01T10:00:00Z","value":"before rotation"}\n', encoding="utf-8")
            reader = BoundedLogTail()
            reader.poll(path, jsonl=True)
            rotated = path.with_name("cn616a_analysis_log_20261101_100100.jsonl")
            path.rename(rotated)
            path.write_text('{"ts":"2026-11-01T10:01:00Z","value":"after rotation"}\n', encoding="utf-8")
            replacement = reader.poll(path, jsonl=True)
            self.assertTrue(replacement.reset)
            self.assertIn("rotated or replaced", replacement.notice or "")
            self.assertTrue(any("after rotation" in row for row in replacement.entries))

            path.write_text('{"ts":"2026-11-01T10:02:00Z"}\n', encoding="utf-8")
            truncated = reader.poll(path, jsonl=True)
            self.assertTrue(truncated.reset)
            self.assertIn("truncated", truncated.notice or "")

    def test_missing_denied_malformed_and_oversized_sources_are_visible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.jsonl"
            reader = BoundedLogTail()
            self.assertIn("Unable to read", reader.poll(missing, jsonl=True).error or "")

            path = Path(directory) / "denied.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            original_open = Path.open

            def denied(candidate: Path, *args, **kwargs):
                if candidate == path:
                    raise PermissionError("test access denied")
                return original_open(candidate, *args, **kwargs)

            with patch.object(Path, "open", denied):
                denied_update = reader.poll(path, jsonl=True)
            self.assertIn("PermissionError", denied_update.error or "")

            path.write_bytes(
                b"{malformed}\n"
                + (b"x" * (MAX_LINE_BYTES + 1))
                + b"\n"
                + b'{"ts":"2026-11-01T10:02:00Z","ok":true}\n'
            )
            parsed = reader.poll(path, jsonl=True)
            self.assertTrue(any("MALFORMED JSONL" in row for row in parsed.entries))
            self.assertTrue(any("OVERSIZED ROW" in row for row in parsed.entries))
            self.assertTrue(any('"ok":true' in row for row in parsed.entries))

    def test_display_retains_at_most_five_thousand_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cn616a_config_log.jsonl"
            with path.open("w", encoding="utf-8") as stream:
                for index in range(MAX_DISPLAY_ROWS + 5):
                    stream.write(json.dumps({"ts": f"2026-11-01T10:{index % 60:02d}:00Z", "n": index}))
                    stream.write("\n")
            reader = BoundedLogTail()
            result = reader.poll(path, jsonl=True)
            self.assertEqual(len(reader.entries), MAX_DISPLAY_ROWS)
            self.assertEqual(len(result.entries), MAX_DISPLAY_ROWS)
            self.assertIn('"n":5', reader.entries[0])
            self.assertIn(f'"n":{MAX_DISPLAY_ROWS + 4}', reader.entries[-1])

    def test_category_switches_source_and_surfaces_missing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cn616a_telemetry_log.jsonl").write_text(
                '{"ts":"2026-11-01T10:00:00Z","kind":"telemetry"}\n',
                encoding="utf-8",
            )
            (root / "cn616a_config_log.jsonl").write_text(
                '{"ts":"2026-11-01T10:01:00Z","kind":"configuration"}\n',
                encoding="utf-8",
            )
            page = LogsPage(root)
            self.addCleanup(page.shutdown)
            page.category_combo.setCurrentIndex(
                page.category_combo.findData("Telemetry JSONL")
            )
            self._wait_for(lambda: "telemetry" in page.output.toPlainText())
            self.assertIn(str(root / "cn616a_telemetry_log.jsonl"), page.source_label.text())
            page.category_combo.setCurrentIndex(
                page.category_combo.findData("Configuration JSONL")
            )
            self._wait_for(lambda: "configuration" in page.output.toPlainText())
            self.assertIn(str(root / "cn616a_config_log.jsonl"), page.source_label.text())

            absent_page = LogsPage(root / "absent")
            self.addCleanup(absent_page.shutdown)
            absent_page.category_combo.setCurrentIndex(
                absent_page.category_combo.findData("Service diagnostics")
            )
            self._wait_for(lambda: "Unable to list source directory" in absent_page.error_label.text())

    def test_offline_log_updates_while_logs_tab_is_hidden_and_shutdown_joins_reader(self) -> None:
        session = OfflineReplaySession()
        window = QtMainWindow(session)
        self.addCleanup(window.close)
        window.logs.category_combo.setCurrentIndex(
            window.logs.category_combo.findData("Telemetry JSONL")
        )
        self._wait_for(lambda: bool(window.logs.output.toPlainText()))
        self.assertEqual(window.tabs.currentWidget(), window.overview)
        self.assertIn("OFFLINE REPLAY / NO HARDWARE", window.logs.provenance_label.text())
        self.assertNotEqual(window.logs._reader.thread_ident, threading.get_ident())
        self.assertIn("SIMULATED ONLY", window.logs.recording_summary_label.text())
        self._wait_for(lambda: "Recording INACTIVE" in window.logs.recording_summary_label.text())
        reader = window.logs._reader
        window.logs.shutdown()
        self.assertFalse(reader.is_alive())

    def test_gui_exception_hooks_keep_default_reporting_and_bound_history(self) -> None:
        previous_sys_hook = sys.excepthook
        previous_thread_hook = threading.excepthook
        sys_reports: list[tuple[object, object, object]] = []
        thread_reports: list[object] = []
        error = RuntimeError("hooked GUI exception")
        try:
            sys.excepthook = lambda *args: sys_reports.append(args)
            threading.excepthook = lambda args: thread_reports.append(args)
            logs_module._HOOKS_INSTALLED = False
            install_gui_exception_hooks()
            sys.excepthook(RuntimeError, error, None)
            thread_args = SimpleNamespace(
                exc_type=RuntimeError,
                exc_value=error,
                exc_traceback=None,
                thread=threading.current_thread(),
            )
            threading.excepthook(thread_args)
            self.assertEqual(len(sys_reports), 1)
            self.assertEqual(thread_reports, [thread_args])
            self.assertTrue(
                any("hooked GUI exception" in record for record in GUI_EXCEPTION_BUFFER.records())
            )
            for index in range(110):
                GUI_EXCEPTION_BUFFER.record(f"exception {index}")
            self.assertEqual(len(GUI_EXCEPTION_BUFFER.records()), 100)
        finally:
            sys.excepthook = previous_sys_hook
            threading.excepthook = previous_thread_hook
            logs_module._HOOKS_INSTALLED = False

    def test_shutdown_during_background_read_stops_reader_after_bounded_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cn616a_telemetry_log.jsonl"
            path.write_text('{"ts":"2026-11-01T10:00:00Z"}\n', encoding="utf-8")
            entered = threading.Event()
            release = threading.Event()
            original_poll = BoundedLogTail.poll

            def blocked_poll(reader, source, *, jsonl):
                entered.set()
                release.wait(timeout=3.0)
                return original_poll(reader, source, jsonl=jsonl)

            with patch.object(BoundedLogTail, "poll", blocked_poll):
                page = LogsPage(Path(directory))
                page.category_combo.setCurrentIndex(
                    page.category_combo.findData("Telemetry JSONL")
                )
                self._wait_for(entered.is_set)
                reader_thread = page._reader
                try:
                    page.shutdown()
                    self.assertTrue(reader_thread.is_alive())
                finally:
                    release.set()
                reader_thread.join(timeout=2.0)
                self.assertFalse(reader_thread.is_alive())

    def test_recording_summary_gui_exceptions_and_log_navigation(self) -> None:
        page = LogsPage(Path(tempfile.gettempdir()))
        self.addCleanup(page.shutdown)
        page.set_recording_summary(
            {
                "status": {
                    "run_active": True,
                    "run_name": "sample",
                    "run_dir": "C:\\recordings\\sample",
                    "run_last_data_ts": "2026-11-01T10:00:00-08:00",
                },
                "error": None,
                "simulated": False,
            }
        )
        page.category_combo.setCurrentIndex(
            page.category_combo.findData("Recording summary")
        )
        self.assertIn("Recording ACTIVE", page.output.toPlainText())
        self.assertIn("sample", page.output.toPlainText())

        page.category_combo.setCurrentIndex(
            page.category_combo.findData("GUI exceptions")
        )
        GUI_EXCEPTION_BUFFER.record("RuntimeError: captured GUI failure")
        self._wait_for(lambda: "captured GUI failure" in page.output.toPlainText())
        self.assertIn("original exception reporting remains enabled", page.error_label.text())

        session = OfflineReplaySession()
        window = QtMainWindow(session)
        self.addCleanup(window.close)
        window.view_logs_button.click()
        self.assertEqual(window.tabs.currentWidget(), window.logs)
        self.assertEqual(window.logs.category_combo.currentData(), "Service activity")


if __name__ == "__main__":
    unittest.main()
