"""Shared, change-aware background reader for service-written JSON snapshots."""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple


LOGGER = logging.getLogger("cn616a.gui")
DEFAULT_STATE_FILES = {
    "telemetry": "cn616a_telemetry_state.json",
    "config": "cn616a_config_state.json",
    "rampsoak": "cn616a_rampsoak_state.json",
    "analysis": "cn616a_analysis_state.json",
    "service_config": "cn616a_service_config_state.json",
}
MAX_STATE_FILE_BYTES = 4 * 1024 * 1024

_FileSignature = Optional[Tuple[int, int, int, int]]


@dataclass(frozen=True)
class StateSnapshot:
    data: Optional[Dict[str, object]]
    error: Optional[str]
    age_s: Optional[float]
    stale: bool
    modified_at: Optional[float]
    read_at: Optional[float]


@dataclass(frozen=True)
class _CachedSnapshot:
    signature: _FileSignature
    data: Optional[Dict[str, object]]
    error: Optional[str]
    modified_at: Optional[float]
    read_at: float


class StateSnapshotReader:
    """Read each service state file off-thread and share safely owned parsed snapshots."""

    def __init__(
        self,
        logs_dir: Path,
        *,
        state_files: Optional[Mapping[str, str]] = None,
        poll_interval_s: float = 0.25,
        retry_interval_s: float = 0.5,
        max_file_bytes: int = MAX_STATE_FILE_BYTES,
    ):
        if poll_interval_s <= 0 or retry_interval_s <= 0 or max_file_bytes <= 0:
            raise ValueError("poll intervals and max_file_bytes must be positive")
        self.logs_dir = Path(logs_dir)
        self.state_files = dict(state_files or DEFAULT_STATE_FILES)
        self.poll_interval_s = float(poll_interval_s)
        self.retry_interval_s = float(retry_interval_s)
        self.max_file_bytes = int(max_file_bytes)
        self._lock = threading.Lock()
        self._snapshots: Dict[str, _CachedSnapshot] = {}
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="cn616a-state-snapshots", daemon=True)
        self._worker.start()

    def snapshot(self, name: str, *, stale_after_s: float = 5.0) -> StateSnapshot:
        if name not in self.state_files:
            raise KeyError(f"unknown state snapshot: {name}")
        with self._lock:
            cached = self._snapshots.get(name)
        if cached is None:
            return StateSnapshot(None, "Snapshot has not been read yet", None, True, None, None)

        age_s = self._age_seconds(cached)
        return StateSnapshot(
            data=copy.deepcopy(cached.data),
            error=cached.error,
            age_s=age_s,
            stale=age_s is None or age_s > stale_after_s,
            modified_at=cached.modified_at,
            read_at=cached.read_at,
        )

    def snapshots(self, *names: str, stale_after_s: float = 5.0) -> Dict[str, StateSnapshot]:
        return {name: self.snapshot(name, stale_after_s=stale_after_s) for name in names}

    def shutdown(self) -> None:
        self._stop.set()
        self._worker.join(timeout=max(2.0, self.poll_interval_s + 1.0))
        if self._worker.is_alive():
            LOGGER.error("State snapshot worker did not stop promptly")

    @staticmethod
    def _age_seconds(cached: _CachedSnapshot) -> Optional[float]:
        timestamp = None
        if cached.data is not None:
            raw_timestamp = cached.data.get("ts") or cached.data.get("timestamp")
            if isinstance(raw_timestamp, str):
                try:
                    parsed = datetime.fromisoformat(raw_timestamp)
                    timestamp = parsed.timestamp()
                except ValueError:
                    pass
        source_time = timestamp if timestamp is not None else cached.modified_at
        return max(0.0, time.time() - source_time) if source_time is not None else None

    def _signature(self, name: str) -> Tuple[_FileSignature, Optional[str], Optional[float]]:
        path = self.logs_dir / self.state_files[name]
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None, f"Snapshot file not found: {path.name}", None
        except OSError as exc:
            return None, f"Unable to stat {path.name}: {type(exc).__name__}: {exc}", None
        return (
            (stat.st_dev, getattr(stat, "st_ino", 0), stat.st_size, stat.st_mtime_ns),
            None,
            stat.st_mtime,
        )

    def _read_snapshot(
        self,
        name: str,
        signature: _FileSignature,
        modified_at: Optional[float],
    ) -> _CachedSnapshot:
        path = self.logs_dir / self.state_files[name]
        now = time.time()
        if signature is None:
            return _CachedSnapshot(signature, None, "Snapshot file is unavailable", modified_at, now)
        if signature[2] > self.max_file_bytes:
            return _CachedSnapshot(
                signature,
                None,
                f"Snapshot file exceeds the {self.max_file_bytes}-byte read limit",
                modified_at,
                now,
            )

        try:
            with path.open("rb") as stream:
                raw = stream.read(self.max_file_bytes + 1)
            if len(raw) > self.max_file_bytes:
                raise ValueError(f"Snapshot file exceeds the {self.max_file_bytes}-byte read limit")
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("Snapshot JSON root must be an object")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            return _CachedSnapshot(
                signature,
                None,
                f"Unable to read {path.name}: {type(exc).__name__}: {exc}",
                modified_at,
                now,
            )

        after_signature, signature_error, after_modified_at = self._signature(name)
        if signature_error is not None or after_signature != signature:
            return _CachedSnapshot(
                after_signature,
                None,
                f"Snapshot changed while reading {path.name}; waiting for a stable write",
                after_modified_at,
                time.time(),
            )
        return _CachedSnapshot(signature, parsed, None, modified_at, time.time())

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            for name in self.state_files:
                if self._stop.is_set():
                    return
                signature, signature_error, modified_at = self._signature(name)
                with self._lock:
                    previous = self._snapshots.get(name)

                if (
                    previous is not None
                    and previous.signature == signature
                    and previous.error is None
                    and signature_error is None
                ):
                    continue
                if (
                    previous is not None
                    and previous.signature == signature
                    and previous.error is not None
                    and now - previous.read_at < self.retry_interval_s
                ):
                    continue

                if signature_error is not None:
                    updated = _CachedSnapshot(signature, None, signature_error, modified_at, time.time())
                else:
                    updated = self._read_snapshot(name, signature, modified_at)
                with self._lock:
                    self._snapshots[name] = updated
                if updated.error is not None and (
                    previous is None
                    or previous.error != updated.error
                    or previous.signature != updated.signature
                ):
                    LOGGER.warning("%s snapshot unavailable: %s", name, updated.error)
            self._stop.wait(self.poll_interval_s)
