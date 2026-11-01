"""Incremental, bounded background ingestion for telemetry and analysis JSONL logs."""

from __future__ import annotations

import json
import logging
import math
import queue
import statistics
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo


LOGGER = logging.getLogger("cn616a.gui")
_CHUNK_BYTES = 64 * 1024
_MAX_WORK_BYTES = 1024 * 1024
_MAX_PARTIAL_LINE_BYTES = 1024 * 1024
_MAX_RAW_POINTS_PER_ZONE = 100_000
_MAX_RENDER_POINTS_PER_ZONE = 20_000
_MAX_REPORTED_ERRORS = 20
_ZONE_IDS = tuple(range(1, 7))
_Identity = Tuple[int, int]


@dataclass
class _Cursor:
    category: str
    path: Path
    identity: _Identity
    target_size: int
    modified_ns: int
    offset: int = 0
    pending: bytes = b""
    discarding_line: bool = False


def parse_iso_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
        except Exception:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _numeric(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _first_value(data: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _zone_mapping(obj: Dict[str, Any]) -> Dict[str, Any]:
    for candidate in (
        obj.get("data"),
        obj.get("telemetry"),
        obj,
    ):
        if isinstance(candidate, dict):
            zones = candidate.get("zones", candidate)
            if isinstance(zones, dict):
                return zones
    return {}


def _zone_value(zones: Dict[str, Any], zone_id: int) -> Dict[str, Any]:
    for key in (str(zone_id), zone_id, f"z{zone_id}", f"Z{zone_id}", f"zone{zone_id}", f"ZONE{zone_id}"):
        zone = zones.get(key)
        if isinstance(zone, dict):
            return zone
    return {}


def _log_files(logs_dir: Path, category: str) -> List[Path]:
    prefix = "cn616a_telemetry_log" if category == "telemetry" else "cn616a_analysis_log"
    active = logs_dir / f"{prefix}.jsonl"
    try:
        rotated = list(logs_dir.glob(f"{prefix}_*.jsonl"))
    except OSError as exc:
        LOGGER.warning("Unable to list %s history files: %s", category, exc)
        rotated = []
    paths = ([active] if active.is_file() else []) + [path for path in rotated if path != active]
    def sort_key(path: Path) -> Tuple[int, str]:
        try:
            return path.stat().st_mtime_ns, path.name
        except OSError:
            return -1, path.name

    paths.sort(key=sort_key)
    result = []
    for path in paths:
        try:
            path.stat()
            result.append(path)
        except OSError:
            LOGGER.warning("Unable to stat history file %s", path)
    return result


def _identity(path: Path) -> Tuple[_Identity, int, int]:
    stat = path.stat()
    return (stat.st_dev, getattr(stat, "st_ino", 0)), stat.st_size, stat.st_mtime_ns


def _downsample_indices(rows: List[Tuple[Any, ...]], max_points: int) -> List[int]:
    count = len(rows)
    if count <= max_points:
        return list(range(count))
    field_count = len(rows[0]) - 2
    mandatory = {0, count - 1}
    deltas = []
    for index in range(1, count):
        try:
            delta = (rows[index][0] - rows[index - 1][0]).total_seconds()
        except (AttributeError, TypeError):
            delta = 0.0
        if delta > 0:
            deltas.append(float(delta))
        for field in range(2, len(rows[index])):
            previous = _numeric(rows[index - 1][field]) is not None
            current = _numeric(rows[index][field]) is not None
            if previous != current:
                mandatory.update((index - 1, index))
    if deltas:
        gap_threshold = max(5.0, 2.5 * float(statistics.median_low(deltas)))
        for index in range(1, count):
            try:
                delta = (rows[index][0] - rows[index - 1][0]).total_seconds()
            except (AttributeError, TypeError):
                continue
            if delta > gap_threshold:
                mandatory.update((index - 1, index))
    if len(mandatory) > max_points:
        raise ValueError("history has more discontinuities than the chart point limit")

    points_per_bucket = 2 + (2 * field_count)
    bucket_count = (max_points - len(mandatory)) // points_per_bucket
    if bucket_count < 1:
        raise ValueError("history cannot be safely downsampled within the chart point limit")

    selected = set(mandatory)
    for bucket in range(bucket_count):
        start = (bucket * count) // bucket_count
        end = ((bucket + 1) * count) // bucket_count
        end = max(start + 1, end)
        selected.update((start, min(count - 1, end - 1)))
        for field in range(2, len(rows[0])):
            valid = [
                index
                for index in range(start, min(end, count))
                if _numeric(rows[index][field]) is not None
            ]
            if valid:
                selected.add(min(valid, key=lambda index: _numeric(rows[index][field])))
                selected.add(max(valid, key=lambda index: _numeric(rows[index][field])))

    if len(selected) > max_points:
        raise ValueError("history extrema exceed the chart point limit")
    return sorted(selected)


class _HistoryEngine:
    def __init__(self, logs_dir: Path):
        self.logs_dir = Path(logs_dir)
        self.window_hours = 0.0
        self._initialized = False
        self._reload = False
        self._reload_cursors: List[_Cursor] = []
        self._reload_index = 0
        self._cursors: Dict[Tuple[str, str], _Cursor] = {}
        self._structure: Tuple[Tuple[str, str, _Identity], ...] = ()
        self._telemetry = {zone: [] for zone in _ZONE_IDS}
        self._analysis = {zone: [] for zone in _ZONE_IDS}
        self._latest_telemetry: Optional[datetime] = None
        self._latest_analysis: Optional[datetime] = None
        self._sequence = 0
        self._pending_bytes = 0
        self._work_bytes = 0
        self._errors: List[str] = []

    @property
    def last_work_bytes(self) -> int:
        return self._work_bytes

    @property
    def max_work_bytes(self) -> int:
        return _MAX_WORK_BYTES

    def update(self, window_hours: float) -> Tuple[Optional[Dict[str, Any]], bool]:
        self._work_bytes = 0
        files, structure = self._discover()
        if (
            not self._initialized
            or window_hours > self.window_hours
            or structure != self._structure
            or self._is_truncated(files)
        ):
            if (
                not self._reload
                or structure != self._structure
                or window_hours != self.window_hours
            ):
                self._begin_reload(files, structure, window_hours)
            complete = self._scan_reload_step()
            if not complete:
                return None, True
            self._initialized = True
            self._reload = False
            self.window_hours = window_hours
            self._finalize()
            return self._result(), False

        if window_hours < self.window_hours:
            self.window_hours = window_hours
            self._prune()
            self._finalize()
            return self._result(), False

        changed, more = self._read_appends(files)
        if more:
            return None, True
        if not changed:
            if self._errors:
                return self._result(), False
            return None, False
        self._prune()
        self._finalize()
        return self._result(), False

    def _discover(self):
        files = []
        structure = []
        for category in ("telemetry", "analysis"):
            for path in _log_files(self.logs_dir, category):
                try:
                    identity, size, modified_ns = _identity(path)
                except OSError as exc:
                    self._add_error(f"{path.name}: {type(exc).__name__}: {exc}")
                    continue
                files.append((category, path, identity, size, modified_ns))
                structure.append((category, path.name, identity))
        structure.sort()
        return files, tuple(structure)

    def _is_truncated(self, files) -> bool:
        for category, path, identity, size, modified_ns in files:
            cursor = self._cursors.get((category, path.name))
            if (
                cursor is None
                or cursor.identity != identity
                or size < cursor.offset
                or (size == cursor.offset and modified_ns != cursor.modified_ns)
            ):
                return True
        return len(files) != len(self._cursors)

    def _begin_reload(self, files, structure, window_hours: float) -> None:
        self._reload = True
        self._initialized = False
        self.window_hours = window_hours
        self._structure = structure
        self._reload_index = 0
        self._reload_cursors = [
            _Cursor(category, path, identity, size, modified_ns)
            for category, path, identity, size, modified_ns in files
        ]
        self._cursors = {}
        self._telemetry = {zone: [] for zone in _ZONE_IDS}
        self._analysis = {zone: [] for zone in _ZONE_IDS}
        self._latest_telemetry = None
        self._latest_analysis = None
        self._sequence = 0
        self._pending_bytes = 0
        self._work_bytes = 0
        self._errors = []

    def _scan_reload_step(self) -> bool:
        remaining = _MAX_WORK_BYTES
        while self._reload_index < len(self._reload_cursors) and remaining > 0:
            cursor = self._reload_cursors[self._reload_index]
            if cursor.offset >= cursor.target_size:
                self._cursors[(cursor.category, cursor.path.name)] = cursor
                self._reload_index += 1
                continue
            read_size = min(_CHUNK_BYTES, remaining, cursor.target_size - cursor.offset)
            try:
                with cursor.path.open("rb") as stream:
                    stream.seek(cursor.offset)
                    chunk = stream.read(read_size)
            except OSError as exc:
                self._add_error(f"{cursor.path.name}: {type(exc).__name__}: {exc}")
                cursor.offset = cursor.target_size
                continue
            if not chunk:
                cursor.offset = cursor.target_size
                continue
            cursor.offset += len(chunk)
            remaining -= len(chunk)
            self._pending_bytes += len(chunk)
            self._work_bytes += len(chunk)
            self._consume_chunk(cursor, chunk)
            self._prune()

        while self._reload_index < len(self._reload_cursors):
            cursor = self._reload_cursors[self._reload_index]
            if cursor.offset < cursor.target_size:
                return False
            self._cursors[(cursor.category, cursor.path.name)] = cursor
            self._reload_index += 1
        return True

    def _read_appends(self, files) -> Tuple[bool, bool]:
        remaining = _MAX_WORK_BYTES
        changed = False
        more = False
        for category, path, _, size, modified_ns in files:
            cursor = self._cursors[(category, path.name)]
            cursor.target_size = size
            read_error = False
            while cursor.offset < size and remaining > 0:
                read_size = min(_CHUNK_BYTES, remaining, size - cursor.offset)
                try:
                    with path.open("rb") as stream:
                        stream.seek(cursor.offset)
                        chunk = stream.read(read_size)
                except OSError as exc:
                    self._add_error(f"{path.name}: {type(exc).__name__}: {exc}")
                    changed = True
                    read_error = True
                    break
                if not chunk:
                    break
                cursor.offset += len(chunk)
                remaining -= len(chunk)
                self._pending_bytes += len(chunk)
                self._work_bytes += len(chunk)
                changed = True
                self._consume_chunk(cursor, chunk)
                self._prune()
            if cursor.offset < size:
                more = not read_error
                if remaining <= 0 or read_error:
                    break
            else:
                cursor.modified_ns = modified_ns
        if not more:
            more = any(
                self._cursors[(category, path.name)].offset < size
                for category, path, _, size, _ in files
            )
        return changed, more

    def _consume_chunk(self, cursor: _Cursor, chunk: bytes) -> None:
        data = cursor.pending + chunk
        if cursor.discarding_line:
            if b"\n" not in data:
                cursor.pending = b""
                return
            _, _, data = data.partition(b"\n")
            cursor.discarding_line = False

        pieces = data.split(b"\n")
        for line in pieces[:-1]:
            self._consume_line(cursor.category, line.rstrip(b"\r"))
        cursor.pending = pieces[-1]
        if len(cursor.pending) > _MAX_PARTIAL_LINE_BYTES:
            message = f"{cursor.path.name}: incomplete JSONL record exceeded the line limit"
            self._add_error(message)
            cursor.pending = b""
            cursor.discarding_line = True

    def _consume_line(self, category: str, line: bytes) -> None:
        if not line.strip():
            return
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._add_error(f"Malformed {category} JSONL record: {exc}")
            return
        if not isinstance(obj, dict):
            self._add_error(f"Malformed {category} JSONL record: root is not an object")
            return

        timestamp = parse_iso_timestamp(obj.get("timestamp_pacific") or obj.get("ts"))
        if timestamp is None:
            self._add_error(f"Malformed {category} JSONL record: missing or invalid timestamp")
            return
        self._sequence += 1
        if category == "telemetry":
            zones = _zone_mapping(obj)
            for zone_id in _ZONE_IDS:
                zone = _zone_value(zones, zone_id)
                pv = _numeric(_first_value(zone, "pv_c", "pv", "PV", "process_value_c", "process_value"))
                sp = _numeric(_first_value(zone, "sp_abs_c", "sp_abs", "sp_c", "sp", "SP_abs", "SP", "setpoint_c", "setpoint"))
                autotune_sp = _numeric(_first_value(zone, "autotune_sp_c", "autotune_sp", "sp_autotune_c", "sp_autotune"))
                if pv is not None or sp is not None or autotune_sp is not None:
                    self._telemetry[zone_id].append((timestamp, self._sequence, pv, sp, autotune_sp))
            self._latest_telemetry = max(
                (value for value in (self._latest_telemetry, timestamp) if value is not None),
                key=lambda value: value.timestamp(),
            )
        else:
            zones = obj.get("analysis", {})
            if not isinstance(zones, dict):
                return
            nested_zones = zones.get("zones")
            if isinstance(nested_zones, dict):
                zones = nested_zones
            for zone_id in _ZONE_IDS:
                zone = _zone_value(zones, zone_id)
                mae = _numeric(_first_value(zone, "avg_abs_error_c", "mae", "mean_abs_error_c"))
                if mae is not None:
                    self._analysis[zone_id].append((timestamp, self._sequence, mae))
            self._latest_analysis = max(
                (value for value in (self._latest_analysis, timestamp) if value is not None),
                key=lambda value: value.timestamp(),
            )

    def _add_error(self, message: str) -> None:
        if message not in self._errors and len(self._errors) < _MAX_REPORTED_ERRORS:
            self._errors.append(message)

    def _prune(self) -> None:
        telemetry_cutoff = (
            self._latest_telemetry.timestamp() - self.window_hours * 3600.0
            if self._latest_telemetry is not None
            else None
        )
        analysis_cutoff = (
            self._latest_analysis.timestamp() - self.window_hours * 3600.0
            if self._latest_analysis is not None
            else None
        )
        for zone_id in _ZONE_IDS:
            if telemetry_cutoff is not None:
                self._telemetry[zone_id] = [
                    row for row in self._telemetry[zone_id] if row[0].timestamp() >= telemetry_cutoff
                ]
            if analysis_cutoff is not None:
                self._analysis[zone_id] = [
                    row for row in self._analysis[zone_id] if row[0].timestamp() >= analysis_cutoff
                ]
            if len(self._telemetry[zone_id]) > _MAX_RAW_POINTS_PER_ZONE:
                self._telemetry[zone_id].sort(key=lambda row: (row[0].timestamp(), row[1]))
                self._telemetry[zone_id] = self._compact_rows(self._telemetry[zone_id], _MAX_RAW_POINTS_PER_ZONE)
            if len(self._analysis[zone_id]) > _MAX_RAW_POINTS_PER_ZONE:
                self._analysis[zone_id].sort(key=lambda row: (row[0].timestamp(), row[1]))
                self._analysis[zone_id] = self._compact_rows(self._analysis[zone_id], _MAX_RAW_POINTS_PER_ZONE)

    @staticmethod
    def _compact_rows(rows: List[Tuple[Any, ...]], max_points: int) -> List[Tuple[Any, ...]]:
        indexes = _downsample_indices(rows, max_points // 2)
        return [rows[index] for index in indexes]

    def _finalize(self) -> None:
        for zone_id in _ZONE_IDS:
            self._telemetry[zone_id].sort(key=lambda row: (row[0].timestamp(), row[1]))
            self._analysis[zone_id].sort(key=lambda row: (row[0].timestamp(), row[1]))

    def _result(self) -> Dict[str, Any]:
        telemetry = {}
        analysis = {}
        for zone_id in _ZONE_IDS:
            rows = self._telemetry[zone_id]
            analysis_rows = self._analysis[zone_id]
            if len(rows) > _MAX_RENDER_POINTS_PER_ZONE:
                rows = [rows[index] for index in _downsample_indices(rows, _MAX_RENDER_POINTS_PER_ZONE)]
            if len(analysis_rows) > _MAX_RENDER_POINTS_PER_ZONE:
                analysis_rows = [
                    analysis_rows[index]
                    for index in _downsample_indices(analysis_rows, _MAX_RENDER_POINTS_PER_ZONE)
                ]
            telemetry[zone_id] = {
                "times": [row[0] for row in rows],
                "pv": [row[2] for row in rows],
                "sp": [row[3] for row in rows],
                "sp_autotune": [row[4] for row in rows],
            }
            analysis[zone_id] = {
                "times": [row[0] for row in analysis_rows],
                "mae": [row[2] for row in analysis_rows],
            }
        result = {
            "telemetry": telemetry,
            "analysis": analysis,
            "history_hours": self.window_hours,
            "latest_telemetry": self._latest_telemetry,
            "latest_analysis": self._latest_analysis,
            "bytes_read": self._pending_bytes,
            "work_bytes": self._work_bytes,
            "errors": list(dict.fromkeys(self._errors)),
        }
        self._pending_bytes = 0
        self._errors = []
        return result


class IncrementalHistoryReader:
    """Keep one bounded history worker and only the newest completed snapshot."""

    def __init__(self, logs_dir: Path, *, poll_interval_s: float = 0.25):
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        self._engine = _HistoryEngine(Path(logs_dir))
        self._poll_interval_s = float(poll_interval_s)
        self._condition = threading.Condition()
        self._window_hours: Optional[float] = None
        self._revision = 0
        self._active = True
        self._stopping = False
        self._results: queue.Queue[Dict[str, Any]] = queue.Queue(maxsize=1)
        self._worker = threading.Thread(target=self._run, name="cn616a-chart-history", daemon=True)
        self._worker.start()

    def request(self, history_hours: float) -> int:
        value = float(history_hours)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("history_hours must be positive and finite")
        with self._condition:
            if self._stopping:
                raise RuntimeError("chart history reader is shut down")
            if self._window_hours != value:
                self._window_hours = value
                self._revision += 1
                self._condition.notify()
            return self._revision

    def poll_results(self) -> List[Dict[str, Any]]:
        results = []
        while True:
            try:
                results.append(self._results.get_nowait())
            except queue.Empty:
                return results

    def set_active(self, active: bool) -> None:
        with self._condition:
            self._active = bool(active)
            self._condition.notify()

    @property
    def max_work_bytes(self) -> int:
        return _MAX_WORK_BYTES

    def shutdown(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        self._worker.join(timeout=3.0)
        if self._worker.is_alive():
            LOGGER.error("Chart history worker did not stop promptly")

    def _run(self) -> None:
        processed_revision = 0
        while True:
            with self._condition:
                if not self._stopping and (
                    not self._active
                    or self._window_hours is None
                    or self._revision == processed_revision
                ):
                    self._condition.wait(timeout=self._poll_interval_s)
                if self._stopping:
                    return
                if not self._active:
                    continue
                history_hours = self._window_hours
                revision = self._revision
            if history_hours is None:
                continue
            try:
                result, more_work = self._engine.update(history_hours)
            except Exception as exc:
                LOGGER.exception("Incremental chart history update failed")
                self._engine = _HistoryEngine(self._engine.logs_dir)
                result = {
                    "history_hours": history_hours,
                    "errors": [f"{type(exc).__name__}: {exc}"],
                    "bytes_read": 0,
                }
                more_work = False
            if result is not None:
                result["revision"] = revision
                self._publish(result)
            processed_revision = revision
            if more_work:
                with self._condition:
                    if not self._stopping and revision == self._revision:
                        self._condition.wait(timeout=0.005)
                continue

    def _publish(self, result: Dict[str, Any]) -> None:
        try:
            self._results.put_nowait(result)
        except queue.Full:
            try:
                self._results.get_nowait()
            except queue.Empty:
                pass
            try:
                self._results.put_nowait(result)
            except queue.Full:
                LOGGER.warning("Dropping an obsolete chart history snapshot")
