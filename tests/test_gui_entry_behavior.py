import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_DIR = REPO_ROOT / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from gui.entry_behavior import select_all_on_focus  # noqa: E402


class FakeEntry:
    def __init__(self, value):
        self.value = value
        self.cursor = len(value)
        self.selection = None
        self.focused = True
        self._idle_callbacks = []

    def after_idle(self, callback):
        self._idle_callbacks.append(callback)

    def run_idle(self):
        callbacks, self._idle_callbacks = self._idle_callbacks, []
        for callback in callbacks:
            callback()

    def winfo_exists(self):
        return True

    def focus_get(self):
        return self if self.focused else None

    def select_range(self, start, end):
        self.selection = (start, len(self.value) if end == "end" else end)

    def icursor(self, position):
        self.cursor = len(self.value) if position == "end" else position

    def type_text(self, text):
        if self.selection is not None:
            start, end = self.selection
            self.value = self.value[:start] + text + self.value[end:]
            self.cursor = start + len(text)
            self.selection = None
        else:
            self.value = self.value[:self.cursor] + text + self.value[self.cursor:]
            self.cursor += len(text)

    def delete_selection(self):
        if self.selection is not None:
            start, end = self.selection
            self.value = self.value[:start] + self.value[end:]
            self.cursor = start
            self.selection = None


class EntryBehaviorTests(unittest.TestCase):
    def test_mouse_and_keyboard_focus_select_existing_value_before_typing(self):
        for focus_kind in ("mouse", "keyboard"):
            with self.subTest(focus_kind=focus_kind):
                entry = FakeEntry("70")
                select_all_on_focus(SimpleNamespace(widget=entry))
                if focus_kind == "mouse":
                    entry.icursor(1)
                entry.run_idle()
                entry.type_text("80")
                self.assertEqual(entry.value, "80")

    def test_caret_movement_after_focus_preserves_insert_position(self):
        entry = FakeEntry("70")
        select_all_on_focus(SimpleNamespace(widget=entry))
        entry.run_idle()
        entry.selection = None
        entry.icursor(1)
        entry.type_text("80")
        self.assertEqual(entry.value, "7800")

    def test_select_all_delete_paste_and_signed_decimal_input(self):
        entry = FakeEntry("70")
        select_all_on_focus(SimpleNamespace(widget=entry))
        entry.run_idle()
        entry.delete_selection()
        self.assertEqual(entry.value, "")

        entry = FakeEntry("70")
        select_all_on_focus(SimpleNamespace(widget=entry))
        entry.run_idle()
        entry.type_text("-0.75")
        self.assertEqual(entry.value, "-0.75")

        entry = FakeEntry("70")
        select_all_on_focus(SimpleNamespace(widget=entry))
        entry.run_idle()
        entry.type_text("80")
        self.assertEqual(entry.value, "80")

    def test_focus_lost_before_idle_does_not_change_selection(self):
        entry = FakeEntry("70")
        select_all_on_focus(SimpleNamespace(widget=entry))
        entry.focused = False
        entry.run_idle()
        self.assertIsNone(entry.selection)


if __name__ == "__main__":
    unittest.main()
