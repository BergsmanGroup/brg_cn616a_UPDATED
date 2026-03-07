import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_DIR = REPO_ROOT / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

import cn616a_cli  # noqa: E402


class CliTests(unittest.TestCase):
    def _run_main(self, args):
        with patch.object(sys, "argv", ["cn616a_cli.py", *args]):
            cn616a_cli.main()

    def test_parse_zones(self):
        self.assertEqual(cn616a_cli.parse_zones("auto"), "auto")
        self.assertEqual(cn616a_cli.parse_zones("1, 2,3"), [1, 2, 3])

    def test_ping_builds_message(self):
        with patch("cn616a_cli.send_cmd", return_value={"ok": True}) as mock_send, patch("builtins.print") as mock_print:
            self._run_main(["ping"])

        host, port, msg = mock_send.call_args.args
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(port, 8765)
        self.assertEqual(msg["op"], "ping")
        self.assertIn("id", msg)
        mock_print.assert_called_once_with({"ok": True})

    def test_set_service_config_patch_fields(self):
        with patch("cn616a_cli.send_cmd", return_value={"ok": True}) as mock_send, patch("builtins.print"):
            self._run_main(
                [
                    "set_service_config",
                    "--telemetry-hz",
                    "4",
                    "--config-hz",
                    "1",
                    "--zones",
                    "1,3",
                    "--flush-each-line",
                    "true",
                    "--analysis-hz",
                    "0.5",
                    "--equilibrium-window-s",
                    "20",
                    "--equilibrium-threshold-c",
                    "0.2",
                ]
            )

        msg = mock_send.call_args.args[2]
        self.assertEqual(msg["op"], "set_service_config")
        patch_obj = msg["patch"]
        self.assertEqual(patch_obj["telemetry_hz"], 4.0)
        self.assertEqual(patch_obj["config_hz"], 1.0)
        self.assertEqual(patch_obj["zones_mode"], "list")
        self.assertEqual(patch_obj["zones_list"], [1, 3])
        self.assertTrue(patch_obj["flush_each_line"])
        self.assertEqual(patch_obj["analysis_hz"], 0.5)

    def test_autotune_sp_single_zone_single_value(self):
        with patch("cn616a_cli.send_cmd", return_value={"ok": True}) as mock_send, patch("builtins.print"):
            self._run_main(["autotune_sp", "--zones", "2", "--values", "100"])

        msg = mock_send.call_args.args[2]
        self.assertEqual(msg["op"], "set_autotune_setpoint")
        self.assertEqual(msg["zone"], 2)
        self.assertEqual(msg["value_c"], 100.0)

    def test_autotune_sp_rejects_auto_zones(self):
        with patch("cn616a_cli.send_cmd"), patch("builtins.print"):
            with patch.object(sys, "argv", ["cn616a_cli.py", "autotune_sp", "--zones", "auto", "--values", "100"]):
                with self.assertRaises(SystemExit):
                    cn616a_cli.main()


if __name__ == "__main__":
    unittest.main()
