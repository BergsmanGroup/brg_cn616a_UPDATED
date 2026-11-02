---
description: "Qt step 11: close offline parity and reliability gaps with measured responsiveness and rollback evidence."
agent: agent
---
# Step 11 — integration and reliability

Run only step 11; obey the [shared workflow](../../docs/plans/cn616a-qt-gui-workflow.md).
Require step 10 gates. Premium model recommended for cross-cutting failures.
Read earlier report summaries only to identify relevant unresolved items.

## Read narrowly
Read `docs/reports/qt-gui-step-10.md`, the main plan's parity/acceptance sections,
and current Qt tests/lifecycle. Inspect implementation ranges only for actual
gaps or failing scenarios. Do not initiate another architecture/planning exercise.

## Deliver and validate
- Close tightly scoped missing Release 1 parity, feedback bugs and lifecycle
  defects. Keep unsupported hardware/new Release 2 functions excluded.
- Reuse repository-derived temporary workloads with owned fake service delays,
  disconnect/restart, malformed snapshots, log growth/rotation, pending commands,
  recording recovery, configuration updates, tab/zone changes and floating charts.
- Measure a ten-minute six-zone exercise: 100 ms heartbeat delay p95 <=50 ms,
  p99 <=200 ms, no stall >500 ms; already-ingested telemetry displayed within
  1 second at configured cadence. Record machine/interpreter, method, results,
  caps and failures; offscreen metrics do not certify interactive feel.
- Verify history/series caps and 5,000 log rows after overflow; no multiplying
  readers/timers per window or unbounded queue/memory growth. Check worker exit
  with bounded waits and no writes replayed after reopen.
- Run combined relevant Qt unittest selection plus affected existing helper/
  service fake tests. Verify offline Tk rollback using replay paths and direct
  Tk entry, never its service-starting launcher. Preserve its default launcher.
- If a threshold or safety test fails, fix and remeasure the failing case. If
  blocked, report exact limitation and do not claim readiness.

## End gate
Write `docs/reports/qt-gui-step-11.md`, with measured evidence and a compact parity
matrix: implemented/offline tested/user UI status/hardware pending. User stress
walkthrough covers interruptions, editing, floats, recording, reopen and rollback.
Hardware tests remain NOT RUN and do not gate software implementation completion.
Stop before step 12.
