"""
chart_render.py

Pure, subprocess-safe rendering of one zone's chart to PNG bytes.

This module must NOT import tkinter or any interactive matplotlib backend. It is
imported and executed inside a worker process (see chart_render_pool.py) so that a
native crash inside matplotlib's Agg renderer - the exact failure mode recorded in
cn616a_gui_fault.log (access violations / int divide by zero inside
matplotlib.backends.backend_agg, triggered by GC running mid-draw during Tk's
idle_draw callback) - kills only the worker process, never the GUI.

Helper functions below (`_insert_time_gaps`, `_display_timezone`) are intentionally
duplicated from chart_panel.py rather than imported, so this module has zero
dependency on tkinter and can be spawned as a standalone worker.
"""

from __future__ import annotations

import io
import math
import statistics
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.ticker import ScalarFormatter, MaxNLocator
import matplotlib.dates as mdates


def _display_timezone(tz_name: str):
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return datetime.now().astimezone().tzinfo


def _insert_time_gaps(
    times: List[datetime],
    values: List[float],
    *,
    gap_factor: float = 2.5,
    min_gap_seconds: float = 5.0,
) -> Tuple[List[datetime], List[float]]:
    """Insert NaN separators where timestamp gaps exceed expected cadence."""
    if len(times) <= 1 or len(values) <= 1:
        return (times, values)

    pair_count = min(len(times), len(values))
    if pair_count <= 1:
        return (times[:pair_count], values[:pair_count])

    deltas: List[float] = []
    for i in range(1, pair_count):
        try:
            dt_seconds = float((times[i] - times[i - 1]).total_seconds())
        except Exception:
            continue
        if dt_seconds > 0:
            deltas.append(dt_seconds)

    if not deltas:
        return (times[:pair_count], values[:pair_count])

    median_delta = float(statistics.median_low(deltas))
    gap_threshold = max(float(min_gap_seconds), float(gap_factor) * median_delta)

    out_times: List[datetime] = [times[0]]
    out_values: List[float] = [float(values[0])]

    for i in range(1, pair_count):
        curr_time = times[i]
        curr_value = float(values[i])

        try:
            dt_seconds = float((curr_time - times[i - 1]).total_seconds())
        except Exception:
            dt_seconds = 0.0

        if dt_seconds > gap_threshold:
            out_times.append(curr_time)
            out_values.append(float("nan"))

        out_times.append(curr_time)
        out_values.append(curr_value)

    return (out_times, out_values)


def _axes_bbox_top_left(ax, fig_height_px: float) -> Tuple[float, float, float, float]:
    """Convert an axes' pixel bbox (matplotlib: origin bottom-left) to top-left-origin image coords."""
    bbox = ax.get_window_extent()
    x0, x1 = float(bbox.x0), float(bbox.x1)
    y0_img = fig_height_px - float(bbox.y1)
    y1_img = fig_height_px - float(bbox.y0)
    return (x0, y0_img, x1, y1_img)


