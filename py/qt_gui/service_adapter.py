"""Asynchronous adapter for the existing v1 JSON Lines service and state files."""

from __future__ import annotations

import copy
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from py.gui.history_reader import IncrementalHistoryReader
from py.gui.service_client import ServiceClient, SubmitStatus as ClientSubmitStatus
from py.gui.snapshot_reader import StateSnapshotReader

from .adapter import (
    AdapterHistory,
    AdapterSnapshot,
    AdapterStatus,
    CommandOutcome,
    ControllerIdentity,
    PROFILE_EXECUTION_OPERATIONS,
    READ_ONLY_OPERATIONS,
    RUN_CONTROL_OPERATIONS,
    SERVICE_CONFIGURATION_OPERATIONS,
    SnapshotCategory,
    SubmissionStatus,
    ZoneKey,
    messages_match_profile_zones,
    messages_match_zone,
    new_session_generation,
    normalize_snapshot,
    snapshot_age_seconds,
)


LOGGER = logging.getLogger("cn616a.qt_gui.adapter")
SNAPSHOT_CATEGORIES: tuple[SnapshotCategory, ...] = (
    "telemetry",
    "config",
    "rampsoak",
    "analysis",
    "service_config",
)
_MAX_OUTCOMES = 100


@dataclass(frozen=True, slots=True)
class _PendingSubmission:
    messages: tuple[Mapping[str, object], ...]
    target: ZoneKey | None


