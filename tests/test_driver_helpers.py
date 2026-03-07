import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_DIR = REPO_ROOT / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from cn616a import CN616A, _hex_to_int, _safe_float  # noqa: E402


class DriverHelperTests(unittest.TestCase):
    def test_hex_to_int(self):
        self.assertEqual(_hex_to_int("0x10"), 16)
        self.assertEqual(_hex_to_int("FF"), 255)

    def test_safe_float(self):
        self.assertEqual(_safe_float("1.25"), 1.25)
        self.assertIsNone(_safe_float(None))
        self.assertIsNone(_safe_float("abc"))

    def test_float_register_round_trip(self):
        value = 123.456
        regs = CN616A._float_to_regs(value)
        out = CN616A._regs_to_float(regs)
        self.assertAlmostEqual(out, value, places=3)

    def test_build_enums(self):
        source = {
            "mode": {"0": "AUTO", "1": "MANUAL", "bad": "SKIP"},
            "not_a_map": ["x"],
        }
        enums = CN616A._build_enums(source)
        self.assertEqual(enums["mode"][0], "AUTO")
        self.assertEqual(enums["mode"][1], "MANUAL")
        self.assertNotIn("not_a_map", enums)

    def test_u16_and_f32_from_block(self):
        self.assertEqual(CN616A._u16_from_block([10, 11, 12], start_addr=100, addr=101), 11)

        regs = list(CN616A._float_to_regs(42.5))
        block = [0, 0] + regs + [0]
        out = CN616A._f32_from_block(block, start_addr=200, addr=202)
        self.assertAlmostEqual(out, 42.5, places=4)


if __name__ == "__main__":
    unittest.main()
