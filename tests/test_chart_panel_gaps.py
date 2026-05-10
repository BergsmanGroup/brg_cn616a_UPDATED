import math
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_DIR = REPO_ROOT / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

try:
    from gui.chart_panel import _insert_time_gaps  # noqa: E402
except Exception as exc:  # pragma: no cover - environment-dependent import guard
    _IMPORT_ERROR = exc
    _insert_time_gaps = None
else:
    _IMPORT_ERROR = None


class ChartPanelGapTests(unittest.TestCase):
    def setUp(self):
        if _IMPORT_ERROR is not None:
            self.skipTest(f"chart panel dependencies unavailable: {_IMPORT_ERROR}")

    def test_insert_time_gaps_no_gap(self):
        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        times = [base, base + timedelta(seconds=2), base + timedelta(seconds=4)]
        values = [10.0, 11.0, 12.0]

        out_times, out_values = _insert_time_gaps(times, values)

        self.assertEqual(out_times, times)
        self.assertEqual(out_values, values)

    def test_insert_time_gaps_with_gap(self):
        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        times = [base, base + timedelta(seconds=2), base + timedelta(seconds=22)]
        values = [10.0, 11.0, 12.0]

        out_times, out_values = _insert_time_gaps(times, values)

        self.assertEqual(len(out_times), 4)
        self.assertEqual(len(out_values), 4)
        self.assertEqual(out_times[2], times[2])
        self.assertTrue(math.isnan(out_values[2]))
        self.assertEqual(out_times[3], times[2])
        self.assertEqual(out_values[3], values[2])


if __name__ == "__main__":
    unittest.main()
