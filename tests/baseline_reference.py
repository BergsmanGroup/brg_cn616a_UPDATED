"""Versioned synthetic Modbus RTU inputs, independent of service/GUI JSON."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import math
import struct
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = Path(__file__).parent / "fixtures" / "cn616a-baseline-v1"
REFERENCE_FILE = "modbus-reference.json.gz"
MANIFEST_FILE = "manifest.json"
SCHEMA = "cn616a-modbus-rtu-reference-v1"
ZONES = tuple(range(1, 7))


def crc16(payload: bytes) -> int:
    crc = 0xFFFF
    for byte in payload:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _with_crc(payload: bytes) -> bytes:
    return payload + struct.pack("<H", crc16(payload))


def _transaction(address: int, registers: list[int]) -> dict[str, str]:
    request = struct.pack(">BBHH", 1, 3, address, len(registers))
    response = struct.pack(">BBB", 1, 3, len(registers) * 2)
    response += struct.pack(f">{len(registers)}H", *registers)
    return {"request_hex": _with_crc(request).hex(), "response_hex": _with_crc(response).hex()}


def transaction_registers(transaction: dict[str, str]) -> dict[int, int]:
    request = bytes.fromhex(transaction["request_hex"])
    response = bytes.fromhex(transaction["response_hex"])
    if len(request) != 8 or len(response) < 5:
        raise ValueError("Invalid Modbus RTU frame length")
    for frame in (request, response):
        if crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
            raise ValueError("Modbus RTU reference CRC mismatch")
    unit, function, address, count = struct.unpack(">BBHH", request[:-2])
    if unit != 1 or function != 3 or not 1 <= count <= 120:
        raise ValueError("Reference must contain unit-1 FC03 read transactions only")
    if response[:2] != request[:2] or response[2] != count * 2:
        raise ValueError("Reference request/response identity or byte-count mismatch")
    if len(response) != 5 + count * 2:
        raise ValueError("Truncated or oversized reference response")
    values = struct.unpack(f">{count}H", response[3:-2])
    return {address + index: word for index, word in enumerate(values)}


def _put_float(words: list[int], offset: int, value: float) -> None:
    words[offset : offset + 2] = struct.unpack(">HH", struct.pack(">f", value))


def build_reference(destination: Path) -> dict[str, Any]:
    """Generate v1 once. Never overwrite an existing immutable reference."""
    archive_path = destination / REFERENCE_FILE
    manifest_path = destination / MANIFEST_FILE
    if archive_path.exists() or manifest_path.exists():
        raise FileExistsError("Reference already exists; create a new version instead")
    register_map = json.loads((REPO_ROOT / "cn616a_register_map.json").read_text(encoding="utf-8"))
    layout = register_map["registers"]
    system = [0] * 27
    for address, value in {5: 6, 7: 1, 10: 1, 11: 1, 12: 63}.items():
        system[address - 1] = value
    static = [_transaction(1, system), _transaction(0x0180, [0] * 14)]
    calibration = [0] * 12
    static.append(_transaction(int(layout["user_calibration"]["zone_1_index_hex"], 16), calibration))
    for zone in ZONES:
        zone_words = [0] * 18
        for field in layout["zone_registers"]["fields"]:
            if field["type"] == "F":
                _put_float(zone_words, int(field["offset_hex"], 16), field["default"])
        static.append(
            _transaction(int(layout["zone_registers"]["zone_bases_index_hex"][str(zone)], 16), zone_words)
        )
        profile = [0] * 120
        for segment in range(20):
            _put_float(profile, segment * 6, 70.0 + zone)
            _put_float(profile, segment * 6 + 2, 1.0)
            _put_float(profile, segment * 6 + 4, 0.5)
        static.append(
            _transaction(int(layout["profile_registers"]["profile_bases_index_hex"][str(zone)], 16), profile)
        )

    samples = []
    for sequence in range(16):
        pv_words = [0] * 12
        transactions = []
        for zone in ZONES:
            pv = round(20 + zone + 2 * math.sin(sequence * math.pi / 8 + zone), 3)
            if sequence == 0 and zone == 1:
                pv = 0.0
            _put_float(pv_words, (zone - 1) * 2, pv)
            pid = [0] * 29
            pid_values = {
                "num_segments": 20, "control_method": 1, "loop_status": 2,
                "sp_abs": 70.0 + zone, "autotune_sp": 70.0 + zone,
                "out_pct": 40.0 + zone, "p_gain": 1.0, "cycle_time_s": 5.0,
            }
            for field in layout["pid_registers"]["fields"]:
                offset = int(field["offset_hex"], 16)
                value = pid_values.get(field["name"], 0)
                if field["type"] == "F":
                    _put_float(pid, offset, value)
                else:
                    pid[offset] = int(value)
            transactions.append(
                _transaction(int(layout["pid_registers"]["zone_bases_index_hex"][str(zone)], 16), pid)
            )
        transactions.insert(0, _transaction(int(layout["temperature_pv"]["pattern"]["zone_1_index_hex"], 16), pv_words))
        samples.append({"elapsed_ms": sequence * 500, "transactions": transactions})
    reference = {
        "schema": SCHEMA,
        "provenance": "synthetic; not captured from a physical controller",
        "transport": "Modbus RTU; FC03; CRC16 little-endian; register words big-endian",
        "sample_period_ms": 500,
        "zones": list(ZONES),
        "register_map": register_map,
        "static_transactions": static,
        "samples": samples,
    }
    return save_reference(reference, destination)


def save_reference(reference: dict[str, Any], destination: Path) -> dict[str, Any]:
    """Persist a lossless reference without replacing an existing corpus."""
    archive_path = destination / REFERENCE_FILE
    manifest_path = destination / MANIFEST_FILE
    if archive_path.exists() or manifest_path.exists():
        raise FileExistsError("Reference already exists; create a new version instead")
    for transaction in reference["static_transactions"]:
        transaction_registers(transaction)
    for sample in reference["samples"]:
        for transaction in sample["transactions"]:
            transaction_registers(transaction)
    content = json.dumps(reference, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    compressed_buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed_buffer, mode="wb", filename="", mtime=0, compresslevel=9) as stream:
        stream.write(content)
    compressed = compressed_buffer.getvalue()
    manifest = {
        "schema": SCHEMA,
        "reference_file": REFERENCE_FILE,
        "archive_sha256": hashlib.sha256(compressed).hexdigest(),
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "archive_bytes": len(compressed),
        "uncompressed_bytes": len(content),
        "sample_count": len(reference["samples"]),
        "sample_period_ms": reference["sample_period_ms"],
        "provenance": reference["provenance"],
    }
    destination.mkdir(parents=True, exist_ok=True)
    archive_path.write_bytes(compressed)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


class CaptureStream(Protocol):
    def write(self, data: bytes) -> int: ...
    def read(self, size: int) -> bytes: ...


def capture_transaction(stream: CaptureStream, request_hex: str) -> dict[str, str]:
    """Record actual FC03 response bytes; never reconstruct responses from values."""
    request = bytes.fromhex(request_hex)
    if len(request) != 8 or crc16(request[:-2]) != int.from_bytes(request[-2:], "little"):
        raise ValueError("Invalid raw capture request framing or CRC")
    unit, function, _, count = struct.unpack(">BBHH", request[:-2])
    if unit != 1 or function != 3 or not 1 <= count <= 120:
        raise ValueError("Raw capture permits only unit-1 FC03 reads")
    if stream.write(request) != len(request):
        raise RuntimeError("Incomplete raw capture request write")

    def read_exact(size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            chunk = stream.read(size - len(result))
            if not chunk:
                raise TimeoutError(f"Raw capture timed out reading {size} bytes (received {len(result)})")
            result.extend(chunk)
        return bytes(result)

    header = read_exact(3)
    if header[1] & 0x80:
        response = header + read_exact(2)
        raise RuntimeError(f"Controller returned Modbus exception: {response.hex()}")
    if header[:2] != request[:2] or header[2] != int.from_bytes(request[4:6], "big") * 2:
        raise ValueError(f"Unexpected raw capture response header: {header.hex()}")
    transaction = {"request_hex": request.hex(), "response_hex": (header + read_exact(header[2] + 2)).hex()}
    transaction_registers(transaction)
    return transaction


def capture_reference(
    destination: Path, *, port: str, controller_offline: bool,
    duration_seconds: float = 60.0, baudrate: int = 115200, timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    """Read a unit-1 controller exclusively; never stop or reconfigure a service."""
    if not controller_offline:
        raise ValueError("Raw capture requires --controller-offline: stop the service yourself or use a separate controller")
    if destination.exists():
        raise FileExistsError("Capture destination must be a new directory; existing references are immutable")
    if not port.strip() or baudrate <= 0:
        raise ValueError("An explicit serial port and positive baud rate are required")
    if not all(math.isfinite(value) and value > 0 for value in (duration_seconds, timeout_seconds)):
        raise ValueError("Capture duration and timeout must be finite and positive")
    import serial

    template, _ = load_reference()
    reference = {
        **template,
        "provenance": "physical unit-1 controller; read-only FC03 raw serial capture",
        "static_transactions": [],
        "samples": [],
    }
    frame_gap = max(3.5 * 10 / baudrate, 0.00175)
    # No saved service settings or automatic port discovery are used.
    with serial.Serial(
        port=port, baudrate=baudrate, bytesize=8, parity="N", stopbits=1,
        timeout=timeout_seconds, write_timeout=timeout_seconds, exclusive=True,
    ) as stream:
        def read_transaction(transaction: dict[str, str]) -> dict[str, str]:
            captured = capture_transaction(stream, transaction["request_hex"])
            time.sleep(frame_gap)
            return captured

        reference["static_transactions"] = [
            read_transaction(transaction)
            for transaction in template["static_transactions"]
        ]
        started = time.monotonic()
        period = template["sample_period_ms"] / 1000.0
        while time.monotonic() - started < duration_seconds:
            sample_started = time.monotonic()
            reference["samples"].append({
                "elapsed_ms": round((sample_started - started) * 1000),
                "transactions": [
                    read_transaction(transaction)
                    for transaction in template["samples"][0]["transactions"]
                ],
            })
            time.sleep(max(0.0, period - (time.monotonic() - sample_started)))
    if not reference["samples"]:
        raise RuntimeError("Raw capture produced no complete telemetry snapshots")
    destination.mkdir(parents=True, exist_ok=False)
    return save_reference(reference, destination)


def load_reference(directory: Path = REFERENCE_DIR) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8"))
    compressed = (directory / REFERENCE_FILE).read_bytes()
    if manifest["schema"] != SCHEMA or manifest["reference_file"] != REFERENCE_FILE:
        raise ValueError("Unsupported reference manifest")
    if len(compressed) != manifest["archive_bytes"] or hashlib.sha256(compressed).hexdigest() != manifest["archive_sha256"]:
        raise ValueError("Compressed reference size or SHA-256 mismatch")
    content = gzip.decompress(compressed)
    if len(content) != manifest["uncompressed_bytes"] or hashlib.sha256(content).hexdigest() != manifest["content_sha256"]:
        raise ValueError("Reference content size or SHA-256 mismatch")
    reference = json.loads(content)
    if reference["schema"] != SCHEMA or len(reference["samples"]) != manifest["sample_count"]:
        raise ValueError("Reference schema or sample-count mismatch")
    for transaction in reference["static_transactions"]:
        transaction_registers(transaction)
    for sample in reference["samples"]:
        for transaction in sample["transactions"]:
            transaction_registers(transaction)
    return reference, manifest


@dataclass
class AdaptedReference:
    telemetry: list[dict[str, Any]]
    config: dict[str, Any]
    rampsoak: dict[str, Any]
    manifest: dict[str, Any]


def adapt_reference(directory: Path = REFERENCE_DIR) -> AdaptedReference:
    """Decode wire inputs through the current driver using offline register reads."""
    if str(REPO_ROOT / "py") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "py"))
    from cn616a import CN616A

    reference, manifest = load_reference(directory)

    class OfflineController(CN616A):
        words: dict[int, int]
        missing_reads: list[int]

        def connect(self) -> None:
            raise RuntimeError("The baseline reference cannot open a serial connection")

        def read_block(self, start_addr: int, count: int) -> list[int]:
            missing = [address for address in range(start_addr, start_addr + count) if address not in self.words]
            if missing:
                self.missing_reads.extend(missing)
                raise ValueError(f"Raw reference does not cover registers: {missing}")
            return [self.words[address] for address in range(start_addr, start_addr + count)]

        def read_u16_addr(self, addr: int) -> int:
            return self.read_block(addr, 1)[0]

        def read_f32_addr(self, addr: int) -> float:
            words = self.read_block(addr, 2)
            return self._regs_to_float((words[0], words[1]))

    with tempfile.TemporaryDirectory(prefix="cn616a-reference-map-") as temporary:
        map_path = Path(temporary) / "frozen-register-map.json"
        map_path.write_text(json.dumps(reference["register_map"]), encoding="utf-8")
        controller = OfflineController("SYNTHETIC-OFFLINE", register_map_path=map_path, retries=0)
        static: dict[int, int] = {}
        for transaction in reference["static_transactions"]:
            static.update(transaction_registers(transaction))
        telemetry = []
        for sample in reference["samples"]:
            controller.words = dict(static)
            controller.missing_reads = []
            for transaction in sample["transactions"]:
                controller.words.update(transaction_registers(transaction))
            telemetry.append(controller.read_telemetry(ZONES))
            if controller.missing_reads:
                raise ValueError(f"Driver requested uncaptured registers: {controller.missing_reads}")
        config = controller.read_config(ZONES)
        rampsoak = controller.read_rampsoak_all(ZONES)
        if controller.missing_reads:
            raise ValueError(f"Driver requested uncaptured registers: {controller.missing_reads}")
    return AdaptedReference(telemetry, config, rampsoak, manifest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a new immutable synthetic raw Modbus reference.")
    parser.add_argument("--output-dir", type=Path, required=True, help="Destination must not contain an existing v1 reference.")
    options = parser.parse_args()
    print(json.dumps(build_reference(options.output_dir), indent=2))
