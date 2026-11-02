---
description: "Qt step 07: port existing configuration/readbacks and viewer/retention settings without new hardware APIs."
agent: agent
---
# Step 07 — Configuration parity

Run only step 07 using the [shared workflow](../../docs/plans/cn616a-qt-gui-workflow.md).
Require step 06 gates. Lower-cost implementation model suitable with stable
adapter and existing patch semantics; escalate genuine service-compatibility bugs.

## Read narrowly
Read `docs/reports/qt-gui-step-06.md`, `ConfigPanel` form/patch builders in
[display panels](../../py/gui/display_panels.py), [retention worker](../../py/gui/retention_estimator.py)
and focused tests, and relevant service settings/connection handlers.

## Deliver
- `configuration.py`: system/zone/alarm/scaling and available PID readbacks;
  polling/GUI refresh, equilibrium window/MAE, flush, rotation/retention/estimates,
  zone names/monitor list, chart preferences and connection settings.
- Preserve nested/legacy viewer keys and real Qt chart effects. Gains, hardware
  activation and unsupported controls remain read-only/unavailable.
- Explicit Apply; dirty edits and edit revisions survive snapshots/delayed replies.
  Reuse background retention estimator. Monitor list is not hardware enablement.
- Port connect/disconnect/refresh sequences through the adapter only. Stored
  settings acknowledgment does not prove listener rebinding or serial changes.
  Keep actual endpoint separate; never probe an unowned endpoint during tests.
  Offline connection controls exercise simulated outcomes, not real ports.

## Automated gate
Compare patch keys/enums/default interpretation with Tk builders/service schemas.
Test invalid input, retained edits, stale acknowledgments, settings reflection,
viewer changes, background estimates, partial transitions, and failed endpoint
change without silent client switch. Run affected retention/state regressions.

## End gate
Write `docs/reports/qt-gui-step-07.md`. User walkthrough checks form density,
editing/tab order, Apply feedback, preferences, and stored-vs-active distinction.
Serial/TCP reconnection on a real service is USER-ONLY / NOT RUN. Stop before 08.
