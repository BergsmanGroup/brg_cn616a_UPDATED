import sys
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_DIR = REPO_ROOT / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from gui.run_panel import RunPanel  # noqa: E402


class RunPanelTests(unittest.TestCase):
    def test_folder_picker_error_keeps_manual_directory_entry_available(self):
        state = SimpleNamespace(value="C:\\manual\\run", message=None, ok=None)
        panel = SimpleNamespace(
            run_dir_var=SimpleNamespace(set=lambda value: setattr(state, "value", value)),
            _set_status=lambda message, *, ok=True: (
                setattr(state, "message", message),
                setattr(state, "ok", ok),
            ),
        )
        with patch("gui.run_panel.filedialog.askdirectory", side_effect=tk.TclError("dialog unavailable")):
            RunPanel._on_browse(panel)

        self.assertEqual(state.value, "C:\\manual\\run")
        self.assertIn("enter the run directory manually", state.message)
        self.assertFalse(state.ok)


if __name__ == "__main__":
    unittest.main()
