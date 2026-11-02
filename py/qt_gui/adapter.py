"""Toolkit-independent contracts shared by Qt adapters and models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence


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
class AdapterStatus:
    mode: str
    connected: bool
    read_only: bool
    provenance: str
    message: str
    target: ControllerIdentity | None = None


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    operation: str
    result: str
    message: str
    target: ZoneKey | None = None


class QtAdapter(Protocol):
    """Boundary for snapshots, command outcomes, status, and bounded shutdown."""

    @property
    def status(self) -> AdapterStatus: ...

    def snapshots(self) -> Mapping[str, Mapping[str, object]]: ...

    def outcomes(self) -> Sequence[CommandOutcome]: ...

    def shutdown(self) -> None: ...
