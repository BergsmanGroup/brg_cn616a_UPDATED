"""Toolkit-independent contracts shared by Qt adapters and models."""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import count
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol, Sequence

from py.gui.history_reader import parse_iso_timestamp
from py.gui.state_values import get_field, get_numeric, get_zone, get_zones


SnapshotCategory = Literal["telemetry", "config", "rampsoak", "analysis", "service_config"]
SubmissionStatus = Literal["submitted", "coalesced", "full", "closed", "rejected", "mocked"]
READ_ONLY_OPERATIONS = frozenset(
    {
        "get_run_status",
        "get_service_config",
        "get_service_config",
        "get_status",
        "ping",
        "read_config",
        "read_rampsoak",
    }
)
RUN_CONTROL_OPERATIONS = frozenset({"start_run", "stop_run", "set_run_schedule"})
PROFILE_EXECUTION_OPERATIONS = frozenset({"set_start_profile"})
SERVICE_CONFIGURATION_OPERATIONS = frozenset(
    {"set_service_config", "connect_serial", "disconnect_serial", "refresh_connection"}
)
_SESSION_GENERATIONS = count(1)


def new_session_generation() -> int:
    return next(_SESSION_GENERATIONS)

_ZONE_SECTIONS = {
    "telemetry": "telemetry",
    "config": "config",
    "rampsoak": "rampsoak",
    "analysis": "analysis",
}
_NUMERIC_FIELDS = {
    "telemetry": {
        "pv_c": ("pv_c", "pv", "PV", "process_value_c", "process_value"),
        "sp_abs_c": ("sp_abs_c", "sp_abs", "sp_c", "sp", "SP_abs", "SP", "setpoint_c", "setpoint"),
        "autotune_sp_c": (
            "autotune_sp_c",
            "autotune_sp",
            "sp_autotune_c",
            "sp_autotune",
            "SP_autotune",
        ),
        "out_pct": ("out_pct", "output_pct", "output", "OUT"),
        "p_gain": ("p_gain", "kp", "proportional_gain"),
        "i_gain": ("i_gain", "ki", "integral_gain"),
        "d_gain": ("d_gain", "kd", "derivative_gain"),
    },
    "analysis": {
        "avg_abs_error_c": ("avg_abs_error_c", "mae_c", "mae", "mean_abs_error_c"),
        "n_points": ("n_points", "point_count"),
        "threshold_c": ("threshold_c", "equilibrium_threshold_c"),
        "last_pv_c": ("last_pv_c",),
        "last_sp_c": ("last_sp_c",),
    },
}
_OTHER_FIELDS = {
    "telemetry": {"in_equilibrium": ("in_equilibrium", "equilibrium")},
    "analysis": {"in_equilibrium": ("in_equilibrium", "equilibrium")},
}


@dataclass(frozen=True, slots=True)
class ControllerKey:
    bus_id: str
    controller_id: int


@dataclass(frozen=True, slots=True)
class ZoneKey:
    bus_id: str
    controller_id: int
    zone_id: int


@dataclass(frozen=True, slots=True)
class ControllerIdentity:
    key: ControllerKey
    modbus_address: int


@dataclass(frozen=True, slots=True)
class AdapterSnapshot:
    category: SnapshotCategory
    data: Mapping[str, object] | None
    source_time: datetime | None
    age_s: float | None
    stale: bool
    error: str | None
    modified_at: float | None
    read_at: float | None
    identity: ControllerIdentity | None
    identity_warning: str | None
    generation: int
    zone_values: Mapping[int, Mapping[str, object]]
    keyed_values: Mapping[ZoneKey, Mapping[str, object]]


@dataclass(frozen=True, slots=True)
class AdapterHistory:
    generation: int
    endpoint: str
    identity: ControllerIdentity | None
    data: Mapping[str, object] | None
    error: str | None
    source_identity: tuple[str, int] | None
    revision: int


def snapshot_age_seconds(snapshot: AdapterSnapshot) -> float | None:
    """Calculate age from cached timestamps without rereading the source."""
    if snapshot.source_time is not None:
        if snapshot.source_time.tzinfo is None:
            return None
        return max(0.0, (datetime.now(timezone.utc) - snapshot.source_time).total_seconds())
    if snapshot.modified_at is not None:
        return max(0.0, time.time() - snapshot.modified_at)
    return snapshot.age_s


@dataclass(frozen=True, slots=True)
class AdapterStatus:
    mode: str
    connected: bool
    read_only: bool
    provenance: str
    message: str
    target: ControllerIdentity | None = None
    endpoint: str | None = None
    generation: int = 0
    controller_connected: bool | None = None
    identity_warning: str | None = None


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    operation: str
    result: str
    message: str
    target: ZoneKey | None = None
    request_id: str | None = None
    response: Mapping[str, object] | None = None
    generation: int = 0
    endpoint: str | None = None
    created_at: datetime | None = None


