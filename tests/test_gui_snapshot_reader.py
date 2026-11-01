import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_DIR = REPO_ROOT / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from gui.snapshot_reader import StateSnapshotReader  # noqa: E402


class SnapshotReaderTests(unittest.TestCase):
    def test_missing_and_partially_written_snapshots_recover_without_old_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs_dir = Path(temporary)
            reader = StateSnapshotReader(
                logs_dir,
                state_files={"telemetry": "state.json"},
                poll_interval_s=0.02,
                retry_interval_s=0.02,
            )
            path = logs_dir / "state.json"
            try:
                missing = self._wait_for(
                    reader,
                    lambda result: result.error is not None and "not found" in result.error,
                )
                self.assertIn("not found", missing.error)

                path.write_text('{"zones":', encoding="utf-8")
                partial = self._wait_for(
                    reader,
                    lambda result: result.error is not None and "JSONDecodeError" in result.error,
                )
                self.assertIsNone(partial.data)

                path.write_text('{"ts":"2020-01-01T00:00:00+00:00","value":0}', encoding="utf-8")
                recovered = self._wait_for(reader, lambda result: result.data is not None)
                self.assertIsNone(recovered.error)
                self.assertTrue(recovered.stale)
                self.assertEqual(recovered.data["value"], 0)
                recovered.data["value"] = 99
                self.assertEqual(reader.snapshot("telemetry").data["value"], 0)
            finally:
                reader.shutdown()

    def test_atomic_replacement_is_detected_and_age_uses_source_timestamp(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs_dir = Path(temporary)
            path = logs_dir / "state.json"
            initial_timestamp = datetime.now(timezone.utc).isoformat()
            path.write_text(json.dumps({"ts": initial_timestamp, "value": 1}), encoding="utf-8")
            reader = StateSnapshotReader(
                logs_dir,
                state_files={"telemetry": "state.json"},
                poll_interval_s=0.02,
            )
            try:
                initial = self._wait_for(reader, lambda result: result.data is not None)
                self.assertFalse(initial.stale)
                replacement = logs_dir / "replacement.json"
                replacement.write_text(json.dumps({"ts": initial_timestamp, "value": 2}), encoding="utf-8")
                replacement.replace(path)
                changed = self._wait_for(
                    reader,
                    lambda result: result.data is not None and result.data.get("value") == 2,
                )
                self.assertEqual(changed.data["value"], 2)
            finally:
                reader.shutdown()

    def test_oversized_snapshot_is_an_explicit_read_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs_dir = Path(temporary)
            (logs_dir / "state.json").write_text(json.dumps({"value": "x" * 100}), encoding="utf-8")
            reader = StateSnapshotReader(
                logs_dir,
                state_files={"telemetry": "state.json"},
                poll_interval_s=0.02,
                max_file_bytes=32,
            )
            try:
                result = self._wait_for(
                    reader,
                    lambda snapshot: snapshot.error is not None and "exceeds" in snapshot.error,
                )
                self.assertIn("exceeds", result.error)
                self.assertIsNone(result.data)
            finally:
                reader.shutdown()

    def test_unchanged_snapshot_is_not_reread_and_replacement_is_read_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs_dir = Path(temporary)
            path = logs_dir / "state.json"
            path.write_text('{"value":1}', encoding="utf-8")
            reader = StateSnapshotReader(
                logs_dir,
                state_files={"telemetry": "state.json"},
                poll_interval_s=0.02,
            )
            try:
                self._wait_for(reader, lambda result: result.data is not None)
                with patch.object(reader, "_read_snapshot", wraps=reader._read_snapshot) as read_snapshot:
                    time.sleep(0.08)
                    self.assertEqual(read_snapshot.call_count, 0)
                    path.write_text('{"value":2}', encoding="utf-8")
                    updated = self._wait_for(reader, lambda result: result.data and result.data["value"] == 2)
                    self.assertEqual(updated.data["value"], 2)
                    self.assertEqual(read_snapshot.call_count, 1)
            finally:
                reader.shutdown()

    @staticmethod
    def _wait_for(reader, predicate, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = reader.snapshot("telemetry")
            if predicate(result):
                return result
            time.sleep(0.01)
        raise AssertionError("snapshot reader did not reach the expected state")


if __name__ == "__main__":
    unittest.main()
