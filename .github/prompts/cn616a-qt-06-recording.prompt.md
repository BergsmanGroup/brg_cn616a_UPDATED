---
description: "Qt step 06: port recording, scheduled stops and recovery using isolated fake-service contract tests."
agent: agent
---
# Step 06 — recording and scheduling

Run only step 06; obey the [shared workflow](../../docs/plans/cn616a-qt-gui-workflow.md).
Require step 05 gates. Standard model. Do not start the production service.

## Read narrowly
Read `docs/reports/qt-gui-step-05.md`, [recording panel](../../py/gui/run_panel.py),
[recording tests](../../tests/test_gui_run_panel.py), and recording/status/recovery
handlers in [service](../../py/cn616a_service.py) and their fake-controller tests.

## Deliver
- Overview Recording card: directory picker/manual path, name, start/stop,
  active/elapsed/last-data state and recovered interruption. Recording is not
  controller Run/Idle or profile execution.
- Pacific-time scheduled stop with existing conversion/validation semantics and
  exact `set_run_schedule` payload. Service remains authoritative while hidden.
- `start_run`, `stop_run`, `get_run_status` retain existing formats and outcomes;
  no optimistic active status, duplicate start after reconnect, or stop on GUI exit.
- Temporary offline recordings must use only a fake-controller service handler
  or replay backend. Label simulated source; verify real persistence semantics
  through patched service tests, not an animation that only changes a button.

## Automated gate
Start/stop/schedule in temporary directories, verify JSONL and run metadata shapes,
invalid/past schedules and existing timezone behavior, rejected paths, delayed/
unknown outcomes, status coalescing, recovered interruption, GUI restart while
capture continues, and tab-hidden updates. No writes to repository logs/runs.

## End gate
Write `docs/reports/qt-gui-step-06.md`. User checks directory dialog/manual entry,
recording vs heating wording, schedule entry and fake recovery in replay.
Document steps 01–06 as a partial milestone, not full parity. Stop before step 07.
