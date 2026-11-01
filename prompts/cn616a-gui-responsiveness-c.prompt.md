---
name: cn616a-gui-responsiveness-c
description: "Evaluate and build an isolated, independently launchable CN616A chart application."
argument-hint: "Add constraints or identify verified chart/data prerequisite evidence"
agent: agent
---

# CN616A GUI responsiveness: C — separate chart application

Only implement this stage when explicitly in scope. This is the fifth step in
the sequence: Baseline, A1, A2, B, C, D, E. Read [README.md](../README.md),
inspect the current chart and service, and verify completed Baseline, A1, A2,
and B gates in the repository. If prerequisites are missing, report the gap;
do not silently implement them. Inventory current chart features before
editing. The viewer must read service data and must not own the serial port.

## Implementation

- Evaluate PySide6 plus pyqtgraph for the supported Python/Windows runtime.
  Update dependencies and launch instructions only after selecting and
  testing a compatible combination. Reuse A2's incremental data handling where
  practical.
- Launch the viewer explicitly from the main GUI or independently. A chart
  crash, close, or restart must not terminate the control GUI or service.
  Define process ownership and clean shutdown; do not start a second service
  or unexpectedly connect to a controller.
- Preserve the chart features established in A2, including historical gaps,
  live/stale data, zoom/pan/home/clear, zone selection, and persisted viewer
  settings. Provide a clear fallback or rollback path until parity is verified.
- Benchmark real interaction and long-history load versus A2, including high
  data rates, rotation, process failure, and closing/reopening the viewer.

## Safety and compatibility

- Never connect to a real controller or send real control commands, change
  setpoints, start autotune, or change a run schedule without separate,
  explicit operator authorization. Use fake services, synthetic data, and
  temporary directories.
- Keep the service as sole serial/controller owner. Do not duplicate its
  driver or interrupt transactions. Preserve command semantics and existing
  GUI/CLI behavior. Make stale data and failures visible; bound processes,
  memory, queues, and repeated work.
- Keep Python 3.12/Windows support and the current chart safety guard. If a
  package is needed, update dependency manifests, installation instructions,
  and relevant launchers together; review platform compatibility.
- Preserve user changes. Do not perform destructive git operations, commit,
  or modify unrelated files.

## C gate and report

Run data-equivalence, interaction, launch, failure-isolation, dependency, and
Windows packaging tests. Report any intentionally missing features and do not
retire the existing chart until the replacement is verified. Use the
configured test runner and existing test patterns; update affected
documentation.

Report changes, comparisons with A2 and measured workload/units, test
commands/results, unverified Windows/hardware checks, feature gaps, rollback
status, and remaining risks. Distinguish simulated tests from operator
validation. Stop at this gate; do not migrate the control GUI to Qt.
