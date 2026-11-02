from __future__ import annotations

import json
import faulthandler
import math
import logging
import os
import platform
import sys
import threading
import tkinter as tk
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from py.qt_gui.adapter import READ_ONLY_OPERATIONS  # noqa: E402
from py.qt_gui.offline import OfflineReplaySession  # noqa: E402
from py.qt_gui.window import QtMainWindow  # noqa: E402


_STRESS_SECONDS = int(os.environ.get("CN616A_QT_STRESS_SECONDS", "600"))
_HEARTBEAT_MS = 100
_INGEST_MS = 2_000
_SIX_ZONES = tuple(range(1, 7))


def _percentile(samples: list[float], percentile: float) -> float:
    if not samples:
        raise ValueError("percentiles require at least one sample")
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]


class QtReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(["qt-reliability-tests"])

    def _wait_for(self, predicate, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.app.processEvents()
        self.assertTrue(predicate(), "Qt reliability fixture did not become ready")

    @unittest.skipUnless(
        os.environ.get("CN616A_QT_TEN_MINUTE_EXERCISE") == "1",
        "set CN616A_QT_TEN_MINUTE_EXERCISE=1 to run the ten-minute acceptance exercise",
    )
    def test_ten_minute_six_zone_replay_responsiveness(self) -> None:
        session = OfflineReplaySession()
        window = QtMainWindow(session)
        window.show()
        self.addCleanup(window.close)
        self._wait_for(lambda: session.history is not None and session.history.data is not None)
        self._wait_for(lambda: window.overview.run_recording_card.start_button.isEnabled())
        window.configuration._line_edits["viewer_history_seconds"].setText("3600")
        window.configuration._apply_settings()
        floating = window.overview.chart_panel.open_floating_window()
        panel = window.overview.chart_panel
        chart = panel.chart
        history_index = next(
            index
            for index in range(chart.window_combo.count())
            if math.isclose(float(chart.window_combo.itemData(index)), 1.0)
        )
        if chart.window_combo.currentIndex() == history_index:
            alternate_index = next(
                index for index in range(chart.window_combo.count()) if index != history_index
            )
            chart.window_combo.setCurrentIndex(alternate_index)
        chart.window_combo.setCurrentIndex(history_index)
        self.assertAlmostEqual(session._history_reader._window_hours, 1.0)
        history_reader = session._history_reader
        self.assertIsNotNone(history_reader)

        seed_line = session.paths["telemetry_log"].read_bytes().splitlines()[-1]
        seed_record = json.loads(seed_line)
        heartbeat_delays_ms: list[float] = []
        heartbeat_intervals_ms: list[float] = []
        ingest_latencies_ms: list[float] = []
        ingest_misses: list[str] = []
        requested_samples: list[tuple[int, float, float]] = []
        observed_zones: list[int] = []
        observed_tabs: list[int] = []
        config_values_seen: list[bool] = []
        ramp_edits_succeeded: list[bool] = []
        floating_timer_counts: list[int] = []
        recording_states_seen: list[bool] = []
        recovery_states_seen: list[bool] = []
        last_heartbeat = time.perf_counter()
        work_count = 0
        selected_zone = 1
        loop = QEventLoop()

        heartbeat = QTimer()
        heartbeat.setInterval(_HEARTBEAT_MS)

        def on_heartbeat() -> None:
            nonlocal last_heartbeat
            now = time.perf_counter()
            actual_interval_ms = (now - last_heartbeat) * 1000.0
            heartbeat_intervals_ms.append(actual_interval_ms)
            heartbeat_delays_ms.append(max(0.0, actual_interval_ms - _HEARTBEAT_MS))
            last_heartbeat = now
            values = chart.pv_curve.getData()[0]
            if values is None or len(values) == 0:
                return
            latest_displayed = float(values[-1])
            while requested_samples and latest_displayed >= requested_samples[0][1]:
                _zone_id, timestamp, started = requested_samples.pop(0)
                ingest_latencies_ms.append((now - started) * 1000.0)
                if ingest_latencies_ms[-1] > 1_000.0:
                    ingest_misses.append(
                        f"zone {_zone_id} timestamp {timestamp:.3f}: "
                        f"{ingest_latencies_ms[-1]:.1f} ms"
                    )

        heartbeat.timeout.connect(on_heartbeat)
        heartbeat.start()

        ingestion = QTimer()
        ingestion.setInterval(_INGEST_MS)

        def append_replay_sample() -> None:
            nonlocal selected_zone
            now = datetime.now(timezone.utc)
            timestamp = now.replace(microsecond=(now.microsecond // 1000) * 1000)
            sample = dict(seed_record)
            sample["ts"] = timestamp.isoformat(timespec="milliseconds")
            sample["timestamp_pacific"] = sample["ts"]
            payload = json.dumps(sample, separators=(",", ":"), allow_nan=False).encode("utf-8")
            with session.paths["telemetry_log"].open("ab") as stream:
                stream.write(payload + b"\n")
            requested_samples.append(
                (selected_zone, timestamp.timestamp(), time.perf_counter())
            )

        ingestion.timeout.connect(append_replay_sample)
        ingestion.start()

        workload = QTimer()
        workload.setInterval(2_000)

        def exercise() -> None:
            nonlocal work_count, selected_zone, floating
            work_count += 1
            selected_zone = ((work_count - 1) % len(_SIX_ZONES)) + 1
            window.overview._select_zone(selected_zone)
            observed_zones.append(selected_zone)

            tab_order = (0, 1, 2, 3)
            window.tabs.setCurrentIndex(tab_order[(work_count - 1) % len(tab_order)])
            observed_tabs.append(window.tabs.currentIndex())

            if window.overview.sp_abs_entry.isEnabled():
                window.overview.sp_abs_entry.setText(str(60 + work_count % 20))
                window.overview._control_form_edited()
            ramp_edits_succeeded.append(
                window.ramp_soak.profile_model.setData(
                    window.ramp_soak.profile_model.index(0, 1),
                    str(work_count % 100),
                )
            )

            if work_count == 5:
                window.configuration._checks["viewer_show_sp_abs"].setChecked(False)
                window.configuration._apply_settings()
                viewer = session.snapshots()["service_config"].data["config"]["viewer"]
                config_values_seen.append(viewer["show_sp_abs"])
            elif work_count == 6:
                window.configuration._checks["viewer_show_sp_abs"].setChecked(True)
                window.configuration._apply_settings()
                viewer = session.snapshots()["service_config"].data["config"]["viewer"]
                config_values_seen.append(viewer["show_sp_abs"])
            elif work_count == 10:
                card = window.overview.run_recording_card
                card.directory_entry.setText(
                    str(session.logs_dir / "reliability-recording")
                )
                card.name_entry.setText("Ten-minute offline reliability exercise")
                card.start_button.click()
                recording_states_seen.append(session._run_active)
            elif work_count == 12:
                card = window.overview.run_recording_card
                if card.recovery_button is not None:
                    card.recovery_button.click()
                recording_states_seen.append(session._run_active)
                recovery_states_seen.append(session._run_recovered)
            elif work_count == 15:
                window.overview.run_recording_card.stop_button.click()
                recording_states_seen.append(session._run_active)
            elif work_count == 60 and floating is not None:
                floating.close()
                floating = None
            elif work_count == 65 and floating is None:
                floating = panel.open_floating_window()
                floating_timer_counts.append(len(floating.findChildren(QTimer)))
            elif work_count % 30 == 0 and floating is not None:
                floating.activateWindow()

        workload.timeout.connect(exercise)
        workload.start()
        QTimer.singleShot(_STRESS_SECONDS * 1000, loop.quit)
        started = time.perf_counter()
        loop.exec()
        elapsed = time.perf_counter() - started

        heartbeat.stop()
        ingestion.stop()
        workload.stop()
        drain_loop = QEventLoop()
        QTimer.singleShot(1_100, drain_loop.quit)
        drain_loop.exec()
        heartbeat.stop()
        now = time.perf_counter()
        for _zone, timestamp, sample_started in requested_samples:
            ingest_misses.append(
                f"zone {_zone} timestamp {timestamp:.3f}: pending after "
                f"{(now - sample_started) * 1000.0:.1f} ms"
            )

        p95 = _percentile(heartbeat_delays_ms, 0.95)
        p99 = _percentile(heartbeat_delays_ms, 0.99)
        max_delay = max(heartbeat_delays_ms, default=float("inf"))
        max_interval = max(heartbeat_intervals_ms, default=float("inf"))
        curve_x = chart.pv_curve.getData()[0]
        summary = {
            "os": platform.platform(),
            "processor": platform.processor(),
            "python": sys.version.split()[0],
            "qt_platform": QApplication.platformName(),
            "elapsed_seconds": round(elapsed, 2),
            "heartbeat_samples": len(heartbeat_delays_ms),
            "heartbeat_delay_p95_ms": round(p95, 2),
            "heartbeat_delay_p99_ms": round(p99, 2),
            "heartbeat_delay_max_ms": round(max_delay, 2),
            "heartbeat_interval_max_ms": round(max_interval, 2),
            "ingested_samples": len(ingest_latencies_ms),
            "ingest_latency_p95_ms": (
                round(_percentile(ingest_latencies_ms, 0.95), 2)
                if ingest_latencies_ms
                else None
            ),
            "ingest_latency_max_ms": (
                round(max(ingest_latencies_ms), 2) if ingest_latencies_ms else None
            ),
            "ingest_misses": len(ingest_misses),
            "workload_cycles": work_count,
            "floating_windows": len(panel._floating_windows),
            "plot_points": len(curve_x) if curve_x is not None else 0,
            "retained_logs_rows": len(window.logs._reader._tail.entries),
            "retained_command_outcomes": len(window.command_outcomes),
            "zones_visited": len(set(observed_zones)),
            "tabs_visited": len(set(observed_tabs)),
            "history_reader_workers": int(history_reader._worker.is_alive()),
            "chart_transform_workers": panel._executor._max_workers,
        }
        print("QT_RELIABILITY_METRICS=" + json.dumps(summary, sort_keys=True))

        self.assertGreaterEqual(elapsed, _STRESS_SECONDS - 1.0, summary)
        self.assertLessEqual(p95, 50.0, summary)
        self.assertLessEqual(p99, 200.0, summary)
        self.assertLessEqual(max_delay, 500.0, summary)
        self.assertLessEqual(max_interval, 500.0, summary)
        self.assertGreater(len(ingest_latencies_ms), 0, summary)
        self.assertLessEqual(max(ingest_latencies_ms), 1_000.0, summary)
        self.assertEqual(ingest_misses, [], ingest_misses[:5])
        minimum_work_cycles = (
            250
            if _STRESS_SECONDS >= 600
            else max(15, int(_STRESS_SECONDS / 2.0) - 1)
        )
        self.assertGreaterEqual(work_count, minimum_work_cycles, summary)
        self.assertLessEqual(len(panel._floating_windows), 1, summary)
        self.assertLessEqual(summary["plot_points"], 20_000, summary)
        self.assertLessEqual(summary["retained_logs_rows"], 5_000, summary)
        self.assertLessEqual(summary["retained_command_outcomes"], 100, summary)
        self.assertEqual(summary["history_reader_workers"], 1, summary)
        self.assertEqual(summary["chart_transform_workers"], 1, summary)
        self.assertEqual(set(observed_zones), set(_SIX_ZONES), summary)
        self.assertEqual(set(observed_tabs), {0, 1, 2, 3}, summary)
        self.assertEqual(config_values_seen, [False, True], summary)
        self.assertTrue(all(ramp_edits_succeeded), summary)
        self.assertEqual(recording_states_seen, [True, True, False], summary)
        self.assertEqual(recovery_states_seen, [True], summary)
        self.assertEqual(floating_timer_counts, [0], summary)
        self.assertIsNotNone(window._history_poll_timer)
        self.assertTrue(window._history_poll_timer.isActive())
        self.assertFalse(session._run_active)
        self.assertEqual(set(session.snapshots()), {
            "telemetry",
            "config",
            "rampsoak",
            "analysis",
            "service_config",
        })
        logs_reader = window.logs._reader
        window.close()
        self.assertTrue(session._closed)
        self.assertIsNone(session._history_reader)
        self.assertFalse(logs_reader.is_alive())
        self.assertIsNotNone(history_reader)
        self.assertFalse(history_reader._worker.is_alive())

    def test_reopening_offline_window_recreates_only_temporary_workers_and_no_commands(self) -> None:
        session = OfflineReplaySession()
        first = QtMainWindow(session)
        first_history_reader = session._history_reader
        first_log_reader = first.logs._reader
        first.close()
        self.assertTrue(session._closed)
        self.assertFalse(first_log_reader.is_alive())
        self.assertIsNotNone(first_history_reader)
        self.assertFalse(first_history_reader._worker.is_alive())

        second_session = OfflineReplaySession()
        second = QtMainWindow(second_session)
        self.addCleanup(second.close)
        self._wait_for(lambda: second_session.history is not None)
        self.assertEqual(
            [
                outcome.operation
                for outcome in second.command_outcomes
                if outcome.operation not in READ_ONLY_OPERATIONS
            ],
            [],
        )
        self.assertEqual(
            [
                outcome.operation
                for outcome in second_session.outcomes()
                if outcome.operation not in READ_ONLY_OPERATIONS
            ],
            [],
        )
        self.assertTrue(second.logs._reader.is_alive())
        self.assertIsNot(second_session._history_reader, session._history_reader)
        history_reader = second_session._history_reader
        log_reader = second.logs._reader
        second.close()
        self.assertTrue(second_session._closed)
        self.assertIsNone(second_session._history_reader)
        self.assertFalse(log_reader.is_alive())
        self.assertIsNotNone(history_reader)

    def test_tk_rollback_reads_replay_snapshots_without_service_starting_launcher(self) -> None:
        from py.gui import main_gui

        session = OfflineReplaySession()
        clients = []
        logger = logging.getLogger("cn616a.gui")
        existing_handlers = set(logger.handlers)
        previous_excepthook = sys.excepthook
        previous_thread_excepthook = threading.excepthook

        class NoSocketServiceClient:
            def __init__(self) -> None:
                self.operations: list[str] = []
                self.closed = False
                clients.append(self)

            def submit(self, _host, _port, messages, _callback, **_kwargs):
                self.operations.extend(str(message.get("op", "")) for message in messages)
                return "submitted"

            def poll_results(self) -> int:
                return 0

            def shutdown(self, *, wait: bool = True) -> None:
                del wait
                self.closed = True

        try:
            with patch.object(main_gui, "ServiceClient", NoSocketServiceClient):
                try:
                    gui = main_gui.CN616AGUI(logs_dir=session.logs_dir, refresh_interval=0.1)
                except tk.TclError as exc:
                    self.skipTest(f"Tk display is unavailable: {exc}")
                try:
                    gui.root.withdraw()
                    gui.root.update()
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline:
                        gui.root.update()
                        snapshot = gui.snapshot_reader.snapshot("telemetry")
                        if snapshot.data is not None:
                            break
                        time.sleep(0.01)
                    self.assertIsNotNone(snapshot.data)
                    gui.telemetry_panel.refresh()
                    self.assertEqual(len(gui.telemetry_panel.zone_numeric_labels), 6)
                    self.assertIn("SYNTHETIC", gui.telemetry_panel.info_label.cget("text"))
                    self.assertTrue(clients)
                    self.assertEqual(
                        [
                            operation
                            for operation in clients[0].operations
                            if operation not in READ_ONLY_OPERATIONS
                        ],
                        [],
                    )
                finally:
                    gui._on_closing()
        finally:
            faulthandler.disable()
            sys.excepthook = previous_excepthook
            threading.excepthook = previous_thread_excepthook
            for handler in tuple(logger.handlers):
                if handler not in existing_handlers:
                    logger.removeHandler(handler)
                    handler.close()
            session.shutdown()
        self.assertTrue(clients[0].closed)


if __name__ == "__main__":
    unittest.main()
