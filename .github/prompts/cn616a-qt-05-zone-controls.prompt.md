---
description: "Qt step 05: migrate setpoint/method/autotune-setpoint edits with exact v1 commands and ambiguity safeguards."
agent: agent
---
# Step 05 — zone update controls

Run only step 05; obey the [shared workflow](../../docs/plans/cn616a-qt-gui-workflow.md).
Require step 04 gates. Standard model with the stable adapter; premium if its
command semantics must change. No physical writes or hardware tests.

## Read narrowly
Read `docs/reports/qt-gui-step-04.md`, the adapter's outcome contract,
[command panel](../../py/gui/command_panel.py), related entry/client tests, and
the exact zone-update handlers in [service](../../py/cn616a_service.py).

## Deliver
- Overview fields for absolute SP, control method, autotune SP and explicit Apply.
  Preserve ordered `set_sp_abs`, `set_control_method`, `set_autotune_setpoint`.
  Correctly label method vs profile control mode; bind to the actual adapter
  target, not a misleading editable unit selector.
- Finite values, existing constraints/units/enums, select-all entry behavior,
  dirty edits surviving refresh/tab changes, explicit discard/retain on zone
  changes. No accidental apply to another target.
- Disable conflicting actions while pending. Per-operation acknowledgment,
  rejected/not-sent/unknown feedback; partial sequence is not atomic.
  Reconcile fresh readback without turning an ambiguous result into success.
- Offline controls use the fake backend only. Provide reproducible success,
  rejection, partial and unknown scenarios, clearly marked simulated.
  Do not add autotune triggers until step 09.

## Automated gate
Assert actual Qt actions yield exact legacy payloads/order and one request per
intent. Test typing/replacement/decimal/negative/paste, NaN/infinity, zero,
dirty refresh, zone changes, stale identity, queue-full, delayed reply, partial
failure and unknown with no automatic retry. Validate payloads against patched
service handlers, not only fake acceptance.

## End gate
Write `docs/reports/qt-gui-step-05.md`. User reviews offline editing feel,
target visibility and every outcome state. List later user-only SP/method/readback
hardware checks without supplying a supposedly safe process temperature.
Stop before step 06.
