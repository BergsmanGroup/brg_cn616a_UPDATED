"""Design-only checks; run with the isolated prototype requirements."""

import os
from pathlib import Path
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QPushButton

from prototype import APP_STYLE, DemoWindow


class PrototypeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        if os.name == "nt" and not QFontDatabase.families():
            # The Windows offscreen plugin does not enumerate system fonts.
            font = Path(os.environ["WINDIR"]) / "Fonts" / "segoeui.ttf"
            if QFontDatabase.addApplicationFont(str(font)) == -1:
                raise RuntimeError(f"Could not load the offscreen test font: {font}")
        cls.app.setStyleSheet(APP_STYLE)

    def setUp(self) -> None:
        self.window = DemoWindow()
        self.window.show()
        self.window.timer.stop()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.window.record_blink.stop()
        for window in self.window.detached_windows:
            window.close()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_all_scales_fit_compact_desktop_layout(self) -> None:
        for index in range(3):
            with self.subTest(units=index + 1):
                self.window.scale_select.setCurrentIndex(index)
                self.app.processEvents()
                table = self.window.zone_table
                self.assertEqual(self.window.width(), 1440)
                self.assertEqual(self.window.height(), 900)
                self.assertEqual(table.rowCount(), (index + 1) * 6)
                self.assertEqual(table.rowHeight(0), 24)
                self.assertEqual(table.horizontalScrollBar().maximum(), 0)
                self.assertEqual(table.verticalScrollBar().maximum(), 0)
                self.assertEqual(self.window.unit_table.rowCount(), index + 1)

    def test_filter_focus_and_scale_reduction(self) -> None:
        self.window.scale_select.setCurrentIndex(2)
        self.window.controller_filter.setCurrentIndex(3)
        self.assertEqual(self.window.zone_table.rowCount(), 6)
        focus = self.window.zone_table.cellWidget(5, 8).findChildren(QPushButton)[0]
        focus.click()
        self.assertEqual(self.window.focused_zone, 18)
        self.assertIn("Unit 03 / local zone 6", self.window.focus_label.text())
        self.assertEqual(self.window.zone_table.currentRow(), 5)
        self.window.scale_select.setCurrentIndex(0)
        self.assertEqual(self.window.focused_zone, 1)
        self.assertIn("Unit 01 / local zone 1", self.window.focus_label.text())

    def test_metric_and_status_colors_and_offline_values(self) -> None:
        self.window.scale_select.setCurrentIndex(2)
        table = self.window.zone_table
        self.assertEqual(table.item(0, 3).foreground().color().name(), "#0757b4")
        self.assertEqual(table.item(0, 4).foreground().color().name(), "#945400")
        self.assertEqual(table.item(0, 6).foreground().color().name(), "#6841a5")
        self.assertEqual(table.item(6, 5).background().color().name(), "#fff0cf")
        self.assertEqual(table.item(12, 5).text(), "Unknown")
        self.assertEqual(table.item(12, 3).text(), "\N{EM DASH}")
        self.assertFalse(table.cellWidget(12, 8).findChildren(QPushButton)[1].isEnabled())
        self.assertEqual(table.cellWidget(0, 8).findChildren(QPushButton)[1].property("role"), "deactivate")
        self.assertEqual(table.cellWidget(1, 8).findChildren(QPushButton)[1].property("role"), "activate")

    def test_recording_indicator_and_stop(self) -> None:
        self.window.record_button.click()
        self.assertTrue(self.window.recording)
        self.assertTrue(self.window.record_blink.isActive())
        self.assertTrue(self.window.record_state.text().startswith("REC"))
        self.window._blink_record_indicator()
        self.assertFalse(self.window.record_state.text().startswith("REC"))
        self.window.record_button.click()
        self.assertFalse(self.window.recording)
        self.assertFalse(self.window.record_blink.isActive())
        self.assertIn("NOT RECORDING", self.window.record_state.text())

    def test_tabs_and_detached_chart_updates(self) -> None:
        self.assertEqual(
            [self.window.tabs.tabText(i) for i in range(self.window.tabs.count())],
            ["Overview", "Configuration", "Ramp / Soak", "Logs"],
        )
        for index in range(self.window.tabs.count()):
            self.window.tabs.setCurrentIndex(index)
            self.app.processEvents()
            self.assertTrue(self.window.tabs.currentWidget().isVisible())
        self.window._open_chart_window()
        self.window.sp_input.setValue(42)
        for _ in range(3):
            self.window._advance_simulation()
        for plot in (self.window.plot, self.window.detached_windows[0].centralWidget()):
            pv, sp = plot.listDataItems()
            self.assertEqual(len(pv.getData()[1]), 3)
            self.assertEqual(list(sp.getData()[1]), [42, 42, 42])


if __name__ == "__main__":
    unittest.main()