def render_zone_chart(request: Dict[str, Any]) -> Dict[str, Any]:
    """
    Render one zone's PV/SP/autotune-SP/MAE chart to a PNG image.

    Request keys (all required unless noted):
      zone_name: str
      times, pv, sp, sp_autotune: parallel lists (times are datetimes)
      mae_times, mae_values: parallel lists (may be empty)
      history_hours: float
      view_xlim: Optional[(datetime, datetime)] - explicit zoom window; None = auto full window
      line_width: float
      pv_color, sp_color, sp_autotune_color: str
      show_sp_abs, show_sp_autotune, show_mae: bool
      fig_width_in, fig_height_in, dpi: float/int - figure size
      tz_name: str - display timezone, e.g. "America/Los_Angeles"

    Returns a dict with either:
      {"png": bytes, "fig_size_px": (w, h), "axes_bbox_px": (x0, y0, x1, y1),
       "xlim": (datetime, datetime), "has_data": bool}
    or:
      {"error": str}
    """
    try:
        zone_name = str(request.get("zone_name") or "Zone")
        times = list(request.get("times") or [])
        pvs = list(request.get("pv") or [])
        sps = list(request.get("sp") or [])
        sp_autotunes = list(request.get("sp_autotune") or [])
        mae_times = list(request.get("mae_times") or [])
        mae_values = list(request.get("mae_values") or [])

        history_hours = float(request.get("history_hours") or 1.0)
        view_xlim = request.get("view_xlim")
        line_width = float(request.get("line_width") or 2.5)
        pv_color = request.get("pv_color") or "blue"
        sp_color = request.get("sp_color") or "red"
        sp_autotune_color = request.get("sp_autotune_color") or "purple"
        show_sp_abs = bool(request.get("show_sp_abs", True))
        show_sp_autotune = bool(request.get("show_sp_autotune", True))
        show_mae = bool(request.get("show_mae", True))

        fig_width_in = float(request.get("fig_width_in") or 12.0)
        fig_height_in = float(request.get("fig_height_in") or 5.0)
        dpi = float(request.get("dpi") or 100.0)
        tz_name = str(request.get("tz_name") or "America/Los_Angeles")
        display_tz = _display_timezone(tz_name)

        fig = Figure(figsize=(fig_width_in, fig_height_in), dpi=dpi)
        ax_pv = fig.add_subplot(111)
        ax_sp = ax_pv.twinx()

        times_display = [
            t.astimezone(display_tz) if getattr(t, "tzinfo", None) is not None else t
            for t in times
        ]

        has_data = bool(times_display)

        if not has_data:
            ax_pv.text(0.5, 0.5, "No data", ha="center", va="center",
                       transform=ax_pv.transAxes, fontsize=14)
            ax_pv.set_xticks([])
            ax_pv.set_yticks([])
            ax_sp.set_yticks([])
            canvas = FigureCanvasAgg(fig)
            canvas.draw()
            fig_w_px, fig_h_px = canvas.get_width_height()
            buf = io.BytesIO()
            canvas.print_png(buf)
            return {
                "png": buf.getvalue(),
                "fig_size_px": (fig_w_px, fig_h_px),
                "axes_bbox_px": _axes_bbox_top_left(ax_pv, fig_h_px),
                "xlim": None,
                "has_data": False,
            }

        if view_xlim is not None:
            min_time, max_time = view_xlim
            min_time = min_time.astimezone(display_tz) if getattr(min_time, "tzinfo", None) else min_time
            max_time = max_time.astimezone(display_tz) if getattr(max_time, "tzinfo", None) else max_time
        else:
            max_time = max(times_display)
            min_time = max_time - timedelta(hours=history_hours)
            data_min = min(times_display)
            if data_min > min_time:
                min_time = data_min

        if min_time >= max_time:
            min_time = max_time - timedelta(minutes=1)

        # PV
        pv_times, pv_vals = [], []
        for t, p in zip(times_display, pvs):
            if isinstance(p, (int, float)) and math.isfinite(float(p)):
                pv_times.append(t)
                pv_vals.append(float(p))
        if pv_vals:
            pv_plot_times, pv_plot_vals = _insert_time_gaps(pv_times, pv_vals)
            ax_pv.plot(pv_plot_times, pv_plot_vals, color=pv_color, linewidth=line_width,
                       label="PV", linestyle="-")

        # SP absolute
        sp_times, sp_vals = [], []
        for t, s in zip(times_display, sps):
            if isinstance(s, (int, float)) and math.isfinite(float(s)):
                sp_times.append(t)
                sp_vals.append(float(s))
        if show_sp_abs and sp_vals:
            sp_plot_times, sp_plot_vals = _insert_time_gaps(sp_times, sp_vals)
            ax_pv.plot(sp_plot_times, sp_plot_vals, color=sp_color, linewidth=line_width,
                       label="SP Abs", linestyle="-")

        # SP autotune
        sp_auto_times, sp_auto_vals = [], []
        for t, s in zip(times_display, sp_autotunes):
            if isinstance(s, (int, float)) and math.isfinite(float(s)):
                sp_auto_times.append(t)
                sp_auto_vals.append(float(s))
        if show_sp_autotune and sp_auto_vals:
            sp_auto_plot_times, sp_auto_plot_vals = _insert_time_gaps(sp_auto_times, sp_auto_vals)
            ax_pv.plot(sp_auto_plot_times, sp_auto_plot_vals, color=sp_autotune_color,
                       linewidth=line_width, label="SP Autotune", linestyle="--")

        ax_pv.set_xlabel("Time", fontsize=10)
        ax_pv.set_ylabel("Temperature (\u00b0C)", fontsize=10, fontweight="bold")
        ax_pv.tick_params(axis="y", labelsize=9)
        ax_pv.tick_params(axis="x", labelsize=9)

        y_formatter = ScalarFormatter(useOffset=False)
        y_formatter.set_scientific(False)
        ax_pv.yaxis.set_major_formatter(y_formatter)

        # MAE (secondary axis)
        mae_times_display = [
            t.astimezone(display_tz) if getattr(t, "tzinfo", None) is not None else t
            for t in mae_times
        ]
        mae_pairs = [
            (t, v) for t, v in zip(mae_times_display, mae_values)
            if v is not None and min_time <= t <= max_time
        ]
        mae_plot_vals: List[float] = []
        if show_mae and mae_pairs:
            mae_plot_times = [t for t, _ in mae_pairs]
            mae_plot_vals = [float(v) for _, v in mae_pairs if math.isfinite(float(v))]
            if mae_plot_vals:
                mae_plot_times, mae_plot_vals = _insert_time_gaps(mae_plot_times, mae_plot_vals)
                ax_sp.plot(mae_plot_times, mae_plot_vals, color="darkgreen",
                           linewidth=max(1.5, line_width * 0.8), linestyle="-", label="MAE")

                mae_min = min(mae_plot_vals)
                mae_max = max(mae_plot_vals)
                pad = max(0.05, abs(mae_max) * 0.2) if (mae_max - mae_min) < 1e-9 else max(0.02, (mae_max - mae_min) * 0.15)
                lower = max(0.0, mae_min - pad)
                upper = max(lower + 0.05, mae_max + pad)
                ax_sp.set_ylim(lower, upper)
                ax_sp.set_ylabel("MAE (\u00b0C)", color="darkgreen", fontsize=10, fontweight="bold")
                ax_sp.yaxis.set_label_position("right")
                ax_sp.yaxis.tick_right()
                ax_sp.yaxis.set_major_locator(MaxNLocator(nbins=4, min_n_ticks=3))
                ax_sp.tick_params(axis="y", right=True, labelright=True, labelcolor="darkgreen", labelsize=9)
                ax_sp.tick_params(axis="x", bottom=False, labelbottom=False)
                mae_formatter = ScalarFormatter(useOffset=False)
                mae_formatter.set_scientific(False)
                ax_sp.yaxis.set_major_formatter(mae_formatter)

        if not (show_mae and mae_pairs and mae_plot_vals):
            ax_sp.set_ylabel("")
            ax_sp.set_yticks([])
            ax_sp.tick_params(axis="y", right=True, labelright=False)
            ax_sp.tick_params(axis="x", bottom=False, labelbottom=False)

        ax_pv.set_xlim(min_time, max_time)
        ax_pv.xaxis.set_major_locator(mdates.AutoDateLocator())
        ax_pv.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S", tz=display_tz))
        ax_pv.tick_params(axis="x", labelrotation=45)
        ax_pv.grid(True, alpha=0.3, linestyle="-", linewidth=0.5)

        all_lines = ax_pv.get_lines() + ax_sp.get_lines()
        if all_lines:
            labels = [l.get_label() for l in all_lines]
            legend = ax_pv.legend(all_lines, labels, loc="upper left", fontsize=9)
            legend.set_zorder(1000)
            legend.get_frame().set_alpha(0.9)

        fig.tight_layout()

        canvas = FigureCanvasAgg(fig)
        canvas.draw()
        fig_w_px, fig_h_px = canvas.get_width_height()
        buf = io.BytesIO()
        canvas.print_png(buf)

        return {
            "png": buf.getvalue(),
            "fig_size_px": (fig_w_px, fig_h_px),
            "axes_bbox_px": _axes_bbox_top_left(ax_pv, fig_h_px),
            "xlim": (min_time, max_time),
            "has_data": True,
        }
    except Exception as exc:  # pragma: no cover - defensive: never let a bad request kill the worker
        return {"error": f"{type(exc).__name__}: {exc}"}
