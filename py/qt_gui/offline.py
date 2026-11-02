"""Temporary service-shaped replay data derived from the immutable raw baseline."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from py.gui.state_values import get_numeric, get_zone, get_zones

from .adapter import (
    AdapterSnapshot,
    AdapterStatus,
    CommandOutcome,
    ControllerIdentity,
    ControllerKey,
    READ_ONLY_OPERATIONS,
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
        self._closed = False
        try:
            self._write_replay()
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
                    "port": "SYNTHETIC",
                    "zones": list(ZONE_IDS),
                    "telemetry": telemetry,
                }
            )
            analysis_records.append(
                {
                    "ts": timestamp,
                    "timestamp_pacific": timestamp,
                    "unit": 1,
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

        mutation = any(str(message.get("op", "unknown")) not in READ_ONLY_OPERATIONS for message in messages)
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
        if mutation and (
            target is None
            or self.status.target is None
            or not messages_match_zone(messages, target)
        ):
            return self._reject_mock(
                messages,
                target,
                "Offline command target is missing or does not match the payload.",
            )

        for message in messages:
            self._record_mock_outcome(
                message,
                "mock",
                "SIMULATED ONLY; no service or hardware request was sent.",
                target,
            )
        return "mocked"

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

    def _record_mock_outcome(
        self,
        message: Mapping[str, object],
        result: str,
        detail: str,
        target: ZoneKey | None,
    ) -> None:
        self._outcomes.append(
            CommandOutcome(
                operation=str(message.get("op", "unknown")),
                result=result,
                message=detail,
                request_id=str(message["id"]) if "id" in message else None,
                generation=self.generation,
                target=target,
                endpoint="offline-replay",
                created_at=datetime.now(timezone.utc),
            )
        )
        del self._outcomes[:-100]

    def shutdown(self) -> None:
        if not self._closed:
            self._closed = True
            self._temporary_directory.cleanup()

    def __enter__(self) -> OfflineReplaySession:
        return self

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> None:
        self.shutdown()
