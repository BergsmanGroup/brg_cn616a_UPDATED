---
description: "Qt step 03: port Overview telemetry and detail views with offline usability checks."
agent: agent
---
# Step 03 — Overview telemetry

Run only step 03 using the [shared workflow](../../docs/plans/cn616a-qt-gui-workflow.md).
Require the step 02 report/gates. A lower-cost implementation model is suitable;
reuse the adapter contract rather than redesigning it.

## Read narrowly
Read `docs/reports/qt-gui-step-02.md`, Qt adapter/models, telemetry formatting in
[display panels](../../py/gui/display_panels.py), and Overview layout in the
[prototype](../../design/cn616a-gui/prototype.py). Do not import Tk panels.

## Deliver
- `overview.py` with keyed `QAbstractTableModel` telemetry and focused detail.
  Show PV, SP, output, loop/sensor/alarm status, available PID/autotune/analysis
  and equilibrium readbacks, names, units, timestamps and age. Preserve all
  currently available detail without cramming every field into the table.
- Six-zone readable layout, stable row selection/sorting, explicit source/mode,
  missing/stale/offline text beyond colors. Selection must never trigger writes.
- Dirty-form preservation hooks for later controls; no synthetic editable gains
  or hardware activation actions. Unimplemented chart/control areas are explicit.
- Keep status updating while other tabs are visible, without re-reading files
  synchronously or rebuilding rows on every tick.

## Automated gate
Compare normalized fields with existing state helpers against the same replay
corpus. Cover zero/negative/missing values, aliases, unavailable PID fields,
stale source times, sort/selection stability, and no command on row/tab selection.
Tests assert model roles and actual rendered text, not just row count.

## End gate
Write `docs/reports/qt-gui-step-03.md`. Ask the user to check information density,
names/units, selected-zone detail, resize behavior, and stale/error examples in
offline replay. Actual telemetry comparison is USER-ONLY HARDWARE / NOT RUN.
Stop before step 04.
