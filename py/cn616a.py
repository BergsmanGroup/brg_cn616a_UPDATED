"""
cn616a.py - Backend driver for Omega CN616A (Modbus RTU), schema-locked API.

Key goals:
- No hardcoded register addresses in the driver: load cn616a_register_map.json.
- Provide stable high-level API returning dicts that match your JSON schemas:
    - telemetry: PV + PID/Control/RampSoak control block + bitmaps
    - config: system + zone alarm/scaling/calibration + per-zone sensor status
    - ramp/soak: 20-segment tables (on demand)

Compatibility assumptions (matching your current working driver):
- Address passed to pymodbus is the CN616A's 40000-based "index" (e.g. 0x0100 for PV z1).
- pymodbus 2.5.3 sync client and keyword argument is `unit=` (not `slave=`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union
import json
import struct
import time

from pymodbus.client.sync import ModbusSerialClient
from pymodbus.exceptions import ModbusException


Number = Union[int, float]


class CN616AError(RuntimeError):
    pass


@dataclass
class SerialParams:
    baudrate: int = 115200
    parity: str = "N"       # 'N', 'E', 'O'
    stopbits: int = 1
    bytesize: int = 8
    timeout: float = 1.0    # seconds


def _hex_to_int(x: str) -> int:
    return int(x, 16)


def _safe_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


class CN616A:
    """
    CN616A Modbus driver (schema-locked).

    The service layer should call:
      - read_telemetry(zones)
      - read_config(zones)
      - read_rampsoak_all(zones)

    and the write helpers for commands.

    Files expected:
      - cn616a_register_map.json (in repo root or provided path)
    """

    def __init__(
        self,
        port: str,
        unit: int = 1,
        *,
        serial: Optional[SerialParams] = None,
        register_map_path: Union[str, Path] = "cn616a_register_map.json",
        retries: int = 2,
        retry_delay: float = 0.15,
        write_quiet_s: float = 0.03,
    ):
        self.port = str(port)
        self.unit = int(unit)
        self.serial = serial or SerialParams()

        self.retries = int(retries)
        self.retry_delay = float(retry_delay)
        self.write_quiet_s = float(write_quiet_s)

        self.map_path = Path(register_map_path)
        self.map: Dict[str, Any] = self._load_map(self.map_path)
        self.enums: Dict[str, Dict[int, str]] = self._build_enums(self.map.get("enums", {}))

        self.client: Optional[ModbusSerialClient] = None
        self.TELEMETRY_PID_FIELDS = [
            ("sp_abs", "f32", None),
            ("out_pct", "f32", None),
            ("control_method", "u16", "control_method"),
            ("control_mode", "u16", "control_mode"),
            ("loop_status", "u16", "loop_status"),
            ("autotune_enable", "u16", "autotune_control"),
            ("autotune_sp", "f32", None),
            ("current_segment_index", "u16", None),
            ("current_segment_state", "u16", "segment_state"),
            ("ramp_soak_remaining", "f32", None),
            ("p_gain", "f32", None),
            ("i_gain", "f32", None),
            ("d_gain", "f32", None),
        ]

    # ----------------------------
    # Connection
    # ----------------------------
    def connect(self) -> None:
        if self.client is not None:
            return

        self.client = ModbusSerialClient(
            method="rtu",
            port=self.port,
            baudrate=self.serial.baudrate,
            parity=self.serial.parity,
            stopbits=self.serial.stopbits,
            bytesize=self.serial.bytesize,
            timeout=self.serial.timeout,
        )

        if not self.client.connect():
            self.client = None
            raise CN616AError(f"Failed to connect on {self.port}")

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            finally:
                self.client = None

    # ----------------------------
    # Map + enums
    # ----------------------------
    @staticmethod
    def _load_map(path: Path) -> Dict[str, Any]:
        """
        Load the register map JSON.

        This repo is typically laid out as:
          repo_root/
            cn616a_register_map.json
            py/
              cn616a.py

        Because scripts are often run from repo_root (or sometimes from py/),
        we try a fairly robust set of candidate locations before failing.

        Search order:
          1) the path as given (expanded)
          2) current working directory / <basename>
          3) this file's directory (py/) / <basename>
          4) repo_root / <basename>   (repo_root inferred from this file being in py/)
          5) repo_root / cn616a_register_map.json (fixed expected name)
          6) first match from a recursive search under repo_root for <basename>
        """
        import os

        basename = Path(path).name
        candidates: list[Path] = []

        # 1) as given
        try:
            candidates.append(Path(os.path.expandvars(os.path.expanduser(str(path)))))
        except Exception:
            candidates.append(Path(path))

        # 2) cwd / basename
        try:
            candidates.append(Path.cwd() / basename)
        except Exception:
            pass

        # 3) py/ (directory containing this file) / basename
        repo_root = None
        try:
            here = Path(__file__).resolve()
            candidates.append(here.parent / basename)
            # 4) repo_root / basename
            repo_root = here.parents[1]  # .../repo_root/py/cn616a.py -> repo_root
            candidates.append(repo_root / basename)
            # 5) repo_root / expected name
            candidates.append(repo_root / "cn616a_register_map.json")
        except Exception:
            pass

        # De-duplicate while preserving order
        dedup: list[Path] = []
        seen = set()
        for p in candidates:
            try:
                key = str(p)
            except Exception:
                continue
            if key in seen:
                continue
            seen.add(key)
            dedup.append(p)

        chosen: Path | None = None
        for p in dedup:
            try:
                # Use both Path and os checks (helps in edge cases on Windows)
                if p.is_file() or os.path.isfile(str(p)):
                    chosen = p
                    break
            except Exception:
                continue

        # 6) recursive search under repo_root if still not found
        if chosen is None and repo_root is not None:
            try:
                for found in repo_root.rglob(basename):
                    if found.is_file():
                        chosen = found
                        break
            except Exception:
                pass

        if chosen is None:
            raise CN616AError(
                "Register map not found. Tried: "
                + ", ".join(str(p) for p in dedup)
            )

        with open(chosen, "r", encoding="utf-8") as f:
            return json.load(f)


    @staticmethod
    def _build_enums(enums_obj: Dict[str, Any]) -> Dict[str, Dict[int, str]]:
        """
        Convert JSON enums {"0":"NAME", ...} to {0:"NAME", ...}
        """
        out: Dict[str, Dict[int, str]] = {}
        for enum_name, mapping in enums_obj.items():
            if not isinstance(mapping, dict):
                continue
            conv: Dict[int, str] = {}
            for k, v in mapping.items():
                try:
                    conv[int(k)] = str(v)
                except Exception:
                    continue
            out[str(enum_name)] = conv
        return out

    def _enum_name(self, enum_key: Optional[str], value: Optional[int]) -> Optional[str]:
        if enum_key is None or value is None:
            return None
        m = self.enums.get(enum_key)
        if not m:
            return None
        return m.get(int(value))

    # ----------------------------
    # Low-level helpers
    # ----------------------------
    def _ensure_connected(self) -> ModbusSerialClient:
        if self.client is None:
            raise CN616AError("Not connected. Call connect() first.")
        return self.client

    def _do(self, fn, *args, **kwargs):
        last_exc = None
        for _ in range(self.retries + 1):
            try:
                return fn(*args, **kwargs)
            except (ModbusException, OSError) as e:
                last_exc = e
                time.sleep(self.retry_delay)
        raise CN616AError(f"Modbus operation failed after retries: {last_exc}") from last_exc

    @staticmethod
    def _regs_to_float(regs: Tuple[int, int]) -> float:
        # Big-endian: first register is MSW, second is LSW
        b = struct.pack(">HH", regs[0] & 0xFFFF, regs[1] & 0xFFFF)
        return struct.unpack(">f", b)[0]

    @staticmethod
    def _float_to_regs(val: float) -> Tuple[int, int]:
        b = struct.pack(">f", float(val))
        msw, lsw = struct.unpack(">HH", b)
        return msw, lsw

    # --- IMPORTANT: addressing ---
    # We pass CN616A's "index" directly as pymodbus address.

    MAX_READ_REGS = 125  # safe Modbus RTU limit for holding registers

    @staticmethod
    def _u16_from_block(block: list[int], start_addr: int, addr: int) -> int:
        i = addr - start_addr
        return int(block[i])

    @staticmethod
    def _f32_from_block(block: list[int], start_addr: int, addr: int) -> float:
        i = addr - start_addr
        msw = block[i]
        lsw = block[i + 1]
        b = struct.pack(">HH", msw & 0xFFFF, lsw & 0xFFFF)
        return struct.unpack(">f", b)[0]

    def read_u16_addr(self, addr: int) -> int:
        c = self._ensure_connected()
        rr = self._do(c.read_holding_registers, address=int(addr), count=1, unit=self.unit)
        if rr.isError():
            raise CN616AError(f"Read u16 error at 0x{addr:04X}: {rr}")
        return int(rr.registers[0])

    def write_u16_addr(self, addr: int, value: int) -> None:
        c = self._ensure_connected()
        time.sleep(self.write_quiet_s)
        rq = self._do(c.write_register, address=int(addr), value=int(value) & 0xFFFF, unit=self.unit)
        if rq.isError():
            raise CN616AError(f"Write u16 error at 0x{addr:04X}: {rq}")

    def read_f32_addr(self, addr: int) -> float:
        c = self._ensure_connected()
        rr = self._do(c.read_holding_registers, address=int(addr), count=2, unit=self.unit)
        if rr.isError():
            raise CN616AError(f"Read float error at 0x{addr:04X}: {rr}")
        return self._regs_to_float((rr.registers[0], rr.registers[1]))

    def write_f32_addr(self, addr: int, value: float) -> None:
        c = self._ensure_connected()
        time.sleep(self.write_quiet_s)
        msw, lsw = self._float_to_regs(value)
        rq = self._do(c.write_registers, address=int(addr), values=[msw, lsw], unit=self.unit)
        if rq.isError():
            raise CN616AError(f"Write float error at 0x{addr:04X}: {rq}")

    def read_block(self, start_addr: int, count: int) -> list[int]:
        if count <= 0:
            return []
        if count > self.MAX_READ_REGS:
            raise CN616AError(
                f"Requested block too large: {count} regs (max {self.MAX_READ_REGS})"
            )

        c = self._ensure_connected()
        rr = self._do(c.read_holding_registers, address=int(start_addr), count=int(count), unit=self.unit)
        if rr.isError():
            raise CN616AError(f"Read block error at 0x{start_addr:04X} count={count}: {rr}")
        return list(rr.registers)

    MAX_READ_REGS = 120  # conservative Modbus RTU limit (typical max is 125 holding regs)

    def read_pv_block(self, zones: Sequence[int]) -> dict[str, float | None]:
        """
        Read PV (float32) for multiple zones efficiently while respecting Modbus max-register limits.

        Some CN616A maps space PV registers far apart (large step between zones). A naive single-block read
        can exceed the Modbus limit (commonly 125 registers) and cause repeated timeouts/retries.
        This function chunks reads into blocks <= MAX_READ_REGS and extracts each zone's PV from the blocks.
        """
        zs = sorted(int(z) for z in zones)
        if not zs:
            return {}

        # Build minimal blocks that cover (addr..addr+1) for each zone, chunked by MAX_READ_REGS
        addrs = [(z, self.pv_addr(z)) for z in zs]

        blocks: list[tuple[int, int, list[tuple[int, int]]]] = []  # (start, count, [(zone, addr), ...])
        cur_items: list[tuple[int, int]] = []
        cur_start: int | None = None
        cur_end: int | None = None  # inclusive

        for z, a in addrs:
            a_end = a + 1  # f32 spans 2 regs
            if cur_start is None:
                cur_start, cur_end = a, a_end
                cur_items = [(z, a)]
                continue

            new_start = cur_start
            new_end = max(cur_end, a_end)  # type: ignore[arg-type]
            new_count = (new_end - new_start) + 1

            if new_count > self.MAX_READ_REGS:
                # flush current block
                blocks.append((cur_start, (cur_end - cur_start) + 1, cur_items))  # type: ignore[operator]
                cur_start, cur_end = a, a_end
                cur_items = [(z, a)]
            else:
                cur_end = new_end
                cur_items.append((z, a))

        if cur_start is not None:
            blocks.append((cur_start, (cur_end - cur_start) + 1, cur_items))  # type: ignore[operator]

        out: dict[str, float | None] = {str(z): None for z in zs}
        for start, count, items in blocks:
            regs = self.read_block(start, count)
            for z, a in items:
                try:
                    out[str(z)] = _safe_float(self._f32_from_block(regs, start, a))
                except Exception:
                    out[str(z)] = None

        return out

    def _pid_block_span(self) -> tuple[int, int]:
        """
        Returns (min_offset_regs, max_offset_regs_inclusive) needed to cover TELEMETRY_PID_FIELDS.
        Offsets are in registers (u16 words).
        """
        offs: list[int] = []
        for name, kind, _enum in self.TELEMETRY_PID_FIELDS:
            f = self._pid_field(name)
            o = _hex_to_int(f["offset_hex"])  # in registers
            offs.append(o)
            if kind == "f32":
                offs.append(o + 1)  # second register
        return min(offs), max(offs)

    def read_pid_telemetry_block(self, zone: int) -> dict:
        """
        Read the PID/control telemetry fields for one zone, but chunk reads so we never
        exceed Modbus max register count (MAX_READ_REGS).
        """
        z = int(zone)
        base = _hex_to_int(self._pid_base_hex(z))

        # Build list of fields with absolute addresses and required span
        items = []  # each: (name, kind, enum_key, addr, addr_end_inclusive)
        for name, kind, enum_key in self.TELEMETRY_PID_FIELDS:
            f = self._pid_field(name)
            o = _hex_to_int(f["offset_hex"])
            addr = base + o
            addr_end = addr + (1 if kind == "f32" else 0)  # f32 spans 2 regs
            items.append((name, kind, enum_key, addr, addr_end))

        # Sort by address and chunk into blocks <= MAX_READ_REGS
        items.sort(key=lambda t: t[3])

        blocks = []  # (start, count, [items...])
        cur_start = None
        cur_end = None
        cur_items = []

        for it in items:
            _, _, _, a, a_end = it
            if cur_start is None:
                cur_start, cur_end = a, a_end
                cur_items = [it]
                continue

            new_start = cur_start
            new_end = max(cur_end, a_end)
            new_count = (new_end - new_start) + 1

            if new_count > self.MAX_READ_REGS:
                blocks.append((cur_start, (cur_end - cur_start) + 1, cur_items))
                cur_start, cur_end = a, a_end
                cur_items = [it]
            else:
                cur_end = new_end
                cur_items.append(it)

        if cur_start is not None:
            blocks.append((cur_start, (cur_end - cur_start) + 1, cur_items))

        # Read each block and parse requested items inside that block
        out = {}

        for start, count, b_items in blocks:
            regs = self.read_block(start, count)
            for name, kind, enum_key, addr, _addr_end in b_items:
                try:
                    if kind == "u16":
                        raw = int(self._u16_from_block(regs, start, addr))
                        out[name] = self._enum_name(enum_key, raw) if enum_key else raw
                    elif kind == "f32":
                        out[name] = _safe_float(self._f32_from_block(regs, start, addr))
                    else:
                        out[name] = None
                except Exception:
                    out[name] = None

        return out

    # Index-hex convenience
    def read_u16(self, index_hex: str) -> int:
        return self.read_u16_addr(_hex_to_int(index_hex))

    def write_u16(self, index_hex: str, value: int) -> None:
        self.write_u16_addr(_hex_to_int(index_hex), value)

    def read_f32(self, index_hex: str) -> float:
        return self.read_f32_addr(_hex_to_int(index_hex))

    def write_f32(self, index_hex: str, value: float) -> None:
        self.write_f32_addr(_hex_to_int(index_hex), value)

    # ----------------------------
    # Address resolution via map
    # ----------------------------
    def _pid_base_hex(self, zone: int) -> str:
        return self.map["registers"]["pid_registers"]["zone_bases_index_hex"][str(int(zone))]

    def _zone_base_hex(self, zone: int) -> str:
        return self.map["registers"]["zone_registers"]["zone_bases_index_hex"][str(int(zone))]

    def _profile_base_hex(self, zone: int) -> str:
        return self.map["registers"]["profile_registers"]["profile_bases_index_hex"][str(int(zone))]

    def _pid_field(self, field_name: str) -> Dict[str, Any]:
        for f in self.map["registers"]["pid_registers"]["fields"]:
            if f["name"] == field_name:
                return f
        raise CN616AError(f"Unknown PID field: {field_name}")

    def _zone_field(self, field_name: str) -> Dict[str, Any]:
        for f in self.map["registers"]["zone_registers"]["fields"]:
            if f["name"] == field_name:
                return f
        raise CN616AError(f"Unknown zone field: {field_name}")

    def pid_addr(self, zone: int, field_name: str) -> int:
        base = _hex_to_int(self._pid_base_hex(zone))
        off = _hex_to_int(self._pid_field(field_name)["offset_hex"])
        return base + off

    def zone_addr(self, zone: int, field_name: str) -> int:
        base = _hex_to_int(self._zone_base_hex(zone))
        off = _hex_to_int(self._zone_field(field_name)["offset_hex"])
        return base + off

    def pv_addr(self, zone: int) -> int:
        pat = self.map["registers"]["temperature_pv"]["pattern"]
        z1 = _hex_to_int(pat["zone_1_index_hex"])
        step = _hex_to_int(pat["index_step_hex"])
        z = int(zone)
        if z < 1:
            raise CN616AError("zone must be >= 1")
        return z1 + (z - 1) * step

    def sensor_status_addr(self, zone: int) -> int:
        ss = self.map["registers"]["sensor_status"]
        z1 = _hex_to_int(ss["zone_1_index_hex"])
        step = _hex_to_int(ss["zone_step_hex"])
        z = int(zone)
        if z < 1:
            raise CN616AError("zone must be >= 1")
        return z1 + (z - 1) * step

    def sensor_alarm_bitmaps_addrs(self) -> Tuple[int, int]:
        ss = self.map["registers"]["sensor_status"]
        bitmaps = ss.get("bitmaps", [])
        # Expect two: sensor_status_bitmap, alarm_status_bitmap
        sensor_b = None
        alarm_b = None
        for b in bitmaps:
            name = b.get("name")
            idx = b.get("index_hex")
            if not idx:
                continue
            if name == "sensor_status_bitmap":
                sensor_b = _hex_to_int(idx)
            elif name == "alarm_status_bitmap":
                alarm_b = _hex_to_int(idx)
        if sensor_b is None or alarm_b is None:
            # Fallback to expected defaults if map omitted them
            sensor_b = _hex_to_int("0x018C")
            alarm_b = _hex_to_int("0x018D")
        return sensor_b, alarm_b

    def rtd_offset_addr(self, zone: int) -> int:
        uc = self.map["registers"]["user_calibration"]
        z1 = _hex_to_int(uc["zone_1_index_hex"])
        step = _hex_to_int(uc["zone_step_hex"])
        z = int(zone)
        if z < 1:
            raise CN616AError("zone must be >= 1")
        return z1 + (z - 1) * step

    def profile_addr(self, zone: int, segment: int, field_offset_hex: str) -> int:
        """
        Profile formula from map:
          addr = base + 6*(segment-1) + field_offset
        where each segment has 3 floats (2 regs each) => 6 registers.
        field_offset: 0x00, 0x02, 0x04
        """
        base = _hex_to_int(self._profile_base_hex(zone))
        seg = int(segment)
        if seg < 1 or seg > 20:
            raise CN616AError("segment must be 1..20")
        field_off = _hex_to_int(field_offset_hex)
        return base + 6 * (seg - 1) + field_off

    # ----------------------------
    # Schema-locked READ APIs
    # ----------------------------
    def read_telemetry(self, zones: Sequence[int]) -> Dict[str, Any]:
        zs = [int(z) for z in zones]
        pv_map = self.read_pv_block(zs)

        # bitmaps in one call (2 regs)
        sensor_b, alarm_b = self.sensor_alarm_bitmaps_addrs()
        start_b = min(sensor_b, alarm_b)
        regs_b = self.read_block(start_b, 2)
        sensor_bitmap = self._u16_from_block(regs_b, start_b, sensor_b)
        alarm_bitmap  = self._u16_from_block(regs_b, start_b, alarm_b)

        # device scale and sensor type (system-wide config read once)
        temperature_scale = None
        sensor_type = None
        sensor_subtype = None
        try:
            for r in self.map["registers"]["system"]:
                if r.get("name") == "temperature_scale":
                    raw = self.read_u16(r["index_hex"])
                    temperature_scale = self._enum_name(r.get("enum"), raw)
                elif r.get("name") == "sensor_type":
                    raw = self.read_u16(r["index_hex"])
                    sensor_type = self._enum_name(r.get("enum"), raw)
                elif r.get("name") == "sensor_subtype":
                    sensor_subtype = self.read_u16(r["index_hex"])
        except Exception:
            pass

        zones_out: Dict[str, Any] = {}
        for z in zs:
            zkey = str(z)
            # pid telemetry via a single block read
            try:
                pid = self.read_pid_telemetry_block(z)
            except Exception:
                pid = {
                    "sp_abs_c": None, "out_pct": None,
                    "control_method": None, "control_mode": None, "loop_status": None,
                    "autotune_enable": None, "autotune_sp_c": None,
                    "segment": {"current_index": None, "current_state": None, "ramp_soak_remaining": None},
                    "p_gain": None, "i_gain": None, "d_gain": None,
                }

            zones_out[zkey] = {
                "pv_c": pv_map.get(zkey),
                "sensor_type": sensor_type,
                "sensor_subtype": sensor_subtype,
                **pid,
            }

        return {
            "device": {"temperature_scale": temperature_scale},
            "bitmaps": {"sensor_status_bitmap_raw": sensor_bitmap, "alarm_status_bitmap_raw": alarm_bitmap},
            "zones": zones_out,
        }

    def read_config(self, zones: Sequence[int]) -> Dict[str, Any]:
        """
        Returns config payload matching your config schema's `data` section:
          {"system": {...}, "zones": {...}}
        """
        # system
        sys_out: Dict[str, Any] = {
            "device_description_raw": None,
            "fw_version": {"major_minor_raw": None, "minor_fix_raw": None},
            "max_zones_raw": None,
            "temperature_scale": None,
            "sensor_type": None,
            "sensor_subtype": None,
            "modbus_address": None,
            "scan_time_seconds": None,
            "active_zone_bitmap_raw": None,
            "system_state": None,
            "startup_state": None,
            "system_alarm_type": None,
            "system_alarm_latch": None,
            "decimal_point": None,
            "password_enable": None,
        }

        # read any system registers present in the map
        for r in self.map["registers"].get("system", []):
            name = r.get("name")
            idx = r.get("index_hex")
            if not name or not idx:
                continue
            try:
                raw = self.read_u16(idx)
            except Exception:
                continue

            # special handling for fw version
            if name == "fw_version_major_minor":
                sys_out["fw_version"]["major_minor_raw"] = raw
                continue
            if name == "fw_version_minor_fix":
                sys_out["fw_version"]["minor_fix_raw"] = raw
                continue

            # map enums to strings where schema expects strings
            enum_key = r.get("enum")
            if enum_key:
                val = self._enum_name(enum_key, raw)
                if val is not None:
                    if name in ("temperature_scale", "system_state", "startup_state", "system_alarm_type",
                                "system_alarm_latch", "decimal_point", "password_enable", "sensor_type"):
                        sys_out[name] = val
                        continue

            # otherwise store raw int into appropriate raw field if present
            if name in sys_out:
                # some schema fields prefer raw ints
                if name.endswith("_raw") or name in ("max_zones_raw", "active_zone_bitmap_raw", "scan_time_seconds", "modbus_address"):
                    sys_out[name] = raw
                else:
                    # leave as None unless explicitly desired
                    pass

            # device description is a u16 in map; keep raw
            if name == "device_description":
                sys_out["device_description_raw"] = raw

            if name == "max_zones":
                sys_out["max_zones_raw"] = raw
            if name == "active_zone_bitmap":
                sys_out["active_zone_bitmap_raw"] = raw
            if name == "modbus_address":
                sys_out["modbus_address"] = raw
            if name == "scan_time_seconds":
                sys_out["scan_time_seconds"] = raw
            if name == "sensor_type":
                # store enum string if available, else raw
                sys_out["sensor_type"] = self._enum_name("sensor_type", raw) or None
            if name == "sensor_subtype":
                # subtype meaning depends on sensor_type; keep raw
                sys_out["sensor_subtype"] = raw

        # per-zone config
        zones_out: Dict[str, Any] = {}
        for z in zones:
            zi = int(z)
            zkey = str(zi)

            # alarms + scaling + calibration + per-zone sensor status
            # alarm setpoints floats
            sp_high = None
            sp_low = None
            try:
                sp_high = self.read_f32_addr(self.zone_addr(zi, "alarm_sp_high"))
            except Exception:
                sp_high = None
            try:
                sp_low = self.read_f32_addr(self.zone_addr(zi, "alarm_sp_low"))
            except Exception:
                sp_low = None

            # alarm1/2 modes/latch/status enums
            def _u16_enum_zone(field: str, enum_name: str) -> Optional[str]:
                try:
                    rawv = self.read_u16_addr(self.zone_addr(zi, field))
                    return self._enum_name(enum_name, rawv)
                except Exception:
                    return None

            alarm1_mode = _u16_enum_zone("alarm1_mode", "alarm_type")
            alarm1_latch = _u16_enum_zone("alarm1_latch", "setting_toggle")
            alarm1_status = _u16_enum_zone("alarm1_status", "alarm_status")

            alarm2_mode = _u16_enum_zone("alarm2_mode", "alarm_type")
            alarm2_latch = _u16_enum_zone("alarm2_latch", "setting_toggle")
            alarm2_status = _u16_enum_zone("alarm2_status", "alarm_status")

            # scaling floats
            def _f32_zone(field: str) -> Optional[float]:
                try:
                    return float(self.read_f32_addr(self.zone_addr(zi, field)))
                except Exception:
                    return None

            cur_hi = _f32_zone("current_scale_high")
            cur_lo = _f32_zone("current_scale_low")
            vol_hi = _f32_zone("voltage_scale_high")
            vol_lo = _f32_zone("voltage_scale_low")

            # calibration
            rtd_off = None
            try:
                rtd_off = self.read_f32_addr(self.rtd_offset_addr(zi))
            except Exception:
                rtd_off = None

            # per-zone sensor status (enum)
            sens_status = None
            try:
                raw = self.read_u16_addr(self.sensor_status_addr(zi))
                sens_status = self._enum_name("sensor_status", raw)
            except Exception:
                sens_status = None

            zones_out[zkey] = {
                "alarms": {
                    "sp_high": _safe_float(sp_high),
                    "sp_low": _safe_float(sp_low),
                    "alarm1": {"mode": alarm1_mode, "latch": alarm1_latch, "status": alarm1_status},
                    "alarm2": {"mode": alarm2_mode, "latch": alarm2_latch, "status": alarm2_status},
                },
                "scaling": {
                    "current_scale_high": cur_hi,
                    "current_scale_low": cur_lo,
                    "voltage_scale_high": vol_hi,
                    "voltage_scale_low": vol_lo,
                },
                "calibration": {"rtd_offset_ohm": _safe_float(rtd_off)},
                "sensor_status": sens_status,
            }

        return {"system": sys_out, "zones": zones_out}

    def read_rampsoak_all(
        self,
        zones: Sequence[int],
        *,
        segments: Sequence[int] = tuple(range(1, 21)),
    ) -> Dict[str, Any]:
        """
        Returns ramp/soak payload matching your rampsoak schema's `data` section:
          {"zones": {"1": {"segments": {"1": {"sp_c":..,"slope_c_per_min":..,"time_h":..}, ...}}}}
        """
        out: Dict[str, Any] = {"zones": {}}
        for z in zones:
            zi = int(z)
            zkey = str(zi)
            segs: Dict[str, Any] = {}

            for s in segments:
                si = int(s)
                sp = None
                slope = None
                th = None
                try:
                    sp = self.read_f32_addr(self.profile_addr(zi, si, "0x00"))
                except Exception:
                    sp = None
                try:
                    slope = self.read_f32_addr(self.profile_addr(zi, si, "0x02"))
                except Exception:
                    slope = None
                try:
                    th = self.read_f32_addr(self.profile_addr(zi, si, "0x04"))
                except Exception:
                    th = None

                segs[str(si)] = {
                    "sp_c": _safe_float(sp),
                    "slope_c_per_min": _safe_float(slope),
                    "time_h": _safe_float(th),
                }

            out["zones"][zkey] = {"segments": segs}

        return out

    # ----------------------------
    # Schema-locked WRITE helpers
    # ----------------------------
    def set_sp_abs(self, zone: int, value_c: float) -> None:
        self.write_f32_addr(self.pid_addr(int(zone), "sp_abs"), float(value_c))

    def set_sp_abs_many(self, zones: Sequence[int], values_c: Union[float, Sequence[float]]) -> None:
        if isinstance(values_c, (int, float)):
            for z in zones:
                self.set_sp_abs(int(z), float(values_c))
        else:
            vals = list(values_c)
            zs = list(zones)
            if len(vals) != len(zs):
                raise CN616AError("zones and values_c length mismatch")
            for z, v in zip(zs, vals):
                self.set_sp_abs(int(z), float(v))

    def set_control_method(self, zone: int, method: Union[str, int]) -> None:
        """
        method may be:
          - int (raw enum)
          - string matching register-map enum (e.g. "PID_CONTROL" or "ON_OFF_CONTROL")
        """
        if isinstance(method, str):
            # reverse lookup
            inv = {v: k for k, v in self.enums.get("control_method", {}).items()}
            if method not in inv:
                raise CN616AError(f"Unknown control_method string: {method}")
            val = inv[method]
        else:
            val = int(method)
        self.write_u16_addr(self.pid_addr(int(zone), "control_method"), val)

    def set_control_mode(self, zone: int, mode: Union[str, int]) -> None:
        if isinstance(mode, str):
            inv = {v: k for k, v in self.enums.get("control_mode", {}).items()}
            if mode not in inv:
                raise CN616AError(f"Unknown control_mode string: {mode}")
            val = inv[mode]
        else:
            val = int(mode)
        self.write_u16_addr(self.pid_addr(int(zone), "control_mode"), val)

    def set_autotune_setpoint(self, zones: Union[int, Sequence[int]], setpoints: Union[float, Sequence[float]]) -> None:
        """
        Backwards compatible:
          - set_autotune_setpoint(2, 100.0)
        Batch:
          - set_autotune_setpoint([2,3], [100.0, 100.0])
        """
        if isinstance(zones, int):
            z_list = [zones]
        else:
            z_list = [int(z) for z in zones]

        if isinstance(setpoints, (int, float)):
            sp_list = [float(setpoints)]
        else:
            sp_list = [float(x) for x in setpoints]

        if len(z_list) != len(sp_list):
            raise CN616AError(f"zones and setpoints length mismatch ({len(z_list)} vs {len(sp_list)})")

        for z, sp in zip(z_list, sp_list):
            self.write_f32_addr(self.pid_addr(int(z), "autotune_sp"), float(sp))

    def start_autotune(self, zones: Union[int, Sequence[int]]) -> None:
        """
        Start autotune by writing autotune_enable = ENABLE (1) in the PID block.
        """
        if isinstance(zones, int):
            z_list = [zones]
        else:
            z_list = [int(z) for z in zones]

        inv = {v: k for k, v in self.enums.get("autotune_control", {}).items()}
        enable_val = inv.get("ENABLE", 1)

        for z in z_list:
            self.write_u16_addr(self.pid_addr(int(z), "autotune_enable"), int(enable_val))

    def stop_autotune(self, zone: int) -> None:
        inv = {v: k for k, v in self.enums.get("autotune_control", {}).items()}
        disable_val = inv.get("DISABLE", 0)
        self.write_u16_addr(self.pid_addr(int(zone), "autotune_enable"), int(disable_val))

    def write_rampsoak_profile(
        self,
        zone: int,
        segments: Dict[int, Dict[str, Number]],
    ) -> None:
        """
        segments example:
          {
            1: {"sp_c": 80.0, "slope_c_per_min": 5.0, "time_h": 0.5},
            2: {"sp_c": 90.0, "slope_c_per_min": 3.0, "time_h": 1.0}
          }
        """
        zi = int(zone)
        for seg_i, vals in segments.items():
            si = int(seg_i)
            if "sp_c" in vals and vals["sp_c"] is not None:
                self.write_f32_addr(self.profile_addr(zi, si, "0x00"), float(vals["sp_c"]))
            if "slope_c_per_min" in vals and vals["slope_c_per_min"] is not None:
                self.write_f32_addr(self.profile_addr(zi, si, "0x02"), float(vals["slope_c_per_min"]))
            if "time_h" in vals and vals["time_h"] is not None:
                self.write_f32_addr(self.profile_addr(zi, si, "0x04"), float(vals["time_h"]))

    # ----------------------------
    # Convenience: legacy-ish describe (optional)
    # ----------------------------
    def describe(self, zone: int = 1) -> str:
        """
        Human-readable single-zone status line derived from telemetry.
        """
        tel = self.read_telemetry([int(zone)])
        z = tel["zones"].get(str(int(zone)), {})
        pv = z.get("pv_c")
        sp = z.get("sp_abs_c")
        out = z.get("out_pct")
        method = z.get("control_method")
        mode = z.get("control_mode")
        return (
            f"CN616A(port={self.port}, unit={self.unit}) | "
            f"z{zone}: PV={pv}C SP_abs={sp}C OUT={out}% | method={method} mode={mode}"
        )


# ----------------------------
# Minimal CLI (optional)
# ----------------------------
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="CN616A backend CLI (schema-locked, map-driven)")
    ap.add_argument("--port", required=True)
    ap.add_argument("--unit", type=int, default=1)
    ap.add_argument("--zone", type=int, default=1)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--parity", choices=["N", "E", "O"], default="N")
    ap.add_argument("--stopbits", type=int, choices=[1, 2], default=1)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--map", default="cn616a_register_map.json")

    ap.add_argument("--status", action="store_true")
    ap.add_argument("--telemetry", action="store_true")
    ap.add_argument("--config", action="store_true")
    ap.add_argument("--rampsoak", action="store_true")

    ap.add_argument("--set-sp", type=float, default=None)
    ap.add_argument("--pid", action="store_true")
    ap.add_argument("--onoff", action="store_true")

    ap.add_argument("--autotune-sp", type=float, default=None)
    ap.add_argument("--start-autotune", action="store_true")
    ap.add_argument("--stop-autotune", action="store_true")

    args = ap.parse_args()

    serial = SerialParams(
        baudrate=args.baud,
        parity=args.parity,
        stopbits=args.stopbits,
        timeout=args.timeout,
    )

    ctl = CN616A(port=args.port, unit=args.unit, serial=serial, register_map_path=args.map)
    ctl.connect()
    try:
        if args.set_sp is not None:
            ctl.set_sp_abs(args.zone, args.set_sp)

        if args.pid:
            ctl.set_control_method(args.zone, "PID_CONTROL")
        if args.onoff:
            ctl.set_control_method(args.zone, "ON_OFF_CONTROL")

        if args.autotune_sp is not None:
            ctl.set_autotune_setpoint(args.zone, args.autotune_sp)

        if args.start_autotune:
            ctl.start_autotune(args.zone)
        if args.stop_autotune:
            ctl.stop_autotune(args.zone)

        if args.status:
            print(ctl.describe(args.zone))

        if args.telemetry:
            print(json.dumps(ctl.read_telemetry([args.zone]), indent=2))

        if args.config:
            print(json.dumps(ctl.read_config([args.zone]), indent=2))

        if args.rampsoak:
            print(json.dumps(ctl.read_rampsoak_all([args.zone]), indent=2))

    finally:
        ctl.close()