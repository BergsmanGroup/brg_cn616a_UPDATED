"""Standalone simulated CN616A Qt GUI prototype; never connects to hardware."""

from __future__ import annotations

import argparse
import math
import sys
from collections import deque

import pyqtgraph as pg
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


APP_STYLE = """
QWidget { color: #172b3d; font-family: "Segoe UI"; font-size: 9pt; }
QMainWindow, QTabWidget::pane { background: #e8f0f8; }
QWidget#card { background: white; border: 1px solid #b9cde0; border-radius: 4px; }
QLabel#banner { background: #0a4175; color: #fff0c8; padding: 8px 12px; font-weight: 700; }
QLabel#cardTitle { background: #164b76; color: white; font-size: 10pt; font-weight: 700; padding: 6px 8px; }
QLabel#cardTitle[tone="teal"] { background: #076f80; }
QLabel#cardTitle[tone="violet"] { background: #6841a5; }
QLabel#cardTitle[tone="record"] { background: #793a65; }
QLabel#cardTitle[tone="blue"] { background: #0867cb; }
QLabel[role="fieldLabel"] { color: #395c7c; font-size: 8pt; font-weight: 700; }
QLabel#focusIdentity { color: #0758b0; background: #e7f2ff; padding: 6px; font-weight: 700; }
QLabel#pidValues { color: #6841a5; background: #f2ecfa; padding: 5px; font-weight: 700; }
QLabel#recordState { color: #a42b42; background: #fff0f3; padding: 5px; font-weight: 700; }
QLabel#healthSummary { color: #075965; background: #edf8fa; padding: 6px; }
QLabel#visualKey { color: #395c7c; background: #edf4fb; padding: 4px; font-size: 8pt; }
QPushButton { background: #f0f6ff; border: 1px solid #9fbbd8; border-radius: 3px; padding: 4px 7px; color: #14528c; font-weight: 600; }
QPushButton:hover { background: #deedff; border-color: #0867cb; }
QPushButton[role="primary"] { background: #0867cb; color: white; border-color: #075ab7; }
QPushButton[role="activate"] { background: #def5e9; color: #10633d; border-color: #9dcbb1; }
QPushButton[role="deactivate"], QPushButton[role="danger"] { background: #fff0f3; color: #a42b42; border-color: #e1a9b5; }
QPushButton:disabled { background: #edf0f3; color: #6a7886; border-color: #c9d2db; }
QComboBox, QDoubleSpinBox { background: #f0f6ff; border: 1px solid #9fbbd8; border-radius: 3px; padding: 4px 6px; min-height: 18px; }
QDoubleSpinBox#setpoint { background: #fff3d4; color: #8b4e00; border-color: #d6aa59; font-size: 11pt; font-weight: 700; }
QTableWidget { background: white; alternate-background-color: #f1f6fc; gridline-color: #e0eaf3; border: 0; selection-background-color: #dcedff; selection-color: #103c65; }
QTableWidget::item { padding: 2px 5px; }
QHeaderView::section { background: #e1edf9; color: #214d75; padding: 4px 6px; border: 0; font-size: 8pt; font-weight: 700; }
QTabBar::tab { background: #dce9f7; color: #214a71; padding: 7px 18px; font-weight: 700; }
QTabBar::tab:selected { background: #0867cb; color: white; }
QStatusBar { background: #f6faff; color: #526b82; font-size: 8pt; }
"""

ZONE_NAMES = ("Demo chamber", "Demo stage", "Demo valve", "Demo sensor", "Demo bench", "Demo load")
STATUS_COLORS = {
    "Online": ("#10633d", "#def5e9"),
    "Stale": ("#905600", "#fff0cf"),
    "Offline": ("#a42b42", "#ffe4e9"),
}


def card(title: str, *widgets: QWidget, tone: str = "navy") -> QWidget:
    panel = QWidget()
    panel.setObjectName("card")
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(6, 6, 6, 6)
    layout.setSpacing(6)
    heading = QLabel(title)
    heading.setObjectName("cardTitle")
    heading.setProperty("tone", tone)
    layout.addWidget(heading)
    for widget in widgets:
        layout.addWidget(widget)
    return panel


class DemoWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CN616A Control Desk - Simulated Prototype")
        self.resize(1440, 900)
        self.controller_count = 2
        self.focused_zone = 1
        self.recording = False
        self.tick_count = 0
        self.history = deque(maxlen=180)
        self.detached_windows: list[QMainWindow] = []

        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 8, 10, 6)
        outer.setSpacing(6)
        banner = QLabel(
            "SIMULATED DATA ONLY - no service, socket, serial port, or controller commands"
        )
        banner.setObjectName("banner")
        outer.addWidget(banner)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Simulation scale:"))
        self.scale_select = QComboBox()
        self.scale_select.addItems(
            ["1 controller / 6 zones", "2 controllers / 12 zones", "3 controllers / 18 zones"]
        )
        self.scale_select.setCurrentIndex(1)
        self.scale_select.currentIndexChanged.connect(self._set_scale)
        toolbar.addWidget(self.scale_select)
        toolbar.addWidget(QLabel("Controller filter:"))
        self.controller_filter = QComboBox()
        self.controller_filter.addItems(["All controllers", "Unit 01", "Unit 02"])
        self.controller_filter.currentIndexChanged.connect(self._render_zones)
        toolbar.addWidget(self.controller_filter)
        toolbar.addStretch(1)
        toolbar.addWidget(QLabel("All numbers and actions are synthetic."))
        outer.addLayout(toolbar)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._overview_page(), "Overview")
        self.tabs.addTab(self._configuration_page(), "Configuration")
        self.tabs.addTab(self._rampsoak_page(), "Ramp / Soak")
        self.tabs.addTab(self._logs_page(), "Logs")
        outer.addWidget(self.tabs, 1)

        self.setCentralWidget(root)
        for layout in root.findChildren(QVBoxLayout):
            layout.setSpacing(6)
        for table in root.findChildren(QTableWidget):
            table.setAlternatingRowColors(True)
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            table.verticalHeader().hide()
            table.verticalHeader().setMinimumSectionSize(26)
            table.verticalHeader().setDefaultSectionSize(26)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setStretchLastSection(True)
        self.zone_table.verticalHeader().setMinimumSectionSize(24)
        self.zone_table.verticalHeader().setDefaultSectionSize(24)
        self.zone_table.horizontalHeader().setStretchLastSection(False)
        self.zone_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for form_layout in root.findChildren(QFormLayout):
            form_layout.setContentsMargins(0, 0, 0, 0)
            form_layout.setSpacing(6)
            for row in range(form_layout.rowCount()):
                item = form_layout.itemAt(row, QFormLayout.ItemRole.LabelRole)
                if item is not None and item.widget() is not None:
                    item.widget().setProperty("role", "fieldLabel")
        self.statusBar().showMessage("Simulation active - no hardware connection")
        self._render_zones()
        self._render_units()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._advance_simulation)
        self.timer.start(1000)
        self.record_blink = QTimer(self)
        self.record_blink.setInterval(600)
        self.record_blink.timeout.connect(self._blink_record_indicator)

    def _overview_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        split = QSplitter()
        rail = QWidget()
        rail.setMinimumWidth(310)
        rail.setMaximumWidth(360)
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(0, 0, 0, 0)
        self.health_label = QLabel()
        self.health_label.setObjectName("healthSummary")
        self.health_label.setWordWrap(True)
        rail_layout.addWidget(card("Service health / simulated", self.health_label, tone="teal"))
        self.record_state = QLabel("NOT RECORDING / demo")
        self.record_state.setObjectName("recordState")
        record_buttons = QWidget()
        record_layout = QHBoxLayout(record_buttons)
        record_layout.setContentsMargins(0, 0, 0, 0)
        self.record_button = QPushButton("Start recording (demo)")
        self.record_button.setProperty("role", "primary")
        self.record_button.clicked.connect(self._toggle_recording)
        record_layout.addWidget(self.record_button)
        record_layout.addWidget(QPushButton("Choose path (demo)"))
        rail_layout.addWidget(card(
            "Data recording / all configured zones", self.record_state, record_buttons,
            tone="record",
        ))
        self.focus_label = QLabel()
        self.focus_label.setObjectName("focusIdentity")
        self.focus_label.setWordWrap(True)
        self.sp_input = QDoubleSpinBox()
        self.sp_input.setObjectName("setpoint")
        self.sp_input.setRange(-200, 2000)
        self.sp_input.setDecimals(1)
        self.sp_input.setValue(30.0)
        self.mode_select = QComboBox()
        self.mode_select.addItems(["PID", "On / Off"])
        self.control_mode = QComboBox()
        self.control_mode.addItems(["Standard", "Ramp / Soak"])
        self.pid_label = QLabel("P / I / D: 2.4 / 0.12 / 18.0 (demo)")
        self.pid_label.setObjectName("pidValues")
        form = QWidget()
        form_layout = QFormLayout(form)
        form_layout.addRow("SETPOINT / C", self.sp_input)
        form_layout.addRow("METHOD", self.mode_select)
        form_layout.addRow("MODE", self.control_mode)
        action_row = QWidget()
        action_layout = QHBoxLayout(action_row)
        action_layout.setContentsMargins(0, 0, 0, 0)
        autotune = QPushButton("Autotune... (demo)")
        autotune.setProperty("role", "primary")
        autotune.clicked.connect(self._show_autotune_notice)
        detach = QPushButton("Float chart")
        detach.clicked.connect(self._open_chart_window)
        action_layout.addWidget(autotune)
        action_layout.addWidget(detach)
        safety = QLabel("Autotune requires a hardware safety review.\nThis prototype sends no command.")
        safety.setWordWrap(True)
        rail_layout.addWidget(card(
            "Focused zone / local controls", self.focus_label, form, self.pid_label,
            action_row, safety, tone="blue",
        ))
        rail_layout.addStretch(1)

        workspace = QWidget()
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        self.zone_table = QTableWidget(0, 9)
        self.zone_table.setHorizontalHeaderLabels(
            ["Unit / addr", "Zone", "Name", "PV / C", "SP / C", "Mode / state", "MAE", "Equil.", "Actions / demo"]
        )
        self.zone_table.cellClicked.connect(self._focus_table_row)
        self.zone_table.setMinimumHeight(260)
        key = QLabel("BLUE: PV / measured     AMBER: SP / target     VIOLET: MAE     GREEN: equilibrium yes")
        key.setObjectName("visualKey")
        workspace_layout.addWidget(card("Zone overview / select a row to focus", self.zone_table, key), 3)
        self.plot = self._new_plot()
        workspace_layout.addWidget(card("Focused zone / synthetic live trend", self.plot, tone="teal"), 1)
        split.addWidget(workspace)
        split.addWidget(rail)
        split.setSizes([1060, 330])
        layout.addWidget(split)
        self._update_focus_label()
        return page

    def _new_plot(self) -> pg.PlotWidget:
        plot = pg.PlotWidget()
        plot.setBackground("w")
        plot.showGrid(x=True, y=True, alpha=0.25)
        plot.setLabel("left", "Temperature", units="C")
        plot.setLabel("bottom", "Recent synthetic samples")
        plot.addLegend()
        plot.plot(pen=pg.mkPen("#1769d2", width=2), name="PV (demo)")
        plot.plot(pen=pg.mkPen("#b97909", width=2, style=Qt.PenStyle.DashLine), name="SP (demo)")
        plot.setMinimumHeight(145)
        return plot

    def _configuration_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        top = QHBoxLayout()
        bus = QLabel(
            "RS-485 bus settings are shared. PC service = Modbus master; "
            "each CN616A = uniquely addressed slave.\n"
            "Port COM4, 115200 baud, N parity, 8-N-1 are illustrative only."
        )
        bus.setWordWrap(True)
        top.addWidget(card("Bus and service (illustrative)", bus, tone="teal"), 1)
        top.addWidget(card("Polling / recording (proposal)", QLabel(
            "Telemetry cadence: demo 2 Hz\n"
            "History: JSONL with rotation\n"
            "Run recording: all configured zones\n"
            "Equilibrium: global window + MAE threshold"
        ), tone="violet"), 1)
        layout.addLayout(top)
        self.unit_table = QTableWidget(0, 5)
        self.unit_table.setMaximumHeight(112)
        self.unit_table.setHorizontalHeaderLabels(
            ["Unit", "Model", "Slave address", "Status", "Last response / errors"]
        )
        layout.addWidget(card("Configured controller units", self.unit_table))
        self.zone_config_table = QTableWidget(6, 5)
        self.zone_config_table.setMaximumHeight(190)
        self.zone_config_table.setHorizontalHeaderLabels(
            ["Local zone", "User label", "Input type", "Control mode", "Default SP (demo)"]
        )
        for local in range(1, 7):
            values = [str(local), ZONE_NAMES[local - 1], "Type K (demo)", "PID / Standard", f"{20 + local * 10}.0"]
            for col, value in enumerate(values):
                self.zone_config_table.setItem(local - 1, col, QTableWidgetItem(value))
        layout.addWidget(card("Zone metadata for selected controller", self.zone_config_table))
        layout.addStretch(1)
        return page

    def _rampsoak_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        selector = QWidget()
        selector_layout = QHBoxLayout(selector)
        selector_layout.addWidget(QLabel("Controller: Unit 01 (demo)"))
        selector_layout.addWidget(QLabel("Local zone:"))
        zone = QComboBox()
        zone.addItems([f"Zone {index} - {name}" for index, name in enumerate(ZONE_NAMES, 1)])
        selector_layout.addWidget(zone)
        selector_layout.addWidget(QLabel("Profile status: simulated ramping"))
        selector_layout.addStretch(1)
        layout.addWidget(selector)
        profile = QTableWidget(3, 5)
        profile.setMaximumHeight(112)
        profile.setHorizontalHeaderLabels(["Segment", "Target C", "Slope C/min", "Hold h", "Phase (demo)"])
        for row, values in enumerate(
            [
                ["1", "60.0", "1.0", "0.25", "Ramping"],
                ["2", "90.0", "0.5", "1.0", "Pending"],
                ["3", "40.0", "0.8", "0.5", "Pending"],
            ]
        ):
            for col, value in enumerate(values):
                profile.setItem(row, col, QTableWidgetItem(value))
        layout.addWidget(card("Profile segments (synthetic)", profile, tone="violet"))
        actions = QWidget()
        action_layout = QHBoxLayout(actions)
        for label in ("Add segment (demo)", "Validate (demo)", "Save (demo only)", "Start (demo)", "Hold (demo)", "Stop (demo)"):
            button = QPushButton(label)
            if label.startswith(("Save", "Start")):
                button.setProperty("role", "primary")
            elif label.startswith("Stop"):
                button.setProperty("role", "danger")
            button.clicked.connect(lambda _checked=False, action=label: self.statusBar().showMessage(
                f"{action}: simulation only; no service command was sent", 4000
            ))
            action_layout.addWidget(button)
        layout.addWidget(card("Actions do not reach the service", actions))
        note = QLabel(
            "The manual describes up to 20 segments per zone. End behavior and pause/stop semantics "
            "must be confirmed against the controller before implementation."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    def _logs_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        sources = QLabel(
            "Telemetry recordings (JSONL) | service events | controller communication errors | GUI diagnostics\n"
            "These are separate categories. Stopping data recording does not change controller Run / Idle."
        )
        layout.addWidget(card("Log sources", sources, tone="violet"))
        history = QTableWidget(3, 5)
        history.setMaximumHeight(112)
        history.setHorizontalHeaderLabels(["Run", "State", "Started", "Last data", "Zones"])
        rows = [
            ["Demo run A", "Recovered after restart (sim)", "13:40 (sim)", "13:49 (sim)", "12"],
            ["Demo run B", "Stopped (sim)", "09:10 (sim)", "10:23 (sim)", "6"],
            ["Service", "Unit 03 offline (sim)", "13:46 (sim)", "No response", "-"],
        ]
        for row, values in enumerate(rows):
            for col, value in enumerate(values):
                history.setItem(row, col, QTableWidgetItem(value))
        layout.addWidget(card("Recording history and service events", history, tone="record"))
        layout.addWidget(QLabel(
            "Missing/offline values are unknown, not zero. The current service writes state JSON and JSONL; "
            "CSV export is not implied by this concept."
        ))
        layout.addStretch(1)
        return page

    def _set_scale(self, index: int) -> None:
        self.controller_count = index + 1
        if self.focused_zone > self.controller_count * 6:
            self.focused_zone = 1
        self.controller_filter.clear()
        self.controller_filter.addItem("All controllers")
        for unit in range(1, self.controller_count + 1):
            self.controller_filter.addItem(f"Unit {unit:02d}")
        self._render_zones()
        self._render_units()

    def _state_for_unit(self, unit: int) -> str:
        if self.controller_count == 3 and unit == 3:
            return "Offline"
        if self.controller_count >= 2 and unit == 2:
            return "Stale"
        return "Online"

    def _render_zones(self, *_args) -> None:
        table = getattr(self, "zone_table", None)
        if table is None:
            return
        filter_index = self.controller_filter.currentIndex()
        rows: list[tuple[int, int, str]] = []
        for unit in range(1, self.controller_count + 1):
            if filter_index and filter_index != unit:
                continue
            for local in range(1, 7):
                rows.append((unit, local, self._state_for_unit(unit)))
        table.setRowCount(len(rows))
        for row, (unit, local, state) in enumerate(rows):
            global_zone = (unit - 1) * 6 + local
            offline = state == "Offline"
            values = [
                f"Unit {unit:02d} / slave {unit}",
                str(local),
                ZONE_NAMES[local - 1],
                "—" if offline else f"{28 + local * 2.4 + unit * .2:.2f}",
                "—" if offline else f"{30 + local * 2:.1f}",
                "Unknown" if offline else f"PID / {state}",
                "—" if offline else f"{0.01 * local:.3f}",
                "Unknown" if offline else ("Yes" if local % 2 == 0 else "No"),
                "",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col in (3, 4, 6):
                    foreground, background = {
                        3: ("#0757b4", "#eaf3ff"),
                        4: ("#945400", "#fff4db"),
                        6: ("#6841a5", "#f2ecfa"),
                    }[col]
                    item.setForeground(QColor(foreground))
                    item.setBackground(QColor(background))
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                elif col == 5 or col == 7:
                    state_key = state
                    if col == 7 and not offline:
                        state_key = "Online" if local % 2 == 0 else "Stale"
                    foreground, background = STATUS_COLORS[state_key]
                    item.setForeground(QColor(foreground))
                    item.setBackground(QColor(background))
                table.setItem(row, col, item)
            table.item(row, 0).setData(256, global_zone)
            action_cell = QWidget()
            action_layout = QHBoxLayout(action_cell)
            action_layout.setContentsMargins(2, 0, 2, 0)
            focus = QPushButton("Focus")
            focus.clicked.connect(lambda _checked=False, zone=global_zone: self._focus_zone(zone))
            action_layout.addWidget(focus)
            zone_action = QPushButton(
                "Unavailable" if offline else "Deactivate (demo)" if local % 2 else "Activate (demo)"
            )
            zone_action.setProperty("role", "deactivate" if local % 2 else "activate")
            zone_action.setEnabled(not offline)
            zone_action.clicked.connect(
                lambda _checked=False: self._show_activation_notice()
            )
            action_layout.addWidget(zone_action)
            action_layout.addStretch(1)
            table.setCellWidget(row, 8, action_cell)
        self._update_focus_label()

    def _render_units(self) -> None:
        if not hasattr(self, "unit_table"):
            return
        self.unit_table.setRowCount(self.controller_count)
        for row in range(self.controller_count):
            unit = row + 1
            state = self._state_for_unit(unit)
            last = "1.2 s / 0 errors" if state == "Online" else "18 s / 1 error" if state == "Stale" else "Never / 3 errors"
            values = [f"Unit {unit:02d}", "CN616A", str(unit), state, last]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 3:
                    foreground, background = STATUS_COLORS[state]
                    item.setForeground(QColor(foreground))
                    item.setBackground(QColor(background))
                self.unit_table.setItem(row, col, item)
        self.health_label.setText(
            f"<b>{self.controller_count} UNITS / {self.controller_count * 6} ZONES</b><br>"
            "Latest synthetic update: 1.2 s<br><br>"
            + "<br>".join(
                f"Unit {unit:02d} / slave {unit}: <b>{self._state_for_unit(unit)}</b>"
                for unit in range(1, self.controller_count + 1)
            )
        )

    def _focus_table_row(self, row: int, _column: int) -> None:
        item = self.zone_table.item(row, 0)
        if item is not None:
            self.focused_zone = int(item.data(256))
            self._update_focus_label()

    def _focus_zone(self, zone: int) -> None:
        self.focused_zone = zone
        self._update_focus_label()

    def _update_focus_label(self) -> None:
        if not hasattr(self, "focus_label"):
            return
        unit = (self.focused_zone - 1) // 6 + 1
        local = (self.focused_zone - 1) % 6 + 1
        self.focus_label.setText(f"Unit {unit:02d} / local zone {local} / {ZONE_NAMES[local - 1]} (demo)")
        for row in range(self.zone_table.rowCount()):
            if self.zone_table.item(row, 0).data(256) == self.focused_zone:
                self.zone_table.selectRow(row)
                break

    def _toggle_recording(self) -> None:
        self.recording = not self.recording
        self.record_button.setText("Stop recording (demo)" if self.recording else "Start recording (demo)")
        self.record_state.setText("REC / recording all zones (demo)" if self.recording else "NOT RECORDING / demo")
        if self.recording:
            self.record_blink.start()
        else:
            self.record_blink.stop()
        self.statusBar().showMessage(
            "Simulated recording state changed - no files are written", 4000
        )

    def _blink_record_indicator(self) -> None:
        self.record_state.setText(
            "     / recording all zones (demo)"
            if self.record_state.text().startswith("REC")
            else "REC / recording all zones (demo)"
        )

    def _show_autotune_notice(self) -> None:
        QMessageBox.information(
            self,
            "Simulation only",
            "No command was sent. Real autotune uses relay feedback and requires a safety review.",
        )

    def _show_activation_notice(self) -> None:
        QMessageBox.information(
            self,
            "Simulation only",
            "No command was sent. Per-zone disable behavior must be validated on hardware.",
        )

    def _open_chart_window(self) -> None:
        window = QMainWindow(self)
        window.setWindowTitle("Detached simulated chart - same GUI process")
        plot = self._new_plot()
        window.setCentralWidget(plot)
        window.resize(760, 420)
        window.show()
        self.detached_windows.append(window)

    def _advance_simulation(self) -> None:
        self.tick_count += 1
        value = 30 + math.sin(self.tick_count / 9) * 0.06
        self.history.append(value)
        x = list(range(len(self.history)))
        y = list(self.history)
        for plot in [self.plot, *(window.centralWidget() for window in self.detached_windows if window.isVisible())]:
            if plot is None:
                continue
            curves = plot.listDataItems()
            if curves:
                curves[0].setData(x, y)
                curves[1].setData(x, [self.sp_input.value()] * len(x))


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulated, hardware-isolated Qt GUI prototype")
    parser.add_argument("--smoke-test", action="store_true", help="Create the UI and exit after 1 second")
    args = parser.parse_args()
    app = QApplication(sys.argv)
    app.setStyleSheet(APP_STYLE)
    window = DemoWindow()
    window.show()
    if args.smoke_test:
        QTimer.singleShot(1000, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
