import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_DIR = REPO_ROOT / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from cn616a_service import CN616AService, ServiceConfig  # noqa: E402


class FakeCtl:
    def __init__(self, port, unit, register_map_path):
        self.port = port
        self.unit = unit
        self.register_map_path = register_map_path
        self.raise_on_set_sp = False
        self.last_call = None

    def close(self):
        return None

    def set_sp_abs(self, zone, value_c):
        if self.raise_on_set_sp:
            raise ValueError("boom")
        self.last_call = ("set_sp_abs", zone, value_c)

    def set_control_method(self, zone, method):
        self.last_call = ("set_control_method", zone, method)

    def set_control_mode(self, zone, mode):
        self.last_call = ("set_control_mode", zone, mode)

    def set_autotune_setpoint(self, zones, setpoints):
        self.last_call = ("set_autotune_setpoint", zones, setpoints)

    def start_autotune(self, zones):
        self.last_call = ("start_autotune", zones)

    def stop_autotune(self, zone):
        self.last_call = ("stop_autotune", zone)

    def read_config(self, zones):
        return {"zones": {str(z): {"sp_abs_c": 80.0} for z in zones}}

    def read_rampsoak_all(self, zones):
        return {"zones": {str(z): {"segments": []} for z in zones}}


class ServiceCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out_dir = Path(self.tmp.name)
        self.cfg = ServiceConfig()

        patcher = patch("cn616a_service.CN616A", new=FakeCtl)
        self.addCleanup(patcher.stop)
        patcher.start()

        self.svc = CN616AService(
            port="COM_TEST",
            unit=1,
            map_path=str(REPO_ROOT / "cn616a_register_map.json"),
            out_dir=self.out_dir,
            tcp_host="127.0.0.1",
            tcp_port=8765,
            cfg=self.cfg,
            verbose=False,
        )

    def test_ping_and_unknown_op(self):
        self.assertEqual(
            self.svc.handle_command({"id": "a", "op": "ping"}),
            {"id": "a", "ok": True, "pong": True},
        )

        resp = self.svc.handle_command({"id": "a", "op": "nope"})
        self.assertFalse(resp["ok"])
        self.assertIn("Unknown op", resp["error"])

    def test_set_service_config_updates_zones_and_viewer(self):
        cmd = {
            "id": "cfg1",
            "op": "set_service_config",
            "patch": {
                "zones_mode": "list",
                "zones_list": [1, 3, 9],
                "viewer_show_sp_abs": False,
                "viewer_show_mae": False,
            },
        }
        resp = self.svc.handle_command(cmd)

        self.assertTrue(resp["ok"])
        self.assertEqual(resp["zones_enabled"], [1, 3])
        self.assertFalse(self.svc.cfg.viewer_show_sp_abs)
        self.assertFalse(self.svc.cfg.viewer_show_mae)

    def test_set_sp_abs_dispatches_to_controller(self):
        resp = self.svc.handle_command({"id": "x", "op": "set_sp_abs", "zone": 2, "value_c": 99.5})
        self.assertTrue(resp["ok"])
        self.assertEqual(self.svc.ctl.last_call, ("set_sp_abs", 2, 99.5))

    def test_get_status_contains_service_config(self):
        resp = self.svc.handle_command({"id": "x", "op": "get_status"})
        self.assertTrue(resp["ok"])
        self.assertIn("service_config", resp)
        self.assertIn("zones_enabled", resp)

    def test_controller_error_is_returned(self):
        self.svc.ctl.raise_on_set_sp = True
        resp = self.svc.handle_command({"id": "x", "op": "set_sp_abs", "zone": 1, "value_c": 100})
        self.assertFalse(resp["ok"])
        self.assertIn("ValueError", resp["error"])


if __name__ == "__main__":
    unittest.main()
