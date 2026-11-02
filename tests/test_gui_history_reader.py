import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_DIR = REPO_ROOT / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from gui.history_reader import IncrementalHistoryReader, _HistoryEngine, _downsample_indices  # noqa: E402


def _record(ts, telemetry=None, analysis=None):
    item = {"ts": ts}
    if telemetry is not None:
        item["telemetry"] = telemetry
    if analysis is not None:
        item["analysis"] = analysis
    return json.dumps(item, separators=(",", ":")) + "\n"


class HistoryReaderTests(unittest.TestCase):
    def test_history_identity_is_exposed_and_mixed_sources_are_flagged(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs_dir = Path(temporary)
            timestamp = "2026-03-01T00:00:00+00:00"
            (logs_dir / "cn616a_telemetry_log.jsonl").write_text(
                _record(
                    timestamp,
                    {"zones": {"1": {"pv_c": 1}}},
                ).replace(
                    '"ts":',
                    '"port":"COM_A","unit":1,"ts":',
                    1,
                ),
                encoding="utf-8",
            )
            (logs_dir / "cn616a_analysis_log.jsonl").write_text(
                json.dumps(
                    {
                        "ts": timestamp,
                        "port": "COM_B",
                        "unit": 1,
                        "analysis": {"zones": {"1": {"avg_abs_error_c": 0.5}}},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            result = self._initial_result(logs_dir)

        self.assertIsNone(result["source_identity"])
        self.assertTrue(result["source_identity_conflict"])

    def test_history_with_unidentified_records_has_no_source_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs_dir = Path(temporary)
            timestamp = "2026-03-01T00:00:00+00:00"
            telemetry = _record(timestamp, {"zones": {"1": {"pv_c": 1}}}).replace(
                '"ts":',
                '"port":"COM_A","unit":1,"ts":',
                1,
            )
            (logs_dir / "cn616a_telemetry_log.jsonl").write_text(telemetry, encoding="utf-8")
            (logs_dir / "cn616a_analysis_log.jsonl").write_text(
                json.dumps(
                    {
                        "ts": timestamp,
                        "analysis": {"zones": {"1": {"avg_abs_error_c": 0.5}}},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            result = self._initial_result(logs_dir)

        self.assertIsNone(result["source_identity"])
        self.assertFalse(result["source_identity_conflict"])
        self.assertTrue(result["source_identity_missing"])

    def test_out_of_order_duplicate_zero_and_alias_values_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs_dir = Path(temporary)
            path = logs_dir / "cn616a_telemetry_log.jsonl"
            base = "2026-03-01T00:00:"
            path.write_text(
                _record(base + "10+00:00", {"zones": {"1": {"pv_c": 0, "sp_abs": 0}}})
                + _record(base + "00+00:00", {"zones": {"z1": {"pv": 2, "setpoint": 3}}})
                + _record(base + "10+00:00", {"zones": {"Z1": {"PV": 4, "SP": 5}}}),
                encoding="utf-8",
            )
            result = self._initial_result(logs_dir)

        zone = result["telemetry"][1]
        self.assertEqual(zone["times"], [
            datetime.fromisoformat(base + "00+00:00"),
            datetime.fromisoformat(base + "10+00:00"),
            datetime.fromisoformat(base + "10+00:00"),
        ])
        self.assertEqual(zone["pv"], [2.0, 0.0, 4.0])
        self.assertEqual(zone["sp"], [3.0, 0.0, 5.0])

    def test_rotated_partial_append_and_incremental_bytes_match_full_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs_dir = Path(temporary)
            base = datetime.fromisoformat("2026-03-01T00:00:00+00:00")
            active = logs_dir / "cn616a_telemetry_log.jsonl"
            rotated = logs_dir / "cn616a_telemetry_log_0001.jsonl"
            rotated.write_text(
                _record(base.isoformat(), {"zones": {"1": {"pv_c": 1, "sp_abs_c": 2}}}),
                encoding="utf-8",
            )
            active.write_text(
                _record(
                    (base.replace(second=1)).isoformat(),
                    {"zones": {"1": {"pv_c": 3, "sp_abs_c": 4}}},
                ),
                encoding="utf-8",
            )
            reader = IncrementalHistoryReader(logs_dir, poll_interval_s=0.02)
            try:
                initial = self._wait_result(reader)
                self.assertEqual(len(initial["telemetry"][1]["times"]), 2)

                partial = _record(
                    (base.replace(second=2)).isoformat(),
                    {"zones": {"1": {"pv_c": 5, "sp_abs_c": 6}}},
                ).rstrip("\n")
                with active.open("ab") as stream:
                    stream.write(partial[:20].encode("utf-8"))
                partial_result = self._wait_result(reader)
                self.assertEqual(len(partial_result["telemetry"][1]["times"]), 2)
                self.assertEqual(partial_result["bytes_read"], 20)

                with active.open("ab") as stream:
                    stream.write((partial[20:] + "\n").encode("utf-8"))
                complete = self._wait_result(reader)
                self.assertEqual(complete["telemetry"][1]["pv"], [1.0, 3.0, 5.0])
                self.assertEqual(
                    complete["bytes_read"],
                    len((partial[20:] + "\n").encode("utf-8")),
                )
                self.assertEqual(
                    complete["telemetry"],
                    _full_read_reference(logs_dir, "telemetry", 1.0),
                )
            finally:
                reader.shutdown()

    def test_truncation_and_reader_restart_reload_from_the_full_read_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs_dir = Path(temporary)
            path = logs_dir / "cn616a_telemetry_log.jsonl"
            path.write_text(
                _record("2026-03-01T00:00:00+00:00", {"zones": {"1": {"pv_c": 1}}})
                + _record("2026-03-01T00:00:01+00:00", {"zones": {"1": {"pv_c": 2}}}),
                encoding="utf-8",
            )
            reader = IncrementalHistoryReader(logs_dir, poll_interval_s=0.02)
            try:
                self._wait_result(reader)
                path.write_text(
                    _record("2026-03-01T00:10:00+00:00", {"zones": {"1": {"pv_c": 9}}}),
                    encoding="utf-8",
                )
                truncated = self._wait_result(reader)
                self.assertEqual(truncated["telemetry"][1]["pv"], [9.0])
                self.assertEqual(truncated["telemetry"], _full_read_reference(logs_dir, "telemetry", 1.0))
            finally:
                reader.shutdown()

            restarted = self._initial_result(logs_dir)
            self.assertEqual(restarted["telemetry"], _full_read_reference(logs_dir, "telemetry", 1.0))

    def test_in_place_rewrite_and_rotation_are_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs_dir = Path(temporary)
            path = logs_dir / "cn616a_telemetry_log.jsonl"
            timestamp = "2026-03-01T00:00:00+00:00"
            first = _record(timestamp, {"zones": {"1": {"pv_c": 1}}})
            second = _record(timestamp, {"zones": {"1": {"pv_c": 2}}})
            self.assertEqual(len(first.encode("utf-8")), len(second.encode("utf-8")))
            path.write_text(first, encoding="utf-8")
            reader = IncrementalHistoryReader(logs_dir, poll_interval_s=0.02)
            try:
                self._wait_result(reader)
                time.sleep(0.02)
                path.write_text(second, encoding="utf-8")
                rewritten = self._wait_result(reader)
                self.assertEqual(rewritten["telemetry"][1]["pv"], [2.0])

                rotated = logs_dir / "cn616a_telemetry_log_0001.jsonl"
                path.replace(rotated)
                path.write_text(
                    _record(
                        "2026-03-01T00:00:01+00:00",
                        {"zones": {"1": {"pv_c": 3}}},
                    ),
                    encoding="utf-8",
                )
                after_rotation = self._wait_result(reader)
                self.assertEqual(after_rotation["telemetry"][1]["pv"], [2.0, 3.0])
            finally:
                reader.shutdown()

    def test_analysis_history_and_longer_window_reload(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs_dir = Path(temporary)
            path = logs_dir / "cn616a_analysis_log.jsonl"
            path.write_text(
                _record(
                    "2026-03-01T00:00:00+00:00",
                    analysis={"zones": {"1": {"avg_abs_error_c": 0}}},
                )
                + _record(
                    "2026-03-01T01:00:00+00:00",
                    analysis={"zones": {"z1": {"avg_abs_error_c": 0.5}}},
                ),
                encoding="utf-8",
            )
            reader = IncrementalHistoryReader(logs_dir, poll_interval_s=0.02)
            try:
                short = self._wait_result(reader, history_hours=0.25)
                self.assertEqual(short["analysis"][1]["mae"], [0.5])
                revision = reader.request(2.0)
                long = self._wait_result(reader, revision=revision)
                self.assertEqual(long["analysis"][1]["mae"], [0.0, 0.5])
                self.assertEqual(long["analysis"], _full_read_reference(logs_dir, "analysis", 2.0))
            finally:
                reader.shutdown()

    def test_downsampling_preserves_extrema_and_time_gap_edges(self):
        base = datetime.fromisoformat("2026-03-01T00:00:00+00:00")
        rows = []
        for index in range(100):
            timestamp = base + (timedelta(seconds=index + (100 if index >= 50 else 0)))
            value = 10.0
            if index == 33:
                value = 100.0
            elif index == 77:
                value = -20.0
            rows.append((timestamp, index, value))

        selected = _downsample_indices(rows, 12)

        self.assertIn(33, selected)
        self.assertIn(77, selected)
        self.assertIn(49, selected)
        self.assertIn(50, selected)
        self.assertLessEqual(len(selected), 12)

    def test_reload_has_a_per_update_byte_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs_dir = Path(temporary)
            path = logs_dir / "cn616a_telemetry_log.jsonl"
            base = datetime.fromisoformat("2026-03-01T00:00:00+00:00")
            records = [
                _record(
                    (base + timedelta(seconds=index)).isoformat(),
                    {"zones": {"1": {"pv_c": index}}},
                ).rstrip("\n")[:-1]
                + ',"padding":"' + ("x" * 250) + '"}\n'
                for index in range(6000)
            ]
            path.write_text("".join(records), encoding="utf-8")
            expected_bytes = path.stat().st_size
            engine = _HistoryEngine(logs_dir)

            result = None
            update_count = 0
            while result is None:
                result, more_work = engine.update(1.0)
                update_count += 1
                self.assertLessEqual(engine.last_work_bytes, engine.max_work_bytes)
                if not more_work:
                    self.assertIsNotNone(result)

        self.assertGreater(update_count, 1)
        self.assertGreater(expected_bytes, engine.max_work_bytes)
        self.assertEqual(result["bytes_read"], expected_bytes)

    @staticmethod
    def _initial_result(logs_dir):
        reader = IncrementalHistoryReader(logs_dir, poll_interval_s=0.02)
        try:
            return HistoryReaderTests._wait_result(reader)
        finally:
            reader.shutdown()

    @staticmethod
    def _wait_result(reader, *, history_hours=1.0, revision=None, timeout=1.0):
        requested_revision = reader.request(history_hours) if revision is None else revision
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for result in reader.poll_results():
                if result.get("revision") == requested_revision:
                    return result
            time.sleep(0.01)
        raise AssertionError("history reader did not publish the requested snapshot")


def _full_read_reference(logs_dir, category, history_hours):
    prefix = f"cn616a_{category}_log"
    paths = sorted(logs_dir.glob(f"{prefix}*.jsonl"))
    parsed = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            timestamp = datetime.fromisoformat(item["ts"])
            parsed.append((timestamp, item))
    latest = max(timestamp for timestamp, _item in parsed)
    cutoff = latest.timestamp() - history_hours * 3600.0
    out = {}
    for zone in range(1, 7):
        rows = []
        for timestamp, item in parsed:
            if timestamp.timestamp() < cutoff:
                continue
            if category == "telemetry":
                zones = item.get("telemetry", {}).get("zones", {})
                value = zones.get(str(zone), {})
                pv = value.get("pv_c", value.get("pv"))
                sp = value.get("sp_abs_c", value.get("sp_abs", value.get("setpoint")))
                at_sp = value.get("autotune_sp_c", value.get("autotune_sp"))
                if pv is not None or sp is not None or at_sp is not None:
                    rows.append((timestamp, pv, sp, at_sp))
            else:
                zones = item.get("analysis", {}).get("zones", {})
                value = zones.get(str(zone), zones.get(f"z{zone}", {}))
                mae = value.get("avg_abs_error_c")
                if mae is not None:
                    rows.append((timestamp, mae))
        rows.sort(key=lambda row: row[0])
        if category == "telemetry":
            out[zone] = {
                "times": [row[0] for row in rows],
                "pv": [row[1] for row in rows],
                "sp": [row[2] for row in rows],
                "sp_autotune": [row[3] for row in rows],
            }
        else:
            out[zone] = {"times": [row[0] for row in rows], "mae": [row[1] for row in rows]}
    return out


if __name__ == "__main__":
    unittest.main()
