from __future__ import annotations

import os
import unittest
import time
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from py.qt_gui.adapter import (  # noqa: E402
    AdapterHistory,
    ControllerIdentity,
    ControllerKey,
    ZoneKey,
    normalize_snapshot,
)
from py.qt_gui.charts import (  # noqa: E402
    ChartsPanel,
    ChartWidget,
    _MAX_PLOT_POINTS,
    _prepare_chart_series,
    _series_arrays,
)
from py.qt_gui.offline import OfflineReplaySession  # noqa: E402
from py.qt_gui.window import QtMainWindow  # noqa: E402


IDENTITY = ControllerIdentity(ControllerKey("chart-test", 2), 2)
ZONE = ZoneKey("chart-test", 2, 1)


class QtGuiChartsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _history(
        self,
        *,
        count: int = 4,
        revision: int = 1,
        data: Mapping[str, object] | None = None,
    ) -> AdapterHistory:
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        times = [start + timedelta(seconds=value) for value in (0, 1, 20, 21)][:count]
        if count > len(times):
            times = [start + timedelta(seconds=index) for index in range(count)]
        if data is None:
            data = MappingProxyType(
                {
                    "telemetry": {
                        1: {
                            "times": times,
                            "pv": [100.0 + index for index in range(count)],
                            "sp": [110.0] * count,
                            "sp_autotune": [105.0] * count,
                        }
                    },
                    "analysis": {
                        1: {
                            "times": times,
                            "mae": [10.0 - index for index in range(count)],
                        }
                    },
                    "history_hours": 1.0,
                    "errors": [],
                }
            )
        return AdapterHistory(
            generation=1,
            endpoint="offline-replay",
            identity=IDENTITY,
            data=data,
            error=None,
            source_identity=("chart-test", 2),
            revision=revision,
        )

    def _service_config(self, settings: dict[str, object]):
        return normalize_snapshot(
            "service_config",
            {
                "config": settings,
                "bus_id": IDENTITY.key.bus_id,
                "controller_id": IDENTITY.key.controller_id,
                "modbus_address": IDENTITY.modbus_address,
            },
            age_s=0,
            stale=False,
            error=None,
            modified_at=None,
            read_at=None,
            generation=1,
        )

    def test_series_preserve_timestamps_values_and_break_large_gaps(self) -> None:
        history = self._history()
        widget = ChartWidget()
        widget.set_selection(1, ZONE)
        widget.set_history(history)

        x_values, pv_values = widget.pv_curve.getData()
        self.assertEqual(list(x_values), [value.timestamp() for value in history.data["telemetry"][1]["times"]])
        self.assertEqual(list(pv_values), [100.0, 101.0, 102.0, 103.0])
        self.assertEqual(list(widget.pv_curve.opts["connect"]), [True, False, True, True])
        self.assertEqual(list(widget.sp_curve.getData()[1]), [110.0] * 4)
        self.assertEqual(list(widget.autotune_curve.getData()[1]), [105.0] * 4)
        self.assertEqual(list(widget.mae_curve.getData()[1]), [10.0, 9.0, 8.0, 7.0])
        self.assertIn("4 telemetry timestamps", widget.status_label.text())
        widget.close()

    def test_viewer_preferences_aliases_window_and_clear_view_are_non_destructive(self) -> None:
        requested: list[float] = []
        panel = ChartsPanel(lambda hours: requested.append(hours) or len(requested))
        history = self._history()
        original_values = list(history.data["telemetry"][1]["pv"])
        panel.set_selection(1, ZONE)
        panel.set_history(history)
        panel.set_service_config(
            self._service_config(
                {
                    "zone_names": {"1": "Feed"},
                    "viewer_history_hours": 0.25,
                    "viewer_line_width": 3.0,
                    "viewer_pv_color": "#123456",
                    "viewer_show_sp_abs": False,
                    "viewer_show_sp_autotune": True,
                    "viewer_show_mae": False,
                }
            )
        )
        panel._render_views()
        self.assertEqual(requested, [0.25])
        self.assertEqual(panel.chart.zone_label.text(), "Zone 1 · Feed")
        self.assertEqual(panel.chart.sp_curve.opts["pen"].widthF(), 3.0)
        self.assertEqual(panel.chart.pv_curve.opts["pen"].color().name(), "#123456")
        self.assertFalse(panel.chart.show_sp.isChecked())
        self.assertTrue(panel.chart.show_autotune.isChecked())
        self.assertFalse(panel.chart.show_mae.isChecked())

        panel.chart.window_combo.setCurrentIndex(panel.chart.window_combo.findData(1.0))
        self.assertEqual(requested[-1], 1.0)
        panel.chart.plot_widget.setXRange(0, 1, padding=0)
        panel.chart.reset_view()
        self.assertAlmostEqual(panel.chart.plot_widget.viewRange()[0][1] - panel.chart.plot_widget.viewRange()[0][0], 3600.0)
        self.assertEqual(history.data["telemetry"][1]["pv"], original_values)
        self.assertIn("unchanged", panel.chart.status_label.text())
        panel.shutdown()
        panel.close()

    def test_legacy_viewer_object_preferences_apply_and_identity_mismatch_is_withheld(self) -> None:
        widget = ChartWidget()
        widget.set_selection(1, ZONE)
        widget.set_history(self._history())
        widget.set_service_config(
            self._service_config(
                {
                    "zone_names": {"1": "Feed"},
                    "viewer": {
                        "history_hours": 4.0,
                        "line_width": 1.25,
                        "pv_color": "green",
                        "show_sp_abs": True,
                        "show_sp_autotune": False,
                        "show_mae": True,
                    },
                    "viewer_line_width": 9.0,
                }
            )
        )
        self.assertEqual(widget._history_hours, 4.0)
        self.assertEqual(widget.pv_curve.opts["pen"].color().name(), "#008000")
        self.assertFalse(widget.show_autotune.isChecked())
        self.assertEqual(widget.zone_label.text(), "Zone 1 · Feed")

        foreign = normalize_snapshot(
            "service_config",
            {"config": {"viewer_line_width": 7.0}, "bus_id": "foreign", "unit": 8},
            age_s=0,
            stale=False,
            error=None,
            modified_at=None,
            read_at=None,
            generation=1,
        )
        widget.set_service_config(foreign)
        self.assertEqual(widget._line_width, 1.25)
        widget.set_history(
            AdapterHistory(
                generation=1,
                endpoint="test",
                identity=None,
                data=None,
                error="History source identity does not match.",
                source_identity=("foreign", 8),
                revision=2,
            )
        )
        self.assertIsNone(widget.pv_curve.getData()[0])
        self.assertEqual(widget.status_label.text(), "History source identity does not match.")
        widget.close()

    def test_analysis_metrics_are_shown_only_for_the_matching_controller(self) -> None:
        panel = ChartsPanel()
        panel.set_selection(1, ZONE)
        panel.set_history(self._history())
        analysis = normalize_snapshot(
            "analysis",
            {
                "analysis": {
                    "1": {
                        "avg_abs_error_c": 0.125,
                        "threshold_c": 0.5,
                        "n_points": 12,
                    }
                },
                "bus_id": IDENTITY.key.bus_id,
                "controller_id": IDENTITY.key.controller_id,
                "modbus_address": IDENTITY.modbus_address,
            },
            age_s=0,
            stale=False,
            error=None,
            modified_at=None,
            read_at=None,
            generation=1,
        )
        panel.set_analysis_snapshot(analysis)
        QTest.qWait(100)
        self.assertIn("MAE 0.125 °C", panel.chart.metrics_label.text())
        self.assertIn("threshold 0.50 °C", panel.chart.metrics_label.text())
        self.assertIn("samples 12", panel.chart.metrics_label.text())
        panel.shutdown()
        panel.close()

    def test_rendered_series_never_exceeds_reader_plot_cap(self) -> None:
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        times = [start + timedelta(seconds=index) for index in range(_MAX_PLOT_POINTS + 1)]
        x_values, y_values, connections = _series_arrays(times, list(range(len(times))))
        self.assertEqual(x_values.size, _MAX_PLOT_POINTS)
        self.assertEqual(y_values.size, _MAX_PLOT_POINTS)
        self.assertEqual(connections.size, _MAX_PLOT_POINTS)

    def test_large_series_transform_runs_off_the_gui_thread(self) -> None:
        worker_threads: list[int] = []
        gui_thread = threading.get_ident()

        def transform(data, zone_id):
            worker_threads.append(threading.get_ident())
            return _prepare_chart_series(data, zone_id)

        panel = ChartsPanel()
        panel.set_selection(1, ZONE)
        with patch("py.qt_gui.charts._prepare_chart_series", side_effect=transform):
            panel.set_history(self._history(count=5000))
            QTest.qWait(150)
        self.assertTrue(worker_threads)
        self.assertNotEqual(worker_threads[0], gui_thread)
        panel.shutdown()
        panel.close()

    def test_floating_windows_share_cached_history_and_close_without_timers(self) -> None:
        session = OfflineReplaySession()
        window = QtMainWindow(session)
        panel = window.overview.chart_panel
        reader = session._history_reader
        first = panel.open_floating_window()
        second = panel.open_floating_window()
        self.assertEqual(len(panel._floating_windows), 2)
        self.assertIs(session._history_reader, reader)
        self.assertEqual(first.centralWidget().findChildren(QTimer), [])
        first.close()
        QTest.qWait(10)
        self.assertEqual(len(panel._floating_windows), 1)
        reopened = panel.open_floating_window()
        self.assertEqual(len(panel._floating_windows), 2)
        self.assertEqual(reopened.centralWidget().findChildren(QTimer), [])
        window.close()
        QTest.qWait(10)
        self.assertEqual(len(panel._floating_windows), 0)
        self.assertTrue(session._closed)

    def test_offline_history_reader_publishes_and_changes_window_without_new_reader(self) -> None:
        session = OfflineReplaySession()
        window = QtMainWindow(session)
        reader = session._history_reader
        deadline = time.monotonic() + 3.0
        while session.history is None and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertIsNotNone(session.history)
        self.assertEqual(session.history.identity, session.status.target)
        self.assertEqual(
            session.history.source_identity,
            (session.status.target.key.bus_id, session.status.target.modbus_address),
        )
        self.assertIs(session._history_reader, reader)

        chart = window.overview.chart_panel.chart
        QTest.qWait(60)
        chart.window_combo.setCurrentIndex(chart.window_combo.findData(0.25))
        deadline = time.monotonic() + 3.0
        while (
            session.history is None
            or session.history.data is None
            or session.history.data["history_hours"] != 0.25
        ) and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertEqual(session.history.data["history_hours"], 0.25)
        self.assertIs(session._history_reader, reader)
        window.close()
        self.assertTrue(session._closed)

    def test_coalesced_chart_updates_allow_ui_heartbeat(self) -> None:
        panel = ChartsPanel()
        panel.set_selection(1, ZONE)
        large_history = self._history(count=5000)
        heartbeats = 0
        updates = 0
        heartbeat = QTimer()
        heartbeat.setInterval(2)

        def beat() -> None:
            nonlocal heartbeats
            heartbeats += 1

        def update() -> None:
            nonlocal updates
            updates += 1
            panel.set_history(
                AdapterHistory(
                    generation=1,
                    endpoint="test",
                    identity=IDENTITY,
                    data=large_history.data,
                    error=None,
                    source_identity=("chart-test", 2),
                    revision=updates,
                )
            )
            if updates >= 30:
                update_timer.stop()

        update_timer = QTimer()
        update_timer.setInterval(2)
        update_timer.timeout.connect(update)
        heartbeat.timeout.connect(beat)
        heartbeat.start()
        update_timer.start()
        QTest.qWait(350)
        QTest.qWait(100)
        heartbeat.stop()
        self.assertGreaterEqual(updates, 10)
        self.assertGreaterEqual(heartbeats, 10)
        self.assertEqual(panel.chart._history.revision, updates)
        panel.shutdown()
        panel.close()


if __name__ == "__main__":
    unittest.main()
