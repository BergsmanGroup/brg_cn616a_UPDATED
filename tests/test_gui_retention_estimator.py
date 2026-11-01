import json
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_DIR = REPO_ROOT / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from gui.retention_estimator import (  # noqa: E402
    MAX_SAMPLE_BYTES,
    MAX_SAMPLE_RECORDS,
    RetentionEstimateWorker,
    estimate_log_storage,
)


class RetentionEstimatorTests(unittest.TestCase):
    def test_small_log_sample_preserves_exact_existing_formula(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs_dir = Path(temporary)
            (logs_dir / "telemetry.jsonl").write_bytes(b'{"sample":1}\n{"sample":2}\n')

            estimate = estimate_log_storage(logs_dir, "telemetry.jsonl", 3600, 2, 100, 10)

        self.assertEqual(estimate["records"], 7200)
        self.assertEqual(estimate["average_record_bytes"], 13)
        self.assertEqual(estimate["estimated_bytes"], 93600)
        self.assertEqual(estimate["file_count"], 936)
        self.assertEqual(estimate["capacity_bytes"], 93600)
        self.assertEqual(estimate["sampled_records"], 2)
        self.assertEqual(estimate["sample_status"], "limited")
        self.assertFalse(estimate["file_count_confident"])

    def test_large_active_and_rotated_logs_have_independent_byte_and_record_bounds(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs_dir = Path(temporary)
            record = json.dumps({"sample": 1, "payload": "x" * 100}) + "\n"
            for name in ("telemetry.jsonl", "telemetry_0001.jsonl"):
                (logs_dir / name).write_text(record * 3000, encoding="utf-8")

            estimate = estimate_log_storage(logs_dir, "telemetry.jsonl", 3600, 2, 1000, 10)

        self.assertEqual(estimate["sampled_files"], 2)
        self.assertLessEqual(estimate["sampled_bytes"], MAX_SAMPLE_BYTES)
        self.assertLessEqual(estimate["sampled_records"], MAX_SAMPLE_RECORDS)
        self.assertEqual(estimate["sample_status"], "sampled")
        self.assertTrue(estimate["file_count_confident"])

    def test_missing_data_keeps_fallback_formula_but_marks_file_count_uncertain(self):
        with tempfile.TemporaryDirectory() as temporary:
            estimate = estimate_log_storage(
                Path(temporary), "missing.jsonl", 60, 2, 100, 17
            )

        self.assertEqual(estimate["average_record_bytes"], 17)
        self.assertEqual(estimate["estimated_bytes"], 2040)
        self.assertEqual(estimate["file_count"], 21)
        self.assertEqual(estimate["sample_status"], "fallback")
        self.assertFalse(estimate["file_count_confident"])

    def test_invalid_inputs_do_not_produce_estimates(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs_dir = Path(temporary)
            for values in (
                (None, 1, 100),
                (1, 0, 100),
                (1, 1, 0),
            ):
                with self.subTest(values=values):
                    self.assertIsNone(
                        estimate_log_storage(logs_dir, "missing.jsonl", *values, 10)
                    )

    def test_worker_reuses_inputs_and_recomputes_after_source_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs_dir = Path(temporary)
            worker = RetentionEstimateWorker(logs_dir, poll_interval_s=0.02)
            specs = {
                "telemetry": ("telemetry.jsonl", 60, 2, 100, 17),
            }
            try:
                revision = worker.request(specs)
                self.assertEqual(worker.request(specs), revision)
                first = self._wait_for_result(worker)
                self.assertEqual(first[0], revision)
                self.assertEqual(first[2]["telemetry"]["sample_status"], "fallback")

                log = logs_dir / "telemetry.jsonl"
                log.write_text('{"sample":1}\n' * 24, encoding="utf-8")
                second = self._wait_for_result(worker)
                self.assertEqual(second[0], revision)
                self.assertEqual(second[2]["telemetry"]["sampled_records"], 24)
                self.assertEqual(second[2]["telemetry"]["sample_status"], "sampled")
            finally:
                worker.shutdown()

    @staticmethod
    def _wait_for_result(worker):
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            results = worker.poll_results()
            if results:
                return results[-1]
            time.sleep(0.01)
        raise AssertionError("retention worker did not publish a result")


if __name__ == "__main__":
    unittest.main()
