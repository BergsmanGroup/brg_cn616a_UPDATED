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
    from gui.chart_panel import (  # noqa: E402
        _extract_analysis_zone_values,
        _extract_zone_values,
        _insert_time_gaps,
    )
except Exception as exc:  # pragma: no cover - environment-dependent import guard
    _IMPORT_ERROR = exc
    _extract_analysis_zone_values = None
    _extract_zone_values = None
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

    def test_chart_extractors_accept_supported_aliases_and_preserve_zero(self):
        timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        telemetry = {
            zone: {"times": [], "pv": [], "sp": [], "sp_autotune": []}
            for zone in range(1, 7)
        }
        added = _extract_zone_values(
            {"telemetry": {"zones": {"z1": {"pv": 0, "setpoint": 0, "sp_autotune": 0}}}},
            timestamp,
            telemetry,
        )
        self.assertEqual(added, 1)
        self.assertEqual(telemetry[1]["pv"], [0.0])
        self.assertEqual(telemetry[1]["sp"], [0.0])
        self.assertEqual(telemetry[1]["sp_autotune"], [0.0])

        analysis = {zone: {"times": [], "mae": []} for zone in range(1, 7)}
        added = _extract_analysis_zone_values(
            {"analysis": {"Z1": {"mae": 0}}},
            timestamp,
            analysis,
        )
        self.assertEqual(added, 1)
        self.assertEqual(analysis[1]["mae"], [0.0])

    def test_chart_extractors_ignore_nonfinite_numeric_values(self):
        timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        telemetry = {
            zone: {"times": [], "pv": [], "sp": [], "sp_autotune": []}
            for zone in range(1, 7)
        }
        added = _extract_zone_values(
            {"data": {"zones": {"1": {"pv_c": float("nan")}}}},
            timestamp,
            telemetry,
        )
        self.assertEqual(added, 0)


if __name__ == "__main__":
    unittest.main()
