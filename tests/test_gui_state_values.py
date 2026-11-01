import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_DIR = REPO_ROOT / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from gui.state_values import get_field, get_numeric, get_zone, get_zones  # noqa: E402


class StateValueTests(unittest.TestCase):
    def test_normal_and_supported_alternate_zone_shapes(self):
        nested = {"telemetry": {"zones": {"1": {"pv_c": 0}}}}
        direct = {"zones": {"z1": {"pv": 10}}}
        flat = {"Z1": {"process_value": 20}}

        self.assertEqual(get_zone(get_zones(nested, "telemetry"), 1)["pv_c"], 0)
        self.assertEqual(get_zone(get_zones(direct), 1)["pv"], 10)
        self.assertEqual(get_zone(get_zones(flat), 1)["process_value"], 20)
        self.assertEqual(get_zones({"telemetry": []}, "telemetry"), {})

    def test_zero_missing_and_nonfinite_numeric_fields_are_distinct(self):
        zone = {"sp_abs_c": 0, "sp_abs": 70, "pv": float("inf")}

        self.assertEqual(get_numeric(zone, "sp_abs_c", "sp_abs"), 0.0)
        self.assertIsNone(get_numeric(zone, "pv"))
        self.assertIsNone(get_numeric(zone, "missing"))
        self.assertEqual(get_field(zone, "missing", "sp_abs"), 70)


if __name__ == "__main__":
    unittest.main()
