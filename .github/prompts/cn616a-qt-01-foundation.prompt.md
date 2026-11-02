---
description: "Qt step 01: create the production shell and repository-data-only replay launch; stop for user UI review."
agent: agent
---
# Step 01 — foundation and safe offline replay

Run only step 01. Premium reasoning model recommended for this migration boundary.
Read and obey the [shared workflow](../../docs/plans/cn616a-qt-gui-workflow.md).
No hardware access or hardware tests. Do not implement later steps.

## Read narrowly
- [Plan](../../docs/plans/cn616a-qt-gui-refactor.md): sections 2–3 and file surfaces.
- [Prototype](../../design/cn616a-gui/prototype.py), its
  [dependencies](../../design/cn616a-gui/requirements.txt), and
  [Tk startup](../../py/gui/main_gui.py): layout/lifecycle references, not demo code to ship.
- [Baseline description](../../tests/fixtures/cn616a-baseline-v1/README.md),
  [replay helper](../../tests/baseline_reference.py), and only the
  `SyntheticLogWriter`/fake-endpoint portions of
  [baseline runner](../../tests/gui_responsiveness_baseline.py).

## Deliver
1. Optional `requirements-qt.txt` including base dependencies; preserve base/Tk
   dependencies. Add `py/qt_gui` entry point and `QMainWindow` with exactly
   Overview, Configuration, Ramp/Soak, Logs. No unsupported enabled controls.
2. Add immutable controller/zone identity records and a small adapter interface
   for snapshots, outcomes, status and shutdown. Document it in the report so
   lower-cost models can extend it without redesign. One configured target only.
3. Add a reusable offline fixture/session helper, e.g.
   `py/qt_gui/offline.py`, generating service-shaped temporary data from the
   repository baseline. Keep raw fixture unchanged; label mock analysis/outcomes.
   Share schema normalization, not the Tk UI/benchmark execution path.
4. Define working `--offline-replay` launch and deterministic test access to the
   session's paths/status. Default to disconnected/read-only without an explicit
   mode; never probe the production endpoint. Defer real adapter implementation
   to step 02. Show mode/provenance prominently.
5. Create `bat/gui_qt_start.bat`, forwarding arguments without starting a service.
   Help works without service/hardware. Preserve Tk launcher. Add minimal launch
   instructions to README/batch documentation.

## Automated gate
Use unittest and offscreen Qt. Verify four tabs, mode labeling, arguments,
missing-dependency diagnostics, temporary-file cleanup, fixture hash preservation,
and bounded close. Guard against serial construction and normal endpoint calls:
tests must fail if offline startup attempts either. Confirm existing Tk/service
imports remain independent of Qt; no actual service startup.

## End gate
Write `docs/reports/qt-gui-step-01.md` using the shared template. Include exact
offline launch and 5-minute user checks for startup, resizing, four tabs, labels,
and close. Mark all data synthetic and controls not yet migrated. Stop for user
review; do not execute step 02.
