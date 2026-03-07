import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_DIR = REPO_ROOT / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from cn616a_service import (  # noqa: E402
    ServiceConfig,
    append_jsonl,
    atomic_write_json,
    hz_to_period_s,
    load_persisted_service_config,
    stable_hash,
)


class ServiceUtilsTests(unittest.TestCase):
    def test_hz_to_period(self):
        self.assertAlmostEqual(hz_to_period_s(2.0), 0.5)
        self.assertGreater(hz_to_period_s(0), 1e8)
        self.assertGreater(hz_to_period_s(-1), 1e8)

    def test_stable_hash_is_order_independent(self):
        a = {"x": 1, "y": {"b": 2, "a": 3}}
        b = {"y": {"a": 3, "b": 2}, "x": 1}
        self.assertEqual(stable_hash(a), stable_hash(b))

    def test_atomic_write_json_and_append_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            json_path = out / "state.json"
            jsonl_path = out / "log.jsonl"

            atomic_write_json(json_path, {"ok": True, "n": 1})
            self.assertTrue(json_path.exists())
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(data["n"], 1)

            append_jsonl(jsonl_path, {"a": 1})
            append_jsonl(jsonl_path, {"b": 2})
            lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0]), {"a": 1})
            self.assertEqual(json.loads(lines[1]), {"b": 2})

    def test_service_config_round_trip_and_viewer_fields(self):
        cfg = ServiceConfig(
            zones_mode="list",
            zones_list=[1, 3, 3, 9],
            viewer_show_sp_abs=False,
            viewer_show_sp_autotune=True,
            viewer_show_mae=False,
        )
        payload = cfg.to_dict()

        # Verify both flat and nested viewer payloads are persisted.
        self.assertIn("viewer_show_sp_abs", payload)
        self.assertIn("viewer", payload)
        self.assertIn("show_mae", payload["viewer"])

        restored = ServiceConfig.from_dict(payload)
        self.assertEqual(restored.effective_zones(), [1, 3])
        self.assertFalse(restored.viewer_show_sp_abs)
        self.assertTrue(restored.viewer_show_sp_autotune)
        self.assertFalse(restored.viewer_show_mae)

    def test_from_dict_accepts_nested_viewer_and_zone_names_list(self):
        raw = {
            "zones_mode": "list",
            "zones_list": [6, 2],
            "zone_names": ["A", "B"],
            "viewer": {
                "history_hours": 4,
                "line_width": 3,
                "pv_color": "black",
                "sp_color": "green",
                "sp_autotune_color": "orange",
                "show_sp_abs": False,
                "show_sp_autotune": False,
                "show_mae": True,
            },
        }
        cfg = ServiceConfig.from_dict(raw)
        self.assertEqual(cfg.effective_zones(), [2, 6])
        self.assertEqual(cfg.zone_names["1"], "A")
        self.assertEqual(cfg.zone_names["2"], "B")
        self.assertFalse(cfg.viewer_show_sp_abs)
        self.assertFalse(cfg.viewer_show_sp_autotune)
        self.assertTrue(cfg.viewer_show_mae)

    def test_load_persisted_service_config(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            state_path = out / "cn616a_service_config_state.json"
            cfg = ServiceConfig(viewer_show_sp_abs=False)
            state_path.write_text(
                json.dumps({"config": cfg.to_dict()}),
                encoding="utf-8",
            )

            loaded = load_persisted_service_config(out)
            self.assertIsNotNone(loaded)
            self.assertFalse(loaded.viewer_show_sp_abs)


if __name__ == "__main__":
    unittest.main()
