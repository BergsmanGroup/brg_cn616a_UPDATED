"""Service-authoritative recording and scheduled-stop controls for the Qt overview."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from py.gui.run_schedule import (
    format_elapsed,
    format_last_data_display,
    format_schedule_display,
    parse_pacific_schedule,
)

from .adapter import AdapterStatus, CommandOutcome, SubmissionStatus


RunSubmitter = Callable[
    [Sequence[Mapping[str, object]], str | None],
    SubmissionStatus,
]
RecoverySimulator = Callable[[], Mapping[str, object] | None]


class RunRecordingCard(QWidget):
    """Run recording is distinct from controller Run/Idle and profile execution."""

    def __init__(
        self,
        submitter: RunSubmitter | None,
        *,
        recovery_simulator: RecoverySimulator | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._submitter = submitter
        self._recovery_simulator = recovery_simulator
        self._adapter_status: AdapterStatus | None = None
        self._run_status: dict[str, object] | None = None
        self._status_error: str | None = None
        self._status_pending = False
        self._mutation_pending: str | None = None
        self._last_status_received: float | None = None
        self._elapsed_anchor: float | None = None
        self._elapsed_received_at: float | None = None
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._sync_controls()
        QTimer.singleShot(0, self.poll_status)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(7)
        self.source_label = QLabel()
        self.source_label.setObjectName("recordingSource")
        self.source_label.setWordWrap(True)
        layout.addWidget(self.source_label)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)
        self.directory_entry = QLineEdit()
        self.directory_entry.setObjectName("recordingDirectory")
        directory_row = QHBoxLayout()
        directory_row.addWidget(self.directory_entry, 1)
        self.browse_button = QPushButton("Browse…")
        self.browse_button.setObjectName("recordingBrowse")
        self.browse_button.clicked.connect(self._browse)
        directory_row.addWidget(self.browse_button)
        form.addRow("Data directory", directory_row)
        self.name_entry = QLineEdit()
        self.name_entry.setObjectName("recordingName")
        form.addRow("Recording name (optional)", self.name_entry)
        layout.addLayout(form)

        actions = QHBoxLayout()
        self.start_button = QPushButton("Start recording")
        self.start_button.setObjectName("startRecording")
        self.stop_button = QPushButton("Stop recording")
        self.stop_button.setObjectName("stopRecording")
        self.start_button.clicked.connect(self.start_recording)
        self.stop_button.clicked.connect(self.stop_recording)
        actions.addWidget(self.start_button)
        actions.addWidget(self.stop_button)
        layout.addLayout(actions)

        schedule_form = QFormLayout()
        schedule_form.setContentsMargins(0, 0, 0, 0)
        schedule_form.setSpacing(6)
        self.schedule_entry = QLineEdit()
        self.schedule_entry.setObjectName("recordingStopAtPacific")
        self.schedule_entry.setPlaceholderText("YYYY-MM-DD HH:MM[:SS]")
        schedule_form.addRow("Scheduled stop (Pacific time)", self.schedule_entry)
        layout.addLayout(schedule_form)
        schedule_actions = QHBoxLayout()
        self.enable_schedule_button = QPushButton("Set scheduled stop")
        self.enable_schedule_button.setObjectName("setRecordingSchedule")
        self.disable_schedule_button = QPushButton("Disable scheduled stop")
        self.disable_schedule_button.setObjectName("disableRecordingSchedule")
        self.enable_schedule_button.clicked.connect(self.set_schedule)
        self.disable_schedule_button.clicked.connect(self.disable_schedule)
        schedule_actions.addWidget(self.enable_schedule_button)
        schedule_actions.addWidget(self.disable_schedule_button)
        layout.addLayout(schedule_actions)

        self.status_label = QLabel("Recording status unknown — waiting for service status.")
        self.status_label.setObjectName("recordingStatus")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.feedback_label = QLabel("No recording command sent.")
        self.feedback_label.setObjectName("recordingFeedback")
        self.feedback_label.setWordWrap(True)
        layout.addWidget(self.feedback_label)

        self.recovery_button: QPushButton | None = None
        if self._recovery_simulator is not None:
            self.recovery_button = QPushButton("Simulate recovered interruption")
            self.recovery_button.setObjectName("simulateRunRecovery")
            self.recovery_button.clicked.connect(self._simulate_recovery)
            layout.addWidget(self.recovery_button)

    def set_adapter_status(self, status: AdapterStatus | None) -> None:
        self._adapter_status = status
        offline = status is not None and status.mode == "offline-replay"
        if offline:
            self.source_label.setText(
                "SIMULATED SOURCE — files are temporary; the entered directory is not written."
            )
        else:
            self.source_label.setText(
                "Data collection only — this does not start/stop controller heating or a profile."
            )
        self._sync_controls()

    def _can_submit(self) -> bool:
        status = self._adapter_status
        return (
            self._submitter is not None
            and status is not None
            and (
                status.mode == "offline-replay"
                or (status.connected and not status.read_only)
            )
        )

    def _sync_controls(self) -> None:
        can_submit = self._can_submit() and self._mutation_pending is None
        known_status = self._run_status is not None and self._status_error is None
        active = bool(self._run_status.get("run_active")) if self._run_status else False
        self.browse_button.setEnabled(self._mutation_pending is None)
        self.directory_entry.setEnabled(self._mutation_pending is None)
        self.name_entry.setEnabled(self._mutation_pending is None)
        self.schedule_entry.setEnabled(self._mutation_pending is None)
        self.start_button.setEnabled(bool(can_submit and known_status and not active))
        self.stop_button.setEnabled(bool(can_submit and known_status and active))
        self.enable_schedule_button.setEnabled(bool(can_submit))
        self.disable_schedule_button.setEnabled(bool(can_submit))
        if self.recovery_button is not None:
            self.recovery_button.setEnabled(
                self._adapter_status is not None
                and self._adapter_status.mode == "offline-replay"
                and self._mutation_pending is None
            )

    def _browse(self) -> None:
        try:
            chosen = QFileDialog.getExistingDirectory(self, "Choose recording directory")
        except (OSError, RuntimeError) as exc:
            self.feedback_label.setText(
                f"Directory picker unavailable ({exc}); enter a path manually."
            )
            return
        if chosen:
            self.directory_entry.setText(chosen)

    def _submit_mutation(self, operation: str, message: Mapping[str, object]) -> None:
        if not self._can_submit():
            self.feedback_label.setText(
                "Recording command not sent: service/controller is unavailable or read-only."
            )
            return
        if self._mutation_pending is not None:
            return
        if self._submitter is None:
            self.feedback_label.setText("Recording command not sent: no service adapter is configured.")
            return
        self._mutation_pending = operation
        self.feedback_label.setText(
            f"{operation} request pending — recording state is not assumed."
        )
        self._sync_controls()
        try:
            result = self._submitter((message,), None)
        except (OSError, RuntimeError, ValueError) as exc:
            self._mutation_pending = None
            self.feedback_label.setText(f"{operation} request could not be queued: {exc}")
            self._sync_controls()
            return
        if result not in {"submitted", "coalesced", "mocked"}:
            if self._mutation_pending == operation:
                self._mutation_pending = None
                self.feedback_label.setText(
                    f"{operation} request was not queued ({result}); no state change is assumed."
                )
                self._sync_controls()

    def start_recording(self) -> None:
        run_dir = self.directory_entry.text().strip()
        if not run_dir:
            self.feedback_label.setText("Enter or choose a data directory before starting.")
            return
        run_name = self.name_entry.text().strip()
        self._submit_mutation(
            "start_run",
            {
                "op": "start_run",
                "run_dir": str(Path(run_dir)),
                "run_name": run_name or None,
            },
        )

    def stop_recording(self) -> None:
        self._submit_mutation("stop_run", {"op": "stop_run"})

    def set_schedule(self) -> None:
        stop_at = parse_pacific_schedule(self.schedule_entry.text())
        if stop_at is None:
            self.feedback_label.setText(
                "Enter a valid Pacific time as YYYY-MM-DD HH:MM or YYYY-MM-DD HH:MM:SS."
            )
            return
        self._submit_mutation(
            "set_run_schedule",
            {
                "op": "set_run_schedule",
                "enabled": True,
                "stop_at_iso": stop_at.isoformat(),
            },
        )

    def disable_schedule(self) -> None:
        self._submit_mutation(
            "set_run_schedule",
            {"op": "set_run_schedule", "enabled": False, "stop_at_iso": None},
        )

    def _simulate_recovery(self) -> None:
        if self._recovery_simulator is None:
            return
        try:
            response = self._recovery_simulator()
        except (OSError, RuntimeError, ValueError) as exc:
            self.feedback_label.setText(f"Recovery replay failed: {exc}")
            return
        if response is None:
            self.feedback_label.setText("No simulated recovery response was available.")
            return
        self._accept_status(response)
        self.feedback_label.setText(
            "SIMULATED ONLY — recovery state came from the temporary replay backend."
        )

    def poll_status(self) -> None:
        if self._status_pending or self._submitter is None:
            return
        self._status_pending = True
        self._sync_controls()
        try:
            result = self._submitter(
                ({"op": "get_run_status"},),
                "qt-run-status",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            self._status_pending = False
            self._status_error = f"Unable to request recording status: {exc}"
            self._render_status()
            self._sync_controls()
            return
        if result not in {"submitted", "coalesced", "mocked"} and self._status_pending:
            self._status_pending = False
            self._status_error = f"Recording status request {result}; state is unknown."
            self._render_status()
            self._sync_controls()

    def set_command_outcome(self, outcome: CommandOutcome) -> None:
        if outcome.operation == "get_run_status":
            self._status_pending = False
            response = outcome.response
            if (
                outcome.result in {"confirmed_success", "mock"}
                and isinstance(response, Mapping)
                and response.get("ok") is True
            ):
                self._accept_status(response)
            else:
                self._status_error = (
                    "Recording status is unknown — "
                    f"{outcome.message or outcome.result}. No retry or state change is assumed."
                )
                self._render_status()
            self._sync_controls()
            return
        if outcome.operation not in {"start_run", "stop_run", "set_run_schedule"}:
            return
        self._mutation_pending = None
        if outcome.result in {"confirmed_success", "mock"}:
            self.feedback_label.setText(
                f"{outcome.operation} acknowledged by "
                f"{'the simulated replay' if outcome.result == 'mock' else 'the service'}; "
                "waiting for authoritative recording status."
            )
        else:
            self.feedback_label.setText(
                f"{outcome.operation} {outcome.result}: {outcome.message}. "
                "No automatic retry was made."
            )
        self._status_error = "Waiting for a fresh recording status after the command outcome."
        self._sync_controls()
        self.poll_status()

    def _accept_status(self, response: Mapping[str, object]) -> None:
        self._run_status = dict(response)
        self._status_error = None
        self._last_status_received = time.monotonic()
        elapsed = response.get("run_elapsed_s")
        self._elapsed_anchor = (
            float(elapsed)
            if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
            else None
        )
        self._elapsed_received_at = self._last_status_received
        self._render_status()
        self._sync_controls()

    def _tick(self) -> None:
        self.poll_status()
        self._render_status()

    def _render_status(self) -> None:
        if self._run_status is None:
            text = self._status_error or "Recording status unknown — waiting for service status."
            self.status_label.setText(text)
            return
        status = self._run_status
        active = status.get("run_active") is True
        prefix = "Last confirmed state — " if self._status_error else ""
        if not active:
            line = f"{prefix}Recording INACTIVE"
        else:
            elapsed = self._elapsed_anchor
            if elapsed is not None and self._elapsed_received_at is not None:
                elapsed += max(0.0, time.monotonic() - self._elapsed_received_at)
                elapsed_text = format_elapsed(elapsed)
            else:
                elapsed_text = "unknown"
            run_name = str(status.get("run_name") or status.get("run_dir") or "unnamed recording")
            run_dir = str(status.get("run_dir") or "directory unknown")
            line = (
                f"{prefix}Recording ACTIVE — {run_name}\n"
                f"Directory: {run_dir} · elapsed {elapsed_text}"
            )
            if status.get("run_recovered") is True:
                interruption = status.get("run_interruption_s")
                duration = (
                    f"{float(interruption):.1f} s"
                    if isinstance(interruption, (int, float)) and not isinstance(interruption, bool)
                    else "unknown duration"
                )
                line += f"\nRecovered after a {duration} interruption; collection continues."
        last_data = format_last_data_display(
            status.get("run_last_data_ts")
            if isinstance(status.get("run_last_data_ts"), str)
            else None
        )
        schedule = (
            f"armed for {format_schedule_display(str(status.get('run_schedule_stop_at')))}"
            if status.get("run_schedule_enabled") is True
            else "disabled"
        )
        lines = [line, f"Last data: {last_data}", f"Scheduled stop: {schedule}"]
        if self._status_error:
            lines.append(self._status_error)
        elif self._last_status_received is not None:
            age = time.monotonic() - self._last_status_received
            if age > 5.0:
                lines.append(f"Status is stale ({int(age)} s since last response).")
        self.status_label.setText("\n".join(lines))

    def shutdown(self) -> None:
        self._timer.stop()
