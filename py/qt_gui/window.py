"""Qt control-desk shell and read-only overview."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMainWindow, QTabWidget, QVBoxLayout, QWidget

from .adapter import QtAdapter
from .overview import OverviewPage


WINDOW_STYLE = """
QWidget { color: #172b3d; font-family: "Segoe UI"; font-size: 9pt; }
QWidget#appRoot { background: #e8f0f8; }
QFrame#topBar { background: #0a4175; border-radius: 6px; }
QLabel#brandIcon { background: #1d5e98; color: white; border: 1px solid #6fa7d3;
    border-radius: 6px; padding: 8px; font-size: 11pt; font-weight: 700; }
QLabel#appTitle { color: white; font-size: 14pt; font-weight: 700; }
QLabel#appSubtitle { color: #c7dcef; font-size: 8pt; font-weight: 600; }
QLabel#modeBanner { color: #fff0c8; font-size: 9pt; font-weight: 600; padding: 4px; }
QTabWidget::pane { border: 1px solid #bdd0e2; border-radius: 4px; background: #e8f0f8; }
QTabBar::tab { background: #dce9f7; color: #214a71; padding: 9px 20px;
    margin-right: 3px; border-top-left-radius: 4px; border-top-right-radius: 4px; font-weight: 700; }
QTabBar::tab:selected { background: #0867cb; color: white; }
QTabBar::tab:hover:!selected { background: #cbdff3; }
QLabel#overviewTitle { color: #133b5e; font-size: 16pt; font-weight: 700; }
QLabel#overviewSubtitle { color: #60798f; }
QLabel#viewOnly { background: #e3edf8; color: #315d82; border-radius: 4px;
    padding: 5px 8px; font-size: 8pt; font-weight: 700; }
QFrame#overviewCard { background: white; border: 1px solid #b9cde0; border-radius: 4px; }
QLabel#cardTitle { background: #164b76; color: white; padding: 8px 10px;
    font-size: 9pt; font-weight: 700; }
QLabel#cardTitle[tone="teal"] { background: #076f80; }
QLabel#cardTitle[tone="blue"] { background: #0867cb; }
QTableView#zoneTable { background: white; alternate-background-color: #f1f6fc;
    gridline-color: #e0eaf3; border: 0; selection-background-color: #dcedff;
    selection-color: #103c65; }
QTableView#zoneTable::item { padding: 3px 6px; }
QHeaderView::section { background: #e1edf9; color: #214d75; padding: 6px;
    border: 0; font-size: 8pt; font-weight: 700; }
QLabel#visualKey { color: #395c7c; background: #edf4fb; padding: 5px; font-size: 8pt; }
QLabel#placeholderTitle { color: #345b7a; font-size: 11pt; font-weight: 700; }
QLabel#healthSummary { color: #075965; background: #e8f5f7; padding: 7px; font-weight: 700; }
QLabel#sourceIdentity { color: #1d4b70; font-weight: 700; }
QLabel#sourceDetails { color: #526b82; font-size: 8pt; }
QLabel#focusIdentity { color: #0758b0; background: #e7f2ff; padding: 8px; font-weight: 700; }
QLabel#focusMeasurements { color: #173d61; background: #f3f8fd; padding: 7px; font-weight: 700; }
QLabel#fieldLabel { color: #395c7c; font-size: 8pt; font-weight: 700; }
QLabel#readOnlyNote { color: #6a5a32; background: #fff6df; padding: 7px; font-size: 8pt; }
QScrollArea#overviewRail, QScrollArea#overviewWorkspace { border: 0; background: transparent; }
QStatusBar { background: #f6faff; color: #526b82; font-size: 8pt; }
"""


TAB_CONTENT = (
    (
        "Overview",
        "Telemetry and controller status will be migrated in a later step.",
    ),
    (
        "Configuration",
        "Configuration editing is not migrated. No settings can be changed.",
    ),
    (
        "Ramp/Soak",
        "Profile editing and execution are not migrated. No commands can be sent.",
    ),
    (
        "Logs",
        "Log browsing is not migrated. Replay files remain in the temporary session.",
    ),
)


class QtMainWindow(QMainWindow):
    def __init__(self, adapter: QtAdapter | None = None) -> None:
        super().__init__()
        self.adapter = adapter
        self._adapter_generation = adapter.generation if adapter is not None else None
        self._history_poll_timer: QTimer | None = None
        self.snapshot_models = {}
        self.history_snapshot = None
        self.command_outcomes = []
        self.setWindowTitle("CN616A Control Desk — Preview")
        self.resize(1100, 720)
        self.setStyleSheet(WINDOW_STYLE)

        root = QWidget(self)
        root.setObjectName("appRoot")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        top_bar = QFrame()
        top_bar.setObjectName("topBar")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(12, 8, 12, 8)
        brand_icon = QLabel("CN")
        brand_icon.setObjectName("brandIcon")
        top_layout.addWidget(brand_icon)
        brand = QVBoxLayout()
        brand.setSpacing(1)
        app_title = QLabel("CN616A Control Desk")
        app_title.setObjectName("appTitle")
        app_subtitle = QLabel("TEMPERATURE CONTROLLER / PREVIEW")
        app_subtitle.setObjectName("appSubtitle")
        brand.addWidget(app_title)
        brand.addWidget(app_subtitle)
        top_layout.addLayout(brand)
        top_layout.addStretch(1)
        self.mode_banner = QLabel(self._mode_text())
        self.mode_banner.setObjectName("modeBanner")
        self.mode_banner.setTextFormat(Qt.TextFormat.PlainText)
        self.mode_banner.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.mode_banner.setWordWrap(True)
        self.mode_banner.setMaximumWidth(700)
        top_layout.addWidget(self.mode_banner, 1)
        layout.addWidget(top_bar)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("mainTabs")
        stale_after = getattr(adapter, "stale_after_s", 5.0) if adapter is not None else 5.0
        if isinstance(stale_after, bool) or not isinstance(stale_after, (int, float)):
            raise TypeError("adapter stale_after_s must be numeric")
        history_requester = getattr(adapter, "request_history", None)
        if not callable(history_requester):
            history_requester = None
        self.overview = OverviewPage(
            stale_after_s=float(stale_after),
            history_requester=history_requester,
        )
        self.tabs.addTab(self.overview, "Overview")
        for title, description in TAB_CONTENT[1:]:
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(18, 18, 18, 18)
            label = QLabel(description)
            label.setWordWrap(True)
            page_layout.addWidget(label)
            page_layout.addStretch(1)
            self.tabs.addTab(page, title)
        layout.addWidget(self.tabs)
        self.setCentralWidget(root)
        self.statusBar().showMessage(self._status_text())
        self.overview.set_status(adapter.status if adapter is not None else None)
        if adapter is not None:
            poll_history = getattr(adapter, "poll_history", None)
            if callable(poll_history):
                self._history_poll_timer = QTimer(self)
                self._history_poll_timer.setInterval(100)
                self._history_poll_timer.timeout.connect(self._poll_adapter_history)
                self._history_poll_timer.start()
            status_changed = getattr(adapter, "status_changed", None)
            snapshot_updated = getattr(adapter, "snapshot_updated", None)
            history_updated = getattr(adapter, "history_updated", None)
            outcome_received = getattr(adapter, "outcome_received", None)
            if status_changed is not None:
                status_changed.connect(self._on_adapter_status)
            if snapshot_updated is not None:
                snapshot_updated.connect(self._on_snapshot_updated)
            if history_updated is not None:
                history_updated.connect(self._on_history_updated)
            if outcome_received is not None:
                outcome_received.connect(self._on_outcome_received)
            for snapshot in adapter.snapshots().values():
                self._on_snapshot_updated(snapshot)
            history = getattr(adapter, "history", None)
            if history is not None:
                self._on_history_updated(history)

    def _poll_adapter_history(self) -> None:
        if self.adapter is None:
            return
        poll_history = getattr(self.adapter, "poll_history", None)
        if not callable(poll_history):
            return
        history = poll_history()
        if history is not None and history is not self.history_snapshot:
            self._on_history_updated(history)

    def _mode_text(self) -> str:
        if self.adapter is None:
            return (
                "DISCONNECTED / READ-ONLY — No endpoint is configured or probed. "
                "No controls are available."
            )
        status = self.adapter.status
        suffix = " READ-ONLY" if status.read_only else ""
        return f"{status.message} — {status.provenance}{suffix}"

    def _status_text(self) -> str:
        if self.adapter is None:
            return "Disconnected; no service or hardware access attempted."
        return self.adapter.status.message

    def _is_current_generation(self, generation: int) -> bool:
        return (
            self.adapter is not None
            and generation == self._adapter_generation
            and self.adapter.is_current_generation(generation)
        )

    def _on_adapter_status(self, status) -> None:
        if not self._is_current_generation(status.generation):
            return
        suffix = " READ-ONLY" if status.read_only else ""
        self.mode_banner.setText(f"{status.message} — {status.provenance}{suffix}")
        self.statusBar().showMessage(status.message)
        self.overview.set_status(status)

    def _on_snapshot_updated(self, snapshot) -> None:
        if self._is_current_generation(snapshot.generation):
            self.snapshot_models[snapshot.category] = snapshot
            self.overview.set_snapshot(snapshot)

    def _on_history_updated(self, history) -> None:
        if self._is_current_generation(history.generation):
            self.history_snapshot = history
            self.overview.set_history(history)

    def _on_outcome_received(self, outcome) -> None:
        if not self._is_current_generation(outcome.generation):
            return
        self.command_outcomes.append(outcome)
        del self.command_outcomes[:-100]
        self.statusBar().showMessage(f"{outcome.operation}: {outcome.result} — {outcome.message}")

    def closeEvent(self, event) -> None:
        if self._history_poll_timer is not None:
            self._history_poll_timer.stop()
        self.overview.shutdown()
        if self.adapter is not None:
            self.adapter.shutdown()
        event.accept()
