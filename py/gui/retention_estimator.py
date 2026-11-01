"""Bounded log sampling and retention estimates for the Configuration panel."""

from __future__ import annotations

import json
import logging
import math
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


LOGGER = logging.getLogger("cn616a.gui")
MAX_SAMPLE_BYTES = 64 * 1024
MAX_SAMPLE_RECORDS = 256
MAX_SAMPLE_FILES = 3
MIN_CONFIDENT_RECORDS = 20


@dataclass(frozen=True)
class _EstimateRequest:
    revision: int
    specs: Tuple[Tuple[str, str, Optional[float], Optional[float], Optional[int], int], ...]


class RetentionEstimateWorker:
    """Recompute bounded estimates only when inputs or sampled-file metadata change."""

    def __init__(self, logs_dir: Path, *, poll_interval_s: float = 1.0):
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        self.logs_dir = Path(logs_dir)
        self.poll_interval_s = float(poll_interval_s)
        self._condition = threading.Condition()
        self._request: Optional[_EstimateRequest] = None
        self._revision = 0
        self._stopping = False
        self._results: queue.Queue[Tuple[int, Tuple[Any, ...], Dict[str, Any]]] = queue.Queue(maxsize=1)
        self._worker = threading.Thread(target=self._run, name="cn616a-retention-estimator", daemon=True)
        self._worker.start()

    def request(
        self,
        specs: Dict[str, Tuple[str, Optional[float], Optional[float], Optional[int], int]],
    ) -> int:
        normalized = tuple(
            (key, *specs[key])
            for key in sorted(specs)
        )
        with self._condition:
            if self._stopping:
                raise RuntimeError("retention estimator is shut down")
            if self._request is not None and normalized == self._request.specs:
                return self._request.revision
            self._revision += 1
            self._request = _EstimateRequest(self._revision, normalized)
            self._condition.notify()
            return self._revision

    def poll_results(self) -> List[Tuple[int, Tuple[Any, ...], Dict[str, Any]]]:
        results = []
        while True:
            try:
                results.append(self._results.get_nowait())
            except queue.Empty:
                return results

    def shutdown(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        self._worker.join(timeout=max(2.0, self.poll_interval_s + 1.0))
        if self._worker.is_alive():
            LOGGER.error("Retention estimate worker did not stop promptly")

    def _run(self) -> None:
        processed_revision = 0
        previous_signature: Optional[Tuple[Any, ...]] = None
        while True:
            with self._condition:
                if not self._stopping and (
                    self._request is None or self._request.revision == processed_revision
                ):
                    self._condition.wait(timeout=self.poll_interval_s)
                if self._stopping:
                    return
                request = self._request
            if request is None:
                continue

            signature = tuple(
                (key, source_signature(self.logs_dir, log_name))
                for key, log_name, *_ in request.specs
            )
            if request.revision == processed_revision and signature == previous_signature:
                continue

            try:
                estimates = {
                    key: estimate_log_storage(
                        self.logs_dir,
                        log_name,
                        retention_s,
                        rate_hz,
                        max_bytes,
                        fallback_bytes,
                    )
                    for key, log_name, retention_s, rate_hz, max_bytes, fallback_bytes in request.specs
                }
            except Exception as exc:
                LOGGER.exception("Retention estimate worker failed")
                estimates = {
                    key: {"error": f"{type(exc).__name__}: {exc}"}
                    for key, *_ in request.specs
                }
            processed_revision = request.revision
            previous_signature = signature
            self._publish((request.revision, request.specs, estimates))

    def _publish(self, result: Tuple[int, Tuple[Any, ...], Dict[str, Any]]) -> None:
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
                LOGGER.warning("Dropping an obsolete retention estimate result")


def _selected_files(logs_dir: Path, log_name: str) -> List[Path]:
    path = Path(log_name)
    active = logs_dir / path.name
    try:
        rotated = list(logs_dir.glob(f"{path.stem}_*{path.suffix}"))
    except OSError:
        LOGGER.exception("Unable to list rotated log files in %s", logs_dir)
        rotated = []

    def modified_ns(candidate: Path) -> int:
        try:
            return candidate.stat().st_mtime_ns
        except OSError:
            return -1

    rotated.sort(key=modified_ns, reverse=True)
    selected = ([active] if active.is_file() else []) + [
        candidate for candidate in rotated if candidate != active
    ][: MAX_SAMPLE_FILES - 1]
    return selected[:MAX_SAMPLE_FILES]


def source_signature(logs_dir: Path, log_name: str) -> Tuple[Tuple[str, int, int, int], ...]:
    """Return bounded-file metadata for change detection without reading log contents."""
    signature = []
    for path in _selected_files(logs_dir, log_name):
        try:
            stat = path.stat()
            signature.append((path.name, stat.st_size, stat.st_mtime_ns, getattr(stat, "st_ino", 0)))
        except OSError:
            signature.append((path.name, -1, -1, -1))
    return tuple(signature)


def _valid_record_lengths(
    chunk: bytes,
    *,
    starts_at_file_beginning: bool,
    ends_at_file_end: bool,
    max_records: int,
) -> List[int]:
    lines = chunk.splitlines(keepends=True)
    if not starts_at_file_beginning and lines and not chunk.startswith(b"\n"):
        lines = lines[1:]
    if not ends_at_file_end and lines and not lines[-1].endswith(b"\n"):
        lines.pop()

    lengths = []
    for line in lines:
        if len(lengths) >= max_records:
            break
        payload = line.strip()
        if not payload:
            continue
        try:
            json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        lengths.append(len(line))
    return lengths


def _sample_records(
    logs_dir: Path,
    log_name: str,
) -> Tuple[List[int], int, Tuple[Tuple[str, int, int, int], ...], List[str]]:
    files = _selected_files(logs_dir, log_name)
    signature_parts = []
    errors = []
    for path in files:
        try:
            stat = path.stat()
            signature_parts.append((path.name, stat.st_size, stat.st_mtime_ns, getattr(stat, "st_ino", 0)))
        except OSError as exc:
            signature_parts.append((path.name, -1, -1, -1))
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")

    record_lengths: List[int] = []
    bytes_read = 0
    remaining_bytes = MAX_SAMPLE_BYTES
    for path in files:
        if not remaining_bytes or len(record_lengths) >= MAX_SAMPLE_RECORDS:
            break
        try:
            file_size = path.stat().st_size
            budget = min(remaining_bytes, max(1, MAX_SAMPLE_BYTES // max(1, len(files))))
            with path.open("rb") as stream:
                if file_size <= budget:
                    chunk = stream.read(budget)
                    bytes_read += len(chunk)
                    record_lengths.extend(
                        _valid_record_lengths(
                            chunk,
                            starts_at_file_beginning=True,
                            ends_at_file_end=True,
                            max_records=MAX_SAMPLE_RECORDS - len(record_lengths),
                        )
                    )
                else:
                    head_size = max(1, (budget - 1) // 2)
                    tail_size = budget - head_size - 1
                    head = stream.read(head_size)
                    bytes_read += len(head)
                    record_lengths.extend(
                        _valid_record_lengths(
                            head,
                            starts_at_file_beginning=True,
                            ends_at_file_end=False,
                            max_records=MAX_SAMPLE_RECORDS - len(record_lengths),
                        )
                    )
                    if tail_size and len(record_lengths) < MAX_SAMPLE_RECORDS:
                        tail_offset = max(0, file_size - tail_size)
                        stream.seek(tail_offset - 1)
                        preceding_byte = stream.read(1) if tail_offset else b""
                        tail = stream.read(tail_size)
                        bytes_read += len(preceding_byte) + len(tail)
                        record_lengths.extend(
                            _valid_record_lengths(
                                tail,
                                starts_at_file_beginning=not tail_offset or preceding_byte == b"\n",
                                ends_at_file_end=True,
                                max_records=MAX_SAMPLE_RECORDS - len(record_lengths),
                            )
                        )
            remaining_bytes -= budget
        except OSError as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            LOGGER.warning("Unable to sample log records from %s: %s", path, exc)

    return (
        record_lengths[:MAX_SAMPLE_RECORDS],
        bytes_read,
        tuple(signature_parts),
        errors,
    )


def estimate_log_storage(
    logs_dir: Path,
    log_name: str,
    retention_s: Optional[float],
    rate_hz: Optional[float],
    max_bytes: Optional[int],
    fallback_record_bytes: int,
) -> Optional[Dict[str, Any]]:
    """Estimate retained bytes with an explicit bounded sample and confidence indicator."""
    if retention_s is None or rate_hz is None or max_bytes is None:
        return None
    if (
        not math.isfinite(retention_s)
        or not math.isfinite(rate_hz)
        or retention_s <= 0
        or rate_hz <= 0
        or max_bytes <= 0
    ):
        return None

    logs_dir = Path(logs_dir)
    lengths, bytes_read, signature, errors = _sample_records(logs_dir, log_name)
    if source_signature(logs_dir, log_name) != signature:
        errors.append("log files changed while the sample was being read")
    sampled_records = len(lengths)
    average_record_bytes = (
        sum(lengths) / sampled_records if sampled_records else float(fallback_record_bytes)
    )
    records = retention_s * rate_hz
    if not math.isfinite(records):
        return None
    estimated_bytes = max(1, math.ceil(records * average_record_bytes))
    file_count = max(1, math.ceil(estimated_bytes / max_bytes))
    if errors:
        sample_status = "read_error"
    elif sampled_records == 0:
        sample_status = "fallback"
    elif sampled_records < MIN_CONFIDENT_RECORDS:
        sample_status = "limited"
    else:
        sample_status = "sampled"

    return {
        "records": records,
        "average_record_bytes": average_record_bytes,
        "estimated_bytes": estimated_bytes,
        "file_count": file_count,
        "capacity_bytes": file_count * max_bytes,
        "sampled_records": sampled_records,
        "sampled_files": len(signature),
        "sampled_bytes": bytes_read,
        "sample_status": sample_status,
        "file_count_confident": sample_status == "sampled",
        "source_signature": signature,
        "sample_errors": errors,
    }
