"""Shared entry focus behavior for editable setpoint and schedule fields."""

import tkinter as tk


def select_all_on_focus(event=None):
    widget = getattr(event, "widget", None)
    if widget is None:
        return

    def select_if_still_focused():
        try:
            if widget.winfo_exists() and widget.focus_get() is widget:
                widget.select_range(0, tk.END)
                widget.icursor(tk.END)
        except tk.TclError:
            return

    widget.after_idle(select_if_still_focused)
