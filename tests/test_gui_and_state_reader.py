import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_DIR = REPO_ROOT / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from gui import main_gui  # noqa: E402
from gui.display_panels import _normalize_zone_names  # noqa: E402
from gui.state_reader import (  # noqa: E402
    format_timestamp,
    get_telemetry_state,
    safe_get,
    safe_read_json,
)


class GuiAndStateReaderTests(unittest.TestCase):
    def test_safe_read_json_missing_and_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            missing = d / "missing.json"
            self.assertIsNone(safe_read_json(missing))

            bad = d / "bad.json"
            bad.write_text("{bad", encoding="utf-8")
            self.assertIsNone(safe_read_json(bad))

    def test_get_telemetry_state_default_shape(self):
        with tempfile.TemporaryDirectory() as td:
            state = get_telemetry_state(Path(td))
        self.assertEqual(state["telemetry"], {})
        self.assertEqual(state["zones"], [])
        self.assertIn("error", state)

    def test_safe_get(self):
        data = {"a": {"b": {"c": 3}}}
        self.assertEqual(safe_get(data, "a", "b", "c", default=None), 3)
        self.assertEqual(safe_get(data, "a", "x", default=99), 99)
        self.assertEqual(safe_get(data, "a", 1, default="bad"), "bad")

    def test_format_timestamp(self):
        self.assertEqual(format_timestamp(None), "N/A")
        self.assertEqual(format_timestamp("2026-03-06T12:00:00"), "2026-03-06T12:00:00")

    def test_normalize_zone_names(self):
        names = _normalize_zone_names({"1": "Alpha", 2: "Beta", "3": ""})
        self.assertEqual(names["1"], "Alpha")
        self.assertEqual(names["2"], "Beta")
        self.assertEqual(names["3"], "Zone 3")

        from_list = _normalize_zone_names(["A", None])
        self.assertEqual(from_list["1"], "A")
        self.assertEqual(from_list["2"], "Zone 2")

    def test_chart_disabled_reason_python_313_default(self):
        gui_obj = main_gui.CN616AGUI.__new__(main_gui.CN616AGUI)
        gui_obj.allow_unsafe_chart = False

        with patch.object(main_gui.sys, "version_info", (3, 13, 0)), patch.dict(main_gui.os.environ, {"CN616A_GUI_ALLOW_UNSAFE_CHART": ""}):
            reason = gui_obj._chart_disabled_reason()
        self.assertIsInstance(reason, str)
        self.assertIn("Chart tab disabled", reason)

    def test_chart_enabled_when_forced(self):
        gui_obj = main_gui.CN616AGUI.__new__(main_gui.CN616AGUI)

        gui_obj.allow_unsafe_chart = True
        with patch.object(main_gui.sys, "version_info", (3, 13, 0)), patch.dict(main_gui.os.environ, {}, clear=False):
            self.assertIsNone(gui_obj._chart_disabled_reason())

        gui_obj.allow_unsafe_chart = False
        with patch.object(main_gui.sys, "version_info", (3, 13, 0)), patch.dict(main_gui.os.environ, {"CN616A_GUI_ALLOW_UNSAFE_CHART": "1"}):
            self.assertIsNone(gui_obj._chart_disabled_reason())


if __name__ == "__main__":
    unittest.main()