class QtServiceAdapter(QObject):
    """Keep sockets and file parsing off the GUI thread; deliver bounded updates on its event loop."""

    snapshot_updated = Signal(object)
    history_updated = Signal(object)
    outcome_received = Signal(object)
    status_changed = Signal(object)

    def __init__(
        self,
        host: str,
        port: int,
        logs_dir: Path,
        *,
        capacity: int = 32,
        timeout_s: float = 2.0,
        poll_interval_ms: int = 100,
        stale_after_s: float = 5.0,
        history_hours: float = 1.0,
        read_only: bool = False,
    ) -> None:
        super().__init__()
        if not host.strip():
            raise ValueError("service host must not be empty")
        if not 1 <= int(port) <= 65535:
            raise ValueError("service port must be in the range 1..65535")
        if poll_interval_ms < 10:
            raise ValueError("poll_interval_ms must be at least 10")
        if stale_after_s <= 0:
            raise ValueError("stale_after_s must be positive")

        self.generation = new_session_generation()
        self.host = host.strip()
        self.port = int(port)
        self.endpoint = f"{self.host}:{self.port}"
        self.logs_dir = Path(logs_dir)
        self.stale_after_s = float(stale_after_s)
        self.read_only_mode = bool(read_only)
        self._closing = False
        self._service_reachable = False
        self._controller_connected: bool | None = None
        self._service_error: str | None = None
        self._target: ControllerIdentity | None = None
        self._identity_warning: str | None = (
            "Waiting for a fresh controller snapshot; writes are disabled."
        )
        self._snapshots: dict[str, AdapterSnapshot] = {}
        self._snapshot_fingerprints: dict[str, tuple[object, ...]] = {}
        self._outcomes: list[CommandOutcome] = []
        self._pending: dict[int, _PendingSubmission] = {}
        self._next_submission = 0
        self._history: AdapterHistory | None = None
        self._history_candidate: dict[str, Any] | None = None
        self._history_revision = 0
        self._status = AdapterStatus(
            mode="service",
            connected=False,
            read_only=True,
            provenance=f"Service snapshots: {self.logs_dir}",
            message=f"CONNECTING — selected endpoint {self.endpoint}; controller identity unknown.",
            endpoint=self.endpoint,
            generation=self.generation,
            identity_warning=self._identity_warning,
        )

        self._snapshot_reader = StateSnapshotReader(self.logs_dir)
        self._history_reader = IncrementalHistoryReader(self.logs_dir)
        self._history_revision = self._history_reader.request(history_hours)
        self._client = ServiceClient(capacity=capacity, timeout_s=timeout_s)
        self._timer = QTimer(self)
        self._timer.setInterval(int(poll_interval_ms))
        self._timer.timeout.connect(self._pump)
        self._timer.start()
        self.refresh_status()

    @property
    def status(self) -> AdapterStatus:
        return self._status

    @property
    def history(self) -> AdapterHistory | None:
        return self._history

    def request_history(self, history_hours: float) -> int:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("QtServiceAdapter.request_history must be called on its Qt thread")
        if self._closing:
            raise RuntimeError("QtServiceAdapter is shut down")
        self._history_revision = self._history_reader.request(history_hours)
        return self._history_revision

    def snapshots(self) -> Mapping[str, AdapterSnapshot]:
        return MappingProxyType(dict(self._snapshots))

    def outcomes(self) -> tuple[CommandOutcome, ...]:
        return tuple(self._outcomes)

    def is_current_generation(self, generation: int) -> bool:
        return not self._closing and generation == self.generation

    def refresh_status(self) -> ClientSubmitStatus:
        if self._closing:
            return "closed"
        return self._client.submit(
            self.host,
            self.port,
            [{"op": "get_status"}],
            self._on_status_result,
            priority=-10,
            coalesce_key="service-status",
        )

    def submit(
        self,
        messages: Sequence[Mapping[str, object]],
        *,
        target: ZoneKey | None = None,
        coalesce_key: str | None = None,
    ) -> SubmissionStatus:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("QtServiceAdapter.submit must be called on its Qt thread")
        if not messages:
            raise ValueError("at least one service message is required")
        if self._closing:
            self._record_immediate_rejection(
                messages,
                target,
                "Adapter is closed; the request was not sent.",
            )
            return "closed"

        copied: list[dict[str, object]] = []
        request_ids: set[str] = set()
        for source in messages:
            message = copy.deepcopy(dict(source))
            operation = message.get("op")
            if not isinstance(operation, str) or not operation:
                return self._reject(messages, target, "Every service message must include an operation.")
            if "unit" in message:
                return self._reject(
                    messages,
                    target,
                    "The v1 service does not route commands using a unit field.",
                )
            request_id = message.get("id")
            if request_id is None:
                request_id = uuid.uuid4().hex
                message["id"] = request_id
            if not isinstance(request_id, str) or not request_id or request_id in request_ids:
                return self._reject(messages, target, "Request IDs must be unique non-empty strings.")
            request_ids.add(request_id)
            copied.append(message)

        mutation = any(str(message["op"]) not in READ_ONLY_OPERATIONS for message in copied)
        run_control = bool(copied) and all(
            str(message["op"]) in RUN_CONTROL_OPERATIONS for message in copied
        )
        service_configuration = bool(copied) and all(
            str(message["op"]) in SERVICE_CONFIGURATION_OPERATIONS for message in copied
        )
        profile_execution = bool(copied) and all(
            str(message["op"]) in PROFILE_EXECUTION_OPERATIONS for message in copied
        )
        if run_control and target is not None:
            return self._reject(
                messages,
                target,
                "Recording and schedule commands are service-level operations, not zone commands.",
            )
        if service_configuration and target is not None:
            return self._reject(
                messages,
                target,
                "Configuration and connection commands are service-level operations, not zone commands.",
            )
        if profile_execution and (
            target is None or not messages_match_profile_zones(copied, target)
        ):
            return self._reject(
                messages,
                target,
                "Profile start/stop requires a verified controller target and an explicit valid zone list.",
            )
        if target is not None and not self._target_matches(target):
            return self._reject(messages, target, "Command target is unknown or belongs to another controller.")
        if mutation:
            if service_configuration:
                if self.read_only_mode:
                    return self._reject(messages, target, "Service is configured read-only; no request was sent.")
                if not self._service_reachable:
                    return self._reject(messages, target, "Service endpoint is not reachable; no request was sent.")
            elif self._status.read_only:
                return self._reject(messages, target, "Service/controller is not ready for writes.")
            if not run_control and not service_configuration and target is None:
                return self._reject(messages, target, "Controller identity is unknown; writes are disabled.")
            if not run_control and not service_configuration:
                payload_matches_target = (
                    messages_match_profile_zones(copied, target)
                    if profile_execution and target is not None
                    else self._messages_match_target(copied, target)
                )
                if not payload_matches_target:
                    return self._reject(messages, target, "Command payload does not match the selected zone target.")

        self._next_submission += 1
        submission_id = self._next_submission
        callback = lambda result: self._on_submission_result(submission_id, result)
        submit_status = self._client.submit(
            self.host,
            self.port,
            copied,
            callback,
            coalesce_key=coalesce_key,
        )
        if submit_status == "submitted":
            self._pending[submission_id] = _PendingSubmission(tuple(copied), target)
        elif submit_status in ("coalesced", "full", "closed"):
            message = {
                "coalesced": "Service request was coalesced with an existing request.",
                "full": "Service request queue is full.",
                "closed": "Service client is closed.",
            }[submit_status]
            self._record_immediate_rejection(
                copied,
                target,
                message,
            )
        return submit_status

    def _target_matches(self, target: ZoneKey) -> bool:
        identity = self._target
        return (
            identity is not None
            and target.bus_id == identity.key.bus_id
            and target.controller_id == identity.key.controller_id
            and 1 <= target.zone_id <= 6
        )

    @staticmethod
    def _messages_match_target(
        messages: Sequence[Mapping[str, object]],
        target: ZoneKey,
    ) -> bool:
        return messages_match_zone(messages, target)

    def _reject(
        self,
        messages: Sequence[Mapping[str, object]],
        target: ZoneKey | None,
        reason: str,
    ) -> SubmissionStatus:
        self._record_immediate_rejection(messages, target, reason)
        return "rejected"

    def _record_immediate_rejection(
        self,
        messages: Sequence[Mapping[str, object]],
        target: ZoneKey | None,
        reason: str,
    ) -> None:
        for message in messages:
            self._record_outcome(
                CommandOutcome(
                    operation=str(message.get("op", "unknown")),
                    result="not_sent",
                    message=reason,
                    target=target,
                    request_id=str(message["id"]) if "id" in message else None,
                    generation=self.generation,
                    endpoint=self.endpoint,
                    created_at=datetime.now(timezone.utc),
                )
            )

    def _on_status_result(self, result: dict[str, Any]) -> None:
        outcomes = result.get("outcomes")
        first = outcomes[0] if isinstance(outcomes, list) and outcomes else None
        if not isinstance(first, dict):
            self._service_reachable = False
            self._controller_connected = None
            self._service_error = "Service status request returned no outcome."
        elif first.get("state") == "confirmed_success":
            response = first.get("response")
            self._service_reachable = True
            self._controller_connected = (
                response.get("connected") is True if isinstance(response, dict) else None
            )
            self._service_error = None
        else:
            self._service_reachable = first.get("state") == "confirmed_failure"
            self._controller_connected = None
            self._service_error = str(first.get("error", first.get("state", "unknown service error")))
        self._refresh_status()

    def _on_submission_result(self, submission_id: int, result: dict[str, Any]) -> None:
        pending = self._pending.pop(submission_id, None)
        if pending is None:
            return
        results = result.get("outcomes")
        if not isinstance(results, list):
            results = []
        for index, message in enumerate(pending.messages):
            item = results[index] if index < len(results) else None
            if not isinstance(item, dict):
                item = {
                    "id": message.get("id"),
                    "op": message.get("op", "unknown"),
                    "state": "unknown",
                    "error": "Service client returned an incomplete result sequence.",
                }
            response = item.get("response")
            state = item.get("state")
            if state not in {"confirmed_success", "confirmed_failure", "not_sent", "unknown"}:
                state = "unknown"
            if state == "confirmed_success" and str(message.get("op")) not in READ_ONLY_OPERATIONS:
                if str(message.get("op")) in RUN_CONTROL_OPERATIONS:
                    detail = "Service acknowledged; authoritative recording status has not been refreshed."
                else:
                    detail = "Service acknowledged; controller readback has not been verified."
            elif state == "confirmed_success":
                detail = "Service response received."
            else:
                detail = str(item.get("error", state))
            self._record_outcome(
                CommandOutcome(
                    operation=str(item.get("op", message.get("op", "unknown"))),
                    result=state,
                    message=detail,
                    target=pending.target,
                    request_id=str(item.get("id")) if item.get("id") is not None else None,
                    response=MappingProxyType(copy.deepcopy(response)) if isinstance(response, dict) else None,
                    generation=self.generation,
                    endpoint=self.endpoint,
                    created_at=datetime.now(timezone.utc),
                )
            )

    def _record_outcome(self, outcome: CommandOutcome) -> None:
        self._outcomes.append(outcome)
        del self._outcomes[:-_MAX_OUTCOMES]
        self.outcome_received.emit(outcome)

    def _pump(self) -> None:
        if self._closing:
            return
        self._client.poll_results()
        self._refresh_snapshots()
        self._refresh_history()

    def _refresh_snapshots(self) -> None:
        raw_snapshots = self._snapshot_reader.snapshots(
            *SNAPSHOT_CATEGORIES,
            stale_after_s=self.stale_after_s,
        )
        refreshed: dict[str, AdapterSnapshot] = {}
        changed: list[AdapterSnapshot] = []
        for category in SNAPSHOT_CATEGORIES:
            raw = raw_snapshots[category]
            view = normalize_snapshot(
                category,
                raw.data,
                age_s=raw.age_s,
                stale=raw.stale,
                error=raw.error,
                modified_at=raw.modified_at,
                read_at=raw.read_at,
                generation=self.generation,
            )
            age_s = snapshot_age_seconds(view)
            view = replace(
                view,
                age_s=age_s,
                stale=view.stale or (age_s is not None and age_s > self.stale_after_s),
            )
            refreshed[category] = view
            fingerprint = (
                view.read_at,
                view.modified_at,
                view.error,
                view.stale,
                view.identity,
            )
            if self._snapshot_fingerprints.get(category) != fingerprint:
                self._snapshot_fingerprints[category] = fingerprint
                changed.append(view)
        self._snapshots = refreshed
        self._reconcile_identity()
        for view in changed:
            self.snapshot_updated.emit(view)

    def _reconcile_identity(self) -> None:
        previous_target = self._target
        identities = {
            snapshot.identity
            for snapshot in self._snapshots.values()
            if snapshot.identity is not None and snapshot.data is not None
        }
        telemetry = self._snapshots.get("telemetry")
        warning = None
        target = None
        if len(identities) > 1:
            warning = "Snapshot categories disagree on controller identity; writes are disabled."
        elif identities:
            target = next(iter(identities))
        else:
            warning = "Controller identity is missing or ambiguous; writes are disabled."
        if telemetry is None or telemetry.error or telemetry.stale:
            warning = "A fresh telemetry identity is required before writes are enabled."
        elif telemetry.identity is None:
            warning = "Telemetry identity is missing or ambiguous; writes are disabled."
        elif target is None and warning is None:
            warning = "Controller identity is missing or ambiguous; writes are disabled."

        self._target = target
        self._identity_warning = warning
        self._refresh_status()
        if target != previous_target and self._history_candidate is not None:
            self._publish_history()

    def _refresh_status(self) -> None:
        writable = (
            not self.read_only_mode
            and self._service_reachable
            and self._controller_connected is True
            and self._target is not None
            and self._identity_warning is None
            and not self._closing
        )
        if not self._service_reachable and self._service_error is None:
            message = f"CONNECTING — selected endpoint {self.endpoint}; controller identity unknown."
        elif not self._service_reachable:
            message = f"SERVICE UNAVAILABLE — {self.endpoint}"
            if self._service_error:
                message = f"{message}: {self._service_error}"
        elif self._controller_connected is False:
            message = "SERVICE REACHABLE — controller is disconnected."
        elif self._service_error:
            message = f"SERVICE REACHABLE — controller status unavailable: {self._service_error}"
        elif self._controller_connected is not True:
            message = "SERVICE REACHABLE — controller status is unknown."
        elif self._identity_warning:
            message = f"CONTROLLER CONNECTED — {self._identity_warning}"
        elif self.read_only_mode:
            message = (
                "SERVICE AND CONTROLLER CONNECTED — READ-ONLY MODE; "
                "all writes are disabled."
            )
        elif self._target is not None:
            message = (
                "SERVICE AND CONTROLLER CONNECTED — "
                f"{self._target.key.bus_id}, controller {self._target.key.controller_id}."
            )
        else:
            message = "CONTROLLER IDENTITY UNKNOWN — writes are disabled."
        updated = AdapterStatus(
            mode="service",
            connected=self._service_reachable,
            read_only=not writable,
            provenance=f"Service endpoint {self.endpoint}; snapshots from {self.logs_dir}",
            message=message,
            target=self._target,
            endpoint=self.endpoint,
            generation=self.generation,
            controller_connected=self._controller_connected,
            identity_warning=self._identity_warning,
        )
        if updated != self._status:
            self._status = updated
            self.status_changed.emit(updated)

    def _refresh_history(self) -> None:
        for result in self._history_reader.poll_results():
            revision = result.get("revision")
            if not isinstance(revision, int) or revision < self._history_revision:
                continue
            self._history_candidate = copy.deepcopy(result)
            self._publish_history()

    def _publish_history(self) -> None:
        result = self._history_candidate
        if result is None:
            return
        source_identity = result.get("source_identity")
        if not (
            isinstance(source_identity, tuple)
            and len(source_identity) == 2
            and isinstance(source_identity[0], str)
            and isinstance(source_identity[1], int)
        ):
            source_identity = None
        error = "; ".join(str(item) for item in result.get("errors", [])) or None
        data: Mapping[str, object] | None = MappingProxyType(copy.deepcopy(result))
        if result.get("source_identity_conflict"):
            error = "History contains multiple controller identities; history is withheld."
            data = None
        elif result.get("source_identity_missing"):
            error = "History contains records with unknown controller identity; history is withheld."
            data = None
        elif self._target is None:
            error = "Controller identity is unavailable; history is withheld."
            data = None
        else:
            expected_identity = (
                self._target.key.bus_id,
                self._target.modbus_address,
            )
            if source_identity != expected_identity:
                error = "History source identity does not match the active controller; history is withheld."
                data = None
        self._history = AdapterHistory(
            generation=self.generation,
            endpoint=self.endpoint,
            identity=self._target if data is not None else None,
            data=data,
            error=error,
            source_identity=source_identity,
            revision=int(result.get("revision", 0)),
        )
        self.history_updated.emit(self._history)

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._timer.stop()
        self._client.poll_results()
        self._client.shutdown(wait=True)
        self._client.poll_results()
        for pending in tuple(self._pending.values()):
            for message in pending.messages:
                self._record_outcome(
                    CommandOutcome(
                        operation=str(message.get("op", "unknown")),
                        result="unknown",
                        message="Adapter closed before a definitive service outcome; do not retry automatically.",
                        target=pending.target,
                        request_id=str(message["id"]) if "id" in message else None,
                        generation=self.generation,
                        endpoint=self.endpoint,
                        created_at=datetime.now(timezone.utc),
                    )
                )
        self._pending.clear()
        self._history_reader.shutdown()
        self._snapshot_reader.shutdown()
        self._refresh_status()
