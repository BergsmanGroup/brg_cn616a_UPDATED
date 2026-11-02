---
description: "Qt step 04: add shared embedded/floating pyqtgraph charts from bounded offline histories."
agent: agent
---
# Step 04 — charts and floating windows

Run only step 04; obey the [shared workflow](../../docs/plans/cn616a-qt-gui-workflow.md).
Require step 03 gates. Standard model is sufficient unless measured threading
or performance defects require escalation.

## Read narrowly
Read `docs/reports/qt-gui-step-03.md`, current Qt adapter/Overview,
[history reader](../../py/gui/history_reader.py) and its tests, and Tk chart
preferences/gap/interaction behavior in [chart panel](../../py/gui/chart_panel.py).

## Deliver
- `charts.py` embedded pyqtgraph chart plus optional independent floating windows
  in the same GUI process. One shared history cache/reader, no per-window sockets.
- Selected-zone PV, absolute/autotune SP, MAE and metrics where supplied; correct
  time axis/units, distinct series, time window, zoom/pan/Home and clear-view.
  Clear never deletes service histories; preserve gaps and source timestamps.
- Apply existing viewer duration/line width/color/visibility keys and legacy
  aliases. No new export/docking framework.
- Parse/transform large histories off-thread; batch and coalesce UI updates.
  Preserve reader caps: 100,000 raw / 20,000 rendered points per zone; no more
  than 20,000 plotted points per series. Bound floating subscriptions on close.

## Automated gate
Use temporary replay histories including appends, gaps, rotation/truncation and
malformed lines. Assert series values/timestamps, gap breaks, time-window changes,
caps after overflow, clear semantics, shared-reader count, and close/reopen without
orphan timers. Check meaningful UI heartbeat during updates; leave the full
ten-minute metric run to step 11.

## End gate
Write `docs/reports/qt-gui-step-04.md`. Offline user checks: select zones, change
window, zoom/pan/reset, toggle lines, float/close/reopen, and inspect gaps.
Do not equate a working replay chart with a hardware poll-rate claim.
Stop before step 05.