def controller_identity(data: Mapping[str, Any] | None) -> ControllerIdentity | None:
    if not isinstance(data, Mapping):
        return None
    bus_id = get_field(data, "bus_id", "port")
    controller_id = get_field(data, "controller_id", "unit")
    modbus_address = get_field(data, "modbus_address", "unit")
    if not isinstance(bus_id, str) or not bus_id.strip() or bus_id.strip().upper() == "SYNTHETIC":
        return None

    def positive_address(value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        integer = int(value)
        if integer != value or not 1 <= integer <= 247:
            return None
        return integer

    controller_number = positive_address(controller_id)
    address = positive_address(modbus_address)
    if controller_number is None or address is None:
        return None
    return ControllerIdentity(
        key=ControllerKey(bus_id=bus_id.strip(), controller_id=controller_number),
        modbus_address=address,
    )


def messages_match_zone(
    messages: Sequence[Mapping[str, object]],
    target: ZoneKey,
) -> bool:
    if not 1 <= target.zone_id <= 6:
        return False
    for message in messages:
        if "zone" in message:
            zone = message["zone"]
            if isinstance(zone, bool) or not isinstance(zone, int) or zone != target.zone_id:
                return False
        if "zones" in message:
            zones = message["zones"]
            if not isinstance(zones, (list, tuple)) or not zones or any(
                isinstance(zone, bool)
                or not isinstance(zone, int)
                or zone != target.zone_id
                for zone in zones
            ):
                return False
    return True


def messages_match_profile_zones(
    messages: Sequence[Mapping[str, object]],
    target: ZoneKey,
) -> bool:
    """Validate an explicit start/stop zone list against a verified controller target."""
    if not 1 <= target.zone_id <= 6 or not messages:
        return False
    for message in messages:
        if message.get("op") not in PROFILE_EXECUTION_OPERATIONS or "zone" in message:
            return False
        zones = message.get("zones")
        if not isinstance(zones, (list, tuple)) or not zones:
            return False
        if any(
            isinstance(zone, bool) or not isinstance(zone, int) or not 1 <= zone <= 6
            for zone in zones
        ):
            return False
        if len(set(zones)) != len(zones) or target.zone_id not in zones:
            return False
    return True


def normalize_snapshot(
    category: SnapshotCategory,
    data: Mapping[str, Any] | None,
    *,
    age_s: float | None,
    stale: bool,
    error: str | None,
    modified_at: float | None,
    read_at: float | None,
    generation: int,
    identity: ControllerIdentity | None = None,
) -> AdapterSnapshot:
    source_time = None
    if isinstance(data, Mapping):
        source_time = parse_iso_timestamp(
            get_field(data, "ts", "timestamp", "timestamp_pacific")
        )
    resolved_identity = identity if identity is not None else controller_identity(data)

    local_zones: dict[int, Mapping[str, object]] = {}
    keyed_zones: dict[ZoneKey, Mapping[str, object]] = {}
    section = _ZONE_SECTIONS.get(category)
    zones = get_zones(data, section) if section is not None else {}
    for zone_id in range(1, 7):
        raw_zone = get_zone(zones, zone_id)
        values: dict[str, object] = dict(raw_zone)
        for field, aliases in _NUMERIC_FIELDS.get(category, {}).items():
            values[field] = get_numeric(raw_zone, *aliases)
        for field, aliases in _OTHER_FIELDS.get(category, {}).items():
            values[field] = get_field(raw_zone, *aliases)
        frozen_values = MappingProxyType(values)
        local_zones[zone_id] = frozen_values
        if resolved_identity is not None:
            key = resolved_identity.key
            keyed_zones[ZoneKey(key.bus_id, key.controller_id, zone_id)] = frozen_values

    identity_warning = None
    if resolved_identity is None:
        identity_warning = "Controller identity is missing or ambiguous; writes are disabled."

    return AdapterSnapshot(
        category=category,
        data=MappingProxyType(copy.deepcopy(dict(data))) if data is not None else None,
        source_time=source_time,
        age_s=age_s,
        stale=stale,
        error=error,
        modified_at=modified_at,
        read_at=read_at,
        identity=resolved_identity,
        identity_warning=identity_warning,
        generation=generation,
        zone_values=MappingProxyType(local_zones),
        keyed_values=MappingProxyType(keyed_zones),
    )


class QtAdapter(Protocol):
    """Boundary for snapshots, command outcomes, status, and bounded shutdown."""

    @property
    def generation(self) -> int: ...

    @property
    def status(self) -> AdapterStatus: ...

    def snapshots(self) -> Mapping[str, AdapterSnapshot]: ...

    def outcomes(self) -> Sequence[CommandOutcome]: ...

    def submit(
        self,
        messages: Sequence[Mapping[str, object]],
        *,
        target: ZoneKey | None = None,
        coalesce_key: str | None = None,
    ) -> SubmissionStatus: ...

    def is_current_generation(self, generation: int) -> bool: ...

    def shutdown(self) -> None: ...
