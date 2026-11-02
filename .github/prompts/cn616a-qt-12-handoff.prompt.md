---
description: "Qt step 12: document offline software acceptance and user-only device validation; do not run hardware tests or cut over defaults."
agent: agent
---
# Step 12 — acceptance package and user handoff

Run only step 12; obey the [shared workflow](../../docs/plans/cn616a-qt-gui-workflow.md).
Require step 11 automated gate. Lower-cost documentation model suitable.
Do not implement speculative features, perform hardware tests, or change defaults.

## Read narrowly
Read `docs/reports/qt-gui-step-11.md`, current launch/help/dependency documentation,
and unresolved feedback/acceptance items. Do not re-audit the entire repository.

## Deliver
- Update README/batch docs with exact install, offline launch, explicit service
  mode (for the user only), source/identity selection, diagnostic locations,
  known limitations and unchanged Tk rollback.
- Create `docs/reports/qt-gui-user-acceptance.md` with two distinct checklists:
  **offline UI review** and **USER-ONLY HARDWARE — NOT EXECUTED BY ASSISTANT**.
  Each row needs action, observable expected result, user result/evidence,
  pass/fail/not-run and issue reference. Never pre-fill hardware success.
- Offline rows: four tabs, telemetry readability, stale/unknown, edit feel,
  charts/floats, every migrated control's fake success/failure, schedule/recovery,
  Logs, disruptions and rollback.
- Later hardware rows: operator confirms safe process/target and single service
  ownership; read-only comparison first; then operator-selected setpoint/method,
  config/connection, recording/schedule/recovery, profile read/save/start/stop,
  autotune and fresh readback checks. No invented safe setpoints/limits; user
  determines process-safe actions. Mark unavailable checks explicitly.
- Include stop conditions: wrong/unknown identity, unexpected output/state,
  ambiguous trigger/write, failed recording persistence, or unexplained mismatch.
  Do not prescribe blind retries, indiscriminate stops or an assumed-safe rollback
  of thermal state. Use the operator's process-specific safety procedure.
- Create `docs/reports/qt-gui-release-1-summary.md`: delivered features, omissions,
  automated evidence links, attributed user feedback, risks, rollback and Release
  2 exclusions. Status may be SOFTWARE READY / USER HARDWARE ACCEPTANCE PENDING,
  not full hardware certification. No automatic Tk-default cutover.

## Gate and report
Verify documented flags/paths and Markdown links against current implementation.
No need to rerun ten-minute tests if code is unchanged and evidence is current.
If a real defect is found, record the failed gate and route to its owning step;
do not silently add an untested last-minute code fix.
Write `docs/reports/qt-gui-step-12.md` using the shared template.
Stop with a concise user handoff and explicit pending acceptance items.
