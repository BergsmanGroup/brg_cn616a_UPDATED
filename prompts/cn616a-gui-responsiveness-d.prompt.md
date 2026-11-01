---
name: cn616a-gui-responsiveness-d
description: "Migrate the CN616A control GUI incrementally to PySide6/Qt while preserving verified workflows."
argument-hint: "Add constraints or identify verified A-C prerequisite evidence"
agent: agent
---

# CN616A GUI responsiveness: D — migrate the control GUI to Qt

Only implement this stage when explicitly in scope and after evaluating A-C.
This is step six: Baseline, A1, A2, B, C, D, E. Read
[README.md](../README.md), inspect the current GUI/service and tests, and
verify completed Baseline, A1, A2, B, and C gates in the repository. Report
missing prerequisites instead of silently implementing them. Do not assume
changing frameworks fixes blocking work. Preserve the service as the sole
controller owner.

## Implementation

- Inventory and preserve the Telemetry/zone commands, Run, Configuration,
  Ramp/Soak, status, startup/shutdown, error, and chart-launch workflows.
  Reuse the service client, snapshot/history abstractions, and separate chart
  where suitable. Keep all Qt widget operations on its GUI thread and move
  blocking work to bounded workers or separate processes.
- Migrate incrementally behind a separate launcher or explicit opt-in so the
  previous GUI remains a usable rollback until parity is demonstrated. Do not
  run Tk and Qt event loops together in the same process as a shortcut.
- Validate keyboard/mouse input, accessibility and focus, dropdown stability,
  editing safeguards, error/stale-state display, pending/unknown command
  states, and Windows packaging. Review dependency and distribution licensing.

## Safety and compatibility

- Never connect to a real controller or send real control commands, change
  setpoints, start autotune, or change a run schedule without separate,
  explicit operator authorization. Use fake services/controllers and
  temporary data.
- Keep controller I/O serialized by the service; do not duplicate the serial
  driver or interrupt a transaction. Preserve command timeout/partial/unknown
  outcome semantics, existing CLI/protocol behavior, units, identities,
  timestamps, stale indication, and configuration/log formats.
- Surface errors explicitly. Bound workers, queues, memory, and repeated work.
  Keep Python 3.12/Windows support. If packages change, update dependencies,
  installation instructions, and launchers together.
- Preserve user changes. Do not perform destructive git operations, commit,
  or modify unrelated files.

## D gate and report

Run focused automated tests plus a feature-by-feature parity checklist and the
same latency/soak workloads used for A-C. Compare results with the existing
GUI and document gaps, migration steps, and rollback. Remove the old GUI only
after explicit approval and verified parity.

Use existing test patterns and the configured runner; update directly
affected docs and launchers. Report changes, feature comparison, workload and
measurements with units, tests, unverified Windows/hardware checks, gaps, and
residual risks. Distinguish simulated evidence from operator validation. Stop
at this gate; do not add a browser interface.
