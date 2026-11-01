import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_DIR = REPO_ROOT / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from gui.main_gui import CN616AGUI  # noqa: E402


class FakePanel:
    def __init__(self):
        self.running = False
        self.auto_refresh_interval = None

    def start_auto_refresh(self):
        self.running = True

    def stop_auto_refresh(self):
        self.running = False


class GuiTabRefreshTests(unittest.TestCase):
    def _gui(self):
        gui = CN616AGUI.__new__(CN616AGUI)
        gui.refresh_interval = 2.0
        gui.chart_available = False
        gui.chart_panel_initialized = False
        gui.chart_panel = None
        gui.command_panel = FakePanel()
        gui.telemetry_panel = FakePanel()
        gui.run_panel = FakePanel()
        gui.config_panel = FakePanel()
        gui.rampsoak_panel = FakePanel()
        gui.panels = [
            gui.command_panel,
            gui.telemetry_panel,
            gui.run_panel,
            gui.config_panel,
            gui.rampsoak_panel,
        ]
        return gui

    def test_only_selected_tab_refreshes_except_background_run_status(self):
        gui = self._gui()
        gui._set_active_tab("Configuration")

        self.assertTrue(gui.config_panel.running)
        self.assertTrue(gui.run_panel.running)
        self.assertFalse(gui.command_panel.running)
        self.assertFalse(gui.telemetry_panel.running)
        self.assertFalse(gui.rampsoak_panel.running)

        gui._set_active_tab("Ramp/Soak")
        self.assertFalse(gui.config_panel.running)
        self.assertTrue(gui.rampsoak_panel.running)
        self.assertTrue(gui.run_panel.running)

    def test_refresh_rate_restart_does_not_start_hidden_tabs(self):
        gui = self._gui()
        gui._set_active_tab("Telemetry")
        gui._set_active_tab("Run", restart=True)

        self.assertFalse(gui.command_panel.running)
        self.assertFalse(gui.telemetry_panel.running)
        self.assertFalse(gui.config_panel.running)
        self.assertFalse(gui.rampsoak_panel.running)
        self.assertTrue(gui.run_panel.running)


if __name__ == "__main__":
    unittest.main()
