---
description: "Qt step 09: port profile start/stop and autotune with non-idempotent command safeguards; hardware tests remain user-only."
agent: agent
---
# Step 09 — profile execution and autotune

Run only step 09; obey the [shared workflow](../../docs/plans/cn616a-qt-gui-workflow.md).
Require step 08 gates. Premium model recommended for non-idempotent actions.
No hardware actuation/tests, including apparently harmless stop operations.

## Read narrowly
Read `docs/reports/qt-gui-step-08.md`, adapter outcome/readback contract,
Tk profile start/stop and [autotune](../../py/gui/command_panel.py) handlers, and
their exact service/driver argument semantics. Do not reread the full manual
to gate already-supported controls on future features.

## Deliver
- Selected/all-monitored-zone profile start/stop via `set_start_profile`, with
  exact explicit target lists, confirmation, pending lockout and available
  segment/state readback. Do not infer pause/resume or hardware Run/Idle.
- Autotune start/stop using existing service operations; explain actual target
  and possible output change in confirmation, distinguish autotune SP from trigger.
  Current status derives from available data, not a fabricated progress timer.
- Acknowledgment/readback/unknown are distinct. No automatic replay after
  timeout, double click, reconnect or GUI restart. Unknown triggers may remain
  unresolved even after a status read; display that uncertainty.
- Any defect requires minimal fix plus contract regression, not a service v2
  redesign. Unknown hardware semantics go to the user-only checklist without
  blocking other migrated supported actions.

## Automated gate
Fake and patched-handler tests: confirmation cancel sends nothing; confirmation
sends once to correct targets; invalid/foreign target rejected; pending disables
duplicates; partial/rejected/timeout outcomes visible; no automatic retry;
stale readback cannot erase uncertainty; reopen/tab change cannot repeat a trigger.
Keep isolated fake endpoints even if a real service happens to be running.

## End gate
Write `docs/reports/qt-gui-step-09.md`. User reviews every confirmation and unknown
state in replay. List later user-only autotune/profile tests, observations and
stop conditions, without conducting them. Stop before step 10.
