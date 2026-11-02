"""Read-only four-tab shell for the optional Qt GUI."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QMainWindow, QTabWidget, QVBoxLayout, QWidget

from .adapter import QtAdapter


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
        self.setWindowTitle("CN616A Qt GUI — Foundation")
        self.resize(1100, 720)

        root = QWidget(self)
        layout = QVBoxLayout(root)
        self.mode_banner = QLabel(self._mode_text())
        self.mode_banner.setObjectName("modeBanner")
        self.mode_banner.setWordWrap(True)
        layout.addWidget(self.mode_banner)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("mainTabs")
        for title, description in TAB_CONTENT:
            page = QWidget()
            page_layout = QVBoxLayout(page)
            label = QLabel(description)
            label.setWordWrap(True)
            page_layout.addWidget(label)
            page_layout.addStretch(1)
            self.tabs.addTab(page, title)
        layout.addWidget(self.tabs)
        self.setCentralWidget(root)
        self.statusBar().showMessage(self._status_text())

    def _mode_text(self) -> str:
        if self.adapter is None:
            return (
                "DISCONNECTED / READ-ONLY — No endpoint is configured or probed. "
                "No controls are available."
            )
        status = self.adapter.status
        return (
            f"{status.message} — {status.provenance} "
            "All controls are unavailable in this foundation build."
        )

    def _status_text(self) -> str:
        if self.adapter is None:
            return "Disconnected; no service or hardware access attempted."
        return f"Mode: {self.adapter.status.mode} | Read-only"

    def closeEvent(self, event) -> None:
        if self.adapter is not None:
            self.adapter.shutdown()
        event.accept()
