"""Temporary service-shaped replay data derived from the immutable raw baseline."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from py.gui.state_values import get_numeric, get_zone, get_zones
from py.gui.history_reader import IncrementalHistoryReader

from .adapter import (
    AdapterHistory,
    AdapterSnapshot,
    AdapterStatus,
    CommandOutcome,
    ControllerIdentity,
    ControllerKey,
    READ_ONLY_OPERATIONS,
    RUN_CONTROL_OPERATIONS,
    SERVICE_CONFIGURATION_OPERATIONS,
    SnapshotCategory,
    SubmissionStatus,
    ZoneKey,
    messages_match_zone,
    new_session_generation,
    normalize_snapshot,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_DIR = REPO_ROOT / "tests" / "fixtures" / "cn616a-baseline-v1"
ZONE_IDS = tuple(range(1, 7))
SNAPSHOT_FILES: dict[SnapshotCategory, str] = {
    "telemetry": "cn616a_telemetry_state.json",
    "config": "cn616a_config_state.json",
    "rampsoak": "cn616a_rampsoak_state.json",
    "analysis": "cn616a_analysis_state.json",
    "service_config": "cn616a_service_config_state.json",
}


def _adapt_baseline() -> Any:
    helper_path = REPO_ROOT / "tests" / "baseline_reference.py"
    module_name = "_cn616a_qt_baseline_reference"
    spec = importlib.util.spec_from_file_location(module_name, helper_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load baseline replay helper: {helper_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        return module.adapt_reference(BASELINE_DIR)
    finally:
        sys.modules.pop(module_name, None)


def _mock_analysis(telemetry: Mapping[str, Any]) -> dict[str, Any]:
    zones = get_zones(dict(telemetry))
    result: dict[str, Any] = {}
    for zone_id in ZONE_IDS:
        values = get_zone(zones, zone_id)
        pv = get_numeric(values, "pv_c", "pv", "PV", "process_value_c", "process_value")
        setpoint = get_numeric(
            values, "sp_abs_c", "sp_abs", "sp_c", "sp", "SP_abs", "SP", "setpoint_c", "setpoint"
        )
        result[str(zone_id)] = {
            "avg_abs_error_c": round(abs(pv - setpoint), 3)
            if pv is not None and setpoint is not None
            else None,
            "equilibrium": False,
            "point_count": 1,
        }
    return result


class OfflineReplaySession:
    """Owns generated snapshots and logs; shutdown removes its temporary files."""

    def __init__(self) -> None:
        self.generation = new_session_generation()
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="cn616a-qt-replay-")
        self.logs_dir = Path(self._temporary_directory.name)
        self.paths = MappingProxyType(
            {
                **{
                    name: self.logs_dir / filename
                    for name, filename in SNAPSHOT_FILES.items()
                },
                "telemetry_log": self.logs_dir / "cn616a_telemetry_log.jsonl",
                "analysis_log": self.logs_dir / "cn616a_analysis_log.jsonl",
            }
        )
        self.status = AdapterStatus(
            mode="offline-replay",
            connected=False,
            read_only=True,
            provenance=(
                "Synthetic repository baseline; analysis is mock-derived and "
                "command outcomes are mock-only."
            ),
            message="OFFLINE REPLAY / NO HARDWARE",
            target=ControllerIdentity(
                key=ControllerKey(bus_id="offline-replay", controller_id=1),
                modbus_address=1,
            ),
            generation=self.generation,
        )
        self._snapshot_data: dict[SnapshotCategory, dict[str, object]] = {}
        self._outcomes: list[CommandOutcome] = []
        self._mock_command_scenario: str | None = None
        self._closed = False
        self._history_reader: IncrementalHistoryReader | None = None
        self._history_revision = 0
        self._history: AdapterHistory | None = None
        self._run_number = 0
        self._run_active = False
        self._run_dir: Path | None = None
        self._run_name = ""
        self._run_started_ts: str | None = None
        self._run_started_monotonic: float | None = None
        self._run_last_data_ts: str | None = None
        self._run_recovered = False
        self._run_interruption_s: float | None = None
        self._run_interruption_started_ts: str | None = None
        self._run_recovered_at_ts: str | None = None
        self._run_schedule_enabled = False
        self._run_schedule_stop_at: datetime | None = None
        try:
            self._write_replay()
            self._history_reader = IncrementalHistoryReader(self.logs_dir)
            self._history_revision = self._history_reader.request(1.0 / 3600.0)
        except Exception:
            self.shutdown()
            raise

    def _write_json(self, path: Path, payload: Mapping[str, object]) -> None:
        path.write_text(
            json.dumps(payload, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )

    def _write_jsonl(self, path: Path, records: Sequence[Mapping[str, object]]) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            for record in records:
                stream.write(json.dumps(record, separators=(",", ":"), allow_nan=False))
                stream.write("\n")

    def _write_replay(self) -> None:
        replay = _adapt_baseline()
        samples = replay.telemetry
        if not samples:
            raise ValueError("The repository baseline contains no telemetry samples")

        final_elapsed = replay.manifest["sample_period_ms"] * (len(samples) - 1)
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(milliseconds=final_elapsed)
        telemetry_records: list[dict[str, object]] = []
        analysis_records: list[dict[str, object]] = []

        for index, telemetry in enumerate(samples):
            elapsed_ms = replay.manifest["sample_period_ms"] * index
            timestamp = (start_time + timedelta(milliseconds=elapsed_ms)).isoformat(
                timespec="milliseconds"
            )
            telemetry_records.append(
                {
                    "ts": timestamp,
                    "timestamp_pacific": timestamp,
                    "unit": 1,
                    "port": "offline-replay",
                    "zones": list(ZONE_IDS),
                    "telemetry": telemetry,
                }
            )
            analysis_records.append(
                {
                    "ts": timestamp,
                    "timestamp_pacific": timestamp,
                    "unit": 1,
                    "port": "offline-replay",
                    "analysis": _mock_analysis(telemetry),
                }
            )

        latest = telemetry_records[-1]
        latest_analysis = analysis_records[-1]
        snapshot_metadata = {
            "ts": latest["ts"],
            "unit": 1,
            "port": "SYNTHETIC",
            "zones": list(ZONE_IDS),
        }
        self._snapshot_data = {
            "telemetry": {**snapshot_metadata, "telemetry": latest["telemetry"]},
            "config": {**snapshot_metadata, "config": replay.config},
            "rampsoak": {**snapshot_metadata, "rampsoak": replay.rampsoak},
            "analysis": {**snapshot_metadata, "analysis": latest_analysis["analysis"]},
            "service_config": {
                "ts": latest["ts"],
                "config": {
                    "gui_refresh_hz": 2.0,
                    "telemetry_poll_hz": 2.0,
                    "analysis_poll_hz": 1.0,
                    "zones_mode": "all",
                    "zones_list": list(ZONE_IDS),
                    "zone_names": {
                        str(zone_id): f"Synthetic Zone {zone_id}"
                        for zone_id in ZONE_IDS
                    },
                    "viewer": {
                        "history_hours": 1 / 3600,
                        "show_sp_abs": True,
                        "show_sp_autotune": True,
                        "show_mae": True,
                        "line_width": 1.5,
                    },
                },
            },
        }
        for name, snapshot in self._snapshot_data.items():
            self._write_json(self.paths[name], snapshot)
        self._write_jsonl(self.paths["telemetry_log"], telemetry_records)
        self._write_jsonl(self.paths["analysis_log"], analysis_records)

    def snapshots(self) -> Mapping[str, AdapterSnapshot]:
        snapshots = {}
        for name, payload in self._snapshot_data.items():
            snapshots[name] = normalize_snapshot(
                name,
                payload,
                age_s=0.0,
                stale=False,
                error=None,
                modified_at=None,
                read_at=None,
                generation=self.generation,
                identity=self.status.target,
            )
        return MappingProxyType(snapshots)

    def outcomes(self) -> tuple[CommandOutcome, ...]:
        return tuple(self._outcomes)

    @property
    def history(self) -> AdapterHistory | None:
        return self._history

    def request_history(self, history_hours: float) -> int:
        if self._closed or self._history_reader is None:
            raise RuntimeError("Offline replay session is shut down")
        self._history_revision = self._history_reader.request(history_hours)
        return self._history_revision

    def set_mock_command_scenario(self, scenario: str) -> None:
        if scenario not in {"success", "rejection", "partial", "unknown"}:
            raise ValueError(f"Unknown offline command scenario: {scenario}")
        if self._closed:
            raise RuntimeError("Offline replay session is shut down")
        self._mock_command_scenario = scenario

    def poll_history(self) -> AdapterHistory | None:
        if self._closed or self._history_reader is None:
            return None
        for result in self._history_reader.poll_results():
            revision = result.get("revision")
            if not isinstance(revision, int) or revision < self._history_revision:
                continue
            source_identity = result.get("source_identity")
            if not (
                isinstance(source_identity, tuple)
                and len(source_identity) == 2
                and isinstance(source_identity[0], str)
                and isinstance(source_identity[1], int)
            ):
                source_identity = None
            errors = result.get("errors", [])
            error = "; ".join(str(item) for item in errors) or None
            data: Mapping[str, object] | None = MappingProxyType(result)
            identity = self.status.target
            expected_identity = (
                (identity.key.bus_id, identity.modbus_address)
                if identity is not None
                else None
            )
            if result.get("source_identity_conflict"):
                error = "History contains multiple controller identities; history is withheld."
                data = None
            elif result.get("source_identity_missing"):
                error = "History contains records with unknown controller identity; history is withheld."
                data = None
            elif expected_identity is None or source_identity != expected_identity:
                error = "History source identity does not match the replay; history is withheld."
                data = None
            self._history = AdapterHistory(
                generation=self.generation,
                endpoint="offline-replay",
                identity=identity if data is not None else None,
                data=data,
                error=error,
                source_identity=source_identity,
                revision=revision,
            )
        return self._history

    def is_current_generation(self, generation: int) -> bool:
        return not self._closed and generation == self.generation

    def submit(
        self,
        messages: Sequence[Mapping[str, object]],
        *,
        target: ZoneKey | None = None,
        coalesce_key: str | None = None,
    ) -> SubmissionStatus:
        del coalesce_key
        if not messages:
            raise ValueError("at least one service message is required")
        if self._closed:
            self._record_mock_outcomes(
                messages,
                "not_sent",
                "Offline session is closed; the request was not sent.",
                target,
            )
            return "closed"

        operations = tuple(str(message.get("op", "unknown")) for message in messages)
        mutation = any(operation not in READ_ONLY_OPERATIONS for operation in operations)
        run_control = bool(operations) and all(
            operation in RUN_CONTROL_OPERATIONS for operation in operations
        )
        run_operation = bool(operations) and all(
            operation in RUN_CONTROL_OPERATIONS | {"get_run_status"}
            for operation in operations
        )
        service_configuration = bool(operations) and all(
            operation in SERVICE_CONFIGURATION_OPERATIONS for operation in operations
        )
        if any("unit" in message for message in messages):
            return self._reject_mock(
                messages,
                target,
                "The v1 service does not route commands using a unit field.",
            )
        if target is not None and (
            self.status.target is None
            or target.bus_id != self.status.target.key.bus_id
            or target.controller_id != self.status.target.key.controller_id
        ):
            return self._reject_mock(
                messages,
                target,
                "Offline target does not match this replay session.",
            )
        if run_operation and target is not None:
            return self._reject_mock(
                messages,
                target,
                "Recording and schedule commands are service-level operations, not zone commands.",
            )

        if service_configuration:
            if target is not None:
                return self._reject_mock(
                    messages,
                    target,
                    "Configuration and connection commands are service-level operations.",
                )
            for index, message in enumerate(messages):
                operation = str(message.get("op"))
                if self._mock_command_scenario is None:
                    result = "mock"
                    detail = (
                        "SIMULATED ONLY; temporary replay settings; no real service, "
                        "serial port, or TCP endpoint was contacted."
                    )
                else:
                    result, detail = self._mock_outcome_for(self._mock_command_scenario, index)
                    detail = f"SIMULATED ONLY; {detail}"
                response: dict[str, object] = {"ok": result in {"mock", "confirmed_success"}}
                if operation == "set_service_config" and result in {"mock", "confirmed_success"}:
                    patch = message.get("patch")
                    if not isinstance(patch, Mapping):
                        response = {"ok": False, "error": "patch must be an object"}
                        result = "confirmed_failure"
                        detail = "SIMULATED ONLY; malformed settings patch rejected."
                    else:
                        snapshot = dict(self._snapshot_data["service_config"])
                        raw_config = snapshot.get("config")
                        current_config = dict(raw_config) if isinstance(raw_config, Mapping) else {}
                        current_config.update(dict(patch))
                        snapshot["config"] = current_config
                        self._snapshot_data["service_config"] = snapshot
                        response["service_config"] = current_config
                elif operation in {"connect_serial", "disconnect_serial", "refresh_connection"}:
                    response["connected"] = self.status.controller_connected
                self._record_mock_outcome(
                    message,
                    result,
                    detail,
                    target,
                    response=response,
                )
            return "mocked"
        if mutation and not run_control and (
            target is None
            or self.status.target is None
            or not messages_match_zone(messages, target)
        ):
            return self._reject_mock(
                messages,
                target,
                "Offline command target is missing or does not match the payload.",
            )

        if run_operation:
            for message in messages:
                response = self._handle_run_command(message)
                self._record_mock_outcome(
                    message,
                    "mock" if response.get("ok") is True else "confirmed_failure",
                    "SIMULATED ONLY; temporary replay backend; no service or hardware request was sent.",
                    target,
                    response=response,
                )
            return "mocked"

        scenario = self._mock_command_scenario
        for index, message in enumerate(messages):
            if scenario is None:
                result = "mock"
                detail = "SIMULATED ONLY; no service or hardware request was sent."
            else:
                result, detail = self._mock_outcome_for(scenario, index)
            if result in {"mock", "confirmed_success"}:
                self._simulate_profile_command(message)
            self._record_mock_outcome(message, result, detail, target)
        return "mocked"

    def _simulate_profile_command(self, message: Mapping[str, object]) -> None:
        operation = str(message.get("op", ""))
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        if operation in {"read_rampsoak", "set_num_segments", "set_rampsoak_segments"}:
            snapshot = dict(self._snapshot_data["rampsoak"])
            section_raw = snapshot.get("rampsoak")
            section = dict(section_raw) if isinstance(section_raw, Mapping) else {}
            zones_raw = section.get("zones")
            zones = dict(zones_raw) if isinstance(zones_raw, Mapping) else {}
            if operation == "set_num_segments":
                zone_id = message.get("zone")
                count = message.get("count")
                if isinstance(zone_id, int) and not isinstance(zone_id, bool) and isinstance(count, int):
                    zone = dict(zones.get(str(zone_id), {}))
                    zone["num_segments"] = count
                    zones[str(zone_id)] = zone
            elif operation == "set_rampsoak_segments":
                zone_id = message.get("zone")
                segments_raw = message.get("segments")
                if (
                    isinstance(zone_id, int)
                    and not isinstance(zone_id, bool)
                    and isinstance(segments_raw, Mapping)
                ):
                    zone = dict(zones.get(str(zone_id), {}))
                    existing_raw = zone.get("segments")
                    segments = dict(existing_raw) if isinstance(existing_raw, Mapping) else {}
                    segments.update(dict(segments_raw))
                    zone["segments"] = segments
                    zones[str(zone_id)] = zone
            section["zones"] = zones
            snapshot["rampsoak"] = section
            snapshot["ts"] = timestamp
            self._snapshot_data["rampsoak"] = snapshot
        elif operation == "set_control_mode":
            zone_id = message.get("zone")
            mode = message.get("mode")
            if not isinstance(zone_id, int) or isinstance(zone_id, bool) or not isinstance(mode, str):
                return
            snapshot = dict(self._snapshot_data["telemetry"])
            section_raw = snapshot.get("telemetry")
            section = dict(section_raw) if isinstance(section_raw, Mapping) else {}
            zones_raw = section.get("zones")
            zones = dict(zones_raw) if isinstance(zones_raw, Mapping) else {}
            zone = dict(zones.get(str(zone_id), {}))
            zone["control_mode"] = mode
            zones[str(zone_id)] = zone
            section["zones"] = zones
            snapshot["telemetry"] = section
            snapshot["ts"] = timestamp
            self._snapshot_data["telemetry"] = snapshot

    @staticmethod
    def _mock_outcome_for(scenario: str, index: int) -> tuple[str, str]:
        if scenario == "success":
            return (
                "confirmed_success",
                "SIMULATED ONLY; acknowledgment modeled; no request or readback occurred.",
            )
        if scenario == "rejection":
            if index == 0:
                return "confirmed_failure", "SIMULATED ONLY; first operation rejected."
            return "not_sent", "SIMULATED ONLY; fail-stop sequence left this operation unsent."
        if scenario == "partial":
            if index == 0:
                return (
                    "confirmed_success",
                    "SIMULATED ONLY; first operation acknowledged; no request or readback occurred.",
                )
            if index == 1:
                return "confirmed_failure", "SIMULATED ONLY; second operation rejected."
            return "not_sent", "SIMULATED ONLY; fail-stop sequence left this operation unsent."
        if index == 0:
            return "unknown", "SIMULATED ONLY; result unknown after modeled send; no retry was made."
        return "not_sent", "SIMULATED ONLY; later operation not sent after the unknown result."

    def _reject_mock(
        self,
        messages: Sequence[Mapping[str, object]],
        target: ZoneKey | None,
        detail: str,
    ) -> SubmissionStatus:
        self._record_mock_outcomes(messages, "not_sent", detail, target)
        return "rejected"

    def _record_mock_outcomes(
        self,
        messages: Sequence[Mapping[str, object]],
        result: str,
        detail: str,
        target: ZoneKey | None,
    ) -> None:
        for message in messages:
            self._record_mock_outcome(message, result, detail, target)

    def _run_status_payload(self) -> dict[str, object]:
        elapsed = None
        if self._run_active and self._run_started_monotonic is not None:
            elapsed = max(0.0, time.monotonic() - self._run_started_monotonic)
        return {
            "run_active": self._run_active,
            "run_dir": str(self._run_dir) if self._run_dir is not None else None,
            "run_name": self._run_name,
            "run_started_ts": self._run_started_ts,
            "run_elapsed_s": elapsed,
            "run_schedule_enabled": self._run_schedule_enabled,
            "run_schedule_stop_at": (
                self._run_schedule_stop_at.isoformat()
                if self._run_schedule_stop_at is not None
                else None
            ),
            "run_last_data_ts": self._run_last_data_ts,
            "run_recovered": self._run_recovered,
            "run_interruption_s": self._run_interruption_s,
            "run_interruption_started_ts": self._run_interruption_started_ts,
            "run_recovered_at_ts": self._run_recovered_at_ts,
        }

    def _write_simulated_run_info(self, event: str, reason: str | None = None) -> None:
        if self._run_dir is None:
            return
        payload = {
            "event": event,
            "reason": reason,
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_name": self._run_name,
            "run_started_ts": self._run_started_ts,
            "last_data_ts": self._run_last_data_ts,
            "recovered": self._run_recovered,
            "interruption_s": self._run_interruption_s,
            "interruption_started_ts": self._run_interruption_started_ts,
            "recovered_at_ts": self._run_recovered_at_ts,
            "unit": 1,
            "port": "offline-replay",
            "zones_enabled": list(ZONE_IDS),
            "run_schedule_enabled": self._run_schedule_enabled,
            "run_schedule_stop_at": (
                self._run_schedule_stop_at.isoformat()
                if self._run_schedule_stop_at is not None
                else None
            ),
        }
        (self._run_dir / "cn616a_run_info.json").write_text(
            json.dumps(payload, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )

    def _create_simulated_run(self, run_name: str) -> None:
        self._run_number += 1
        self._run_dir = self.logs_dir / "simulated-runs" / f"run-{self._run_number:03d}"
        self._run_dir.mkdir(parents=True, exist_ok=False)
        self._run_name = run_name
        self._run_started_ts = datetime.now(timezone.utc).isoformat()
        self._run_started_monotonic = time.monotonic()
        self._run_last_data_ts = self._run_started_ts
        record = json.loads(self.paths["telemetry_log"].read_text(encoding="utf-8").splitlines()[-1])
        record["ts"] = self._run_started_ts
        record["timestamp_pacific"] = self._run_started_ts
        (self._run_dir / "cn616a_telemetry_log.jsonl").write_text(
            json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n",
            encoding="utf-8",
        )
        active_path = self.logs_dir / "cn616a_active_run_state.json"
        active_path.write_text(
            json.dumps(
                {
                    "active": True,
                    "run_dir": str(self._run_dir),
                    "run_name": self._run_name,
                    "run_started_ts": self._run_started_ts,
                    "last_data_ts": self._run_last_data_ts,
                    "service_stopped_ts": None,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self._run_active = True
        self._run_recovered = False
        self._run_interruption_s = None
        self._run_interruption_started_ts = None
        self._run_recovered_at_ts = None
        self._write_simulated_run_info("started")

    def _handle_run_command(self, message: Mapping[str, object]) -> dict[str, object]:
        operation = message.get("op")
        if operation == "get_run_status":
            return {"id": message.get("id"), "ok": True, **self._run_status_payload()}
        if operation == "start_run":
            run_dir = str(message.get("run_dir") or "").strip()
            if not run_dir:
                return {"id": message.get("id"), "ok": False, "error": "run_dir is required"}
            if self._run_schedule_enabled and self._run_schedule_stop_at is not None:
                if datetime.now(timezone.utc) >= self._run_schedule_stop_at:
                    return {
                        "id": message.get("id"),
                        "ok": False,
                        "error": "Scheduled auto-stop time is already in the past.",
                    }
            self._create_simulated_run(str(message.get("run_name") or "").strip())
            return {"id": message.get("id"), "ok": True, **self._run_status_payload()}
        if operation == "stop_run":
            was_active = self._run_active
            if was_active:
                self._write_simulated_run_info("stopped", "manual")
                (self.logs_dir / "cn616a_active_run_state.json").write_text(
                    json.dumps({"active": False, "stopped_ts": datetime.now(timezone.utc).isoformat()}),
                    encoding="utf-8",
                )
                self._run_active = False
                self._run_dir = None
                self._run_name = ""
                self._run_started_ts = None
                self._run_started_monotonic = None
                self._run_last_data_ts = None
                self._run_recovered = False
                self._run_interruption_s = None
                self._run_interruption_started_ts = None
                self._run_recovered_at_ts = None
            return {
                "id": message.get("id"),
                "ok": True,
                "was_active": was_active,
                **self._run_status_payload(),
            }
        if operation == "set_run_schedule":
            enabled = bool(message.get("enabled", False))
            stop_at = None
            if enabled:
                raw_time = message.get("stop_at_iso")
                if not raw_time:
                    return {
                        "id": message.get("id"),
                        "ok": False,
                        "error": "stop_at_iso is required when enabling schedule",
                    }
                try:
                    stop_at = datetime.fromisoformat(str(raw_time))
                except ValueError:
                    return {
                        "id": message.get("id"),
                        "ok": False,
                        "error": "stop_at_iso must be an ISO-8601 timestamp",
                    }
                if stop_at.tzinfo is None:
                    return {
                        "id": message.get("id"),
                        "ok": False,
                        "error": "stop_at_iso must include a UTC offset",
                    }
                if stop_at <= datetime.now(timezone.utc):
                    return {
                        "id": message.get("id"),
                        "ok": False,
                        "error": "stop_at_iso must be in the future",
                    }
            self._run_schedule_enabled = enabled
            self._run_schedule_stop_at = stop_at
            return {"id": message.get("id"), "ok": True, **self._run_status_payload()}
        return {"id": message.get("id"), "ok": False, "error": f"Unknown run operation: {operation}"}

    def simulate_run_recovery(self) -> Mapping[str, object]:
        if not self._run_active:
            self._create_simulated_run("Recovered replay")
        now = datetime.now(timezone.utc)
        (self.logs_dir / "cn616a_active_run_state.json").write_text(
            json.dumps(
                {
                    "active": True,
                    "run_dir": str(self._run_dir),
                    "run_name": self._run_name,
                    "run_started_ts": self._run_started_ts,
                    "last_data_ts": self._run_last_data_ts,
                    "service_stopped_ts": (now - timedelta(seconds=30)).isoformat(),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self._run_recovered = True
        self._run_interruption_s = 30.0
        self._run_interruption_started_ts = (now - timedelta(seconds=30)).isoformat()
        self._run_recovered_at_ts = now.isoformat()
        self._run_started_monotonic = time.monotonic()
        self._write_simulated_run_info("recovered")
        return {"ok": True, **self._run_status_payload()}

    def _record_mock_outcome(
        self,
        message: Mapping[str, object],
        result: str,
        detail: str,
        target: ZoneKey | None,
        *,
        response: Mapping[str, object] | None = None,
    ) -> None:
        self._outcomes.append(
            CommandOutcome(
                operation=str(message.get("op", "unknown")),
                result=result,
                message=detail,
                request_id=str(message["id"]) if "id" in message else None,
                generation=self.generation,
                target=target,
                response=MappingProxyType(dict(response)) if response is not None else None,
                endpoint="offline-replay",
                created_at=datetime.now(timezone.utc),
            )
        )
        del self._outcomes[:-100]

    def shutdown(self) -> None:
        if not self._closed:
            self._closed = True
            if self._history_reader is not None:
                self._history_reader.shutdown()
                self._history_reader = None
            self._temporary_directory.cleanup()

    def __enter__(self) -> OfflineReplaySession:
        return self

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> None:
        self.shutdown()
