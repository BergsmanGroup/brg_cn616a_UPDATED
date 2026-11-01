---
name: cn616a-gui-responsiveness-a1
description: "Remove measured Tkinter GUI stalls and protect in-progress control edits."
argument-hint: "Add constraints or identify the completed Baseline evidence"
agent: agent
---

# CN616A GUI responsiveness: A1

Implement and validate the first Tkinter improvement stage. This is the second
step in the sequence: Baseline, A1, A2, B, C, D, E. Run only A1. Read
[README.md](../README.md), inspect current code/tests, and verify Baseline
measurements or reproduce any missing baseline evidence. If essential evidence
or a prerequisite is missing, report the gap; do not silently perform A2 or
another stage. Preserve existing behavior and workflows except for changes
required to resolve measured defects.

Keep the architectural invariant in the README: **only the service owns the
serial port and talks to the controller**. GUI, chart, and web components are
service clients/readers, never independent Modbus owners.

## Remove recurring blocking work

- Stop scanning all historical log lines on every Configuration refresh.
  Move sampling/estimation work off the Tk thread, bound both bytes and records
  read independently of total history size, cache the result, and recompute
  only when relevant inputs or source files change. Sample representative
  current/rotated records where available. Preserve and clearly label the
  existing retention-estimate formula and missing-data behavior.
- The estimated file count influences retention and pruning. Test small and
  varied records against exact fixture sizes; make approximation and
  uncertainty visible. Do not silently shorten configured retention because
  of an uncertain or failed sample, and do not write service settings merely
  because the form refreshed.
- Move Run status polling and **all** GUI socket operations off the Tk event
  thread, including zone commands, autotune, run/schedule operations,
  Configuration, and Ramp/Soak. Reuse a shared, bounded client/worker
  abstraction where appropriate. Capture widget values on the Tk thread,
  return plain results through a bounded queue, and have a Tk-owned `after`
  callback apply results. Workers must not call Tk APIs, including `after`.
- Coalesce obsolete status requests, avoid unbounded queues and overlapping
  polls, and prevent read-only refresh traffic from starving operator commands.
  Handle TCP JSON Lines framing, response IDs, disconnects, malformed/partial
  responses, timeouts, worker errors, and orderly shutdown explicitly.
- Show immediate pending feedback and disable conflicting duplicate actions
  while a command is in flight. Preserve command order and stop on failure.
  For three zone updates, show which operations succeeded, failed, or have an
  unknown outcome; never claim an all-or-nothing transaction.

## Keep controls responsive and edits safe

- Avoid refreshing hidden panels or repeatedly rewriting unchanged widgets.
  Refresh the selected tab promptly when it becomes visible. Preserve required
  background run-status/recovery behavior and scheduled stops; changing which
  widget paints must not change service behavior. Avoid duplicate timers and
  ensure configuration-rate changes and shutdown cancel the right callbacks.
- Check telemetry values and status fields against actual service snapshots.
  Consistently handle documented or otherwise supported aliases and nested
  fields in telemetry, command form, Ramp/Soak status, and chart where
  applicable. Treat numeric zero as valid, distinguish it from missing data,
  and show unavailable fields honestly. Add fixtures for normal driver output,
  supported alternate shapes, missing values, and zero.
- Fix setpoint and autotune entry behavior so focusing an existing value and
  typing `80` replaces `70` with `80`, not `7080` or `7800`. Cover mouse and
  keyboard focus, cursor movement after focus, select-all, deletion, paste,
  negative/decimal input, and field refreshes while focused or dirty. Apply a
  shared fix to equivalent Ramp/Soak fields if they have the same defect.
- Preserve deliberate in-progress edits across refreshes and tab changes.
  Handle zone changes using established explicit discard behavior or a clearly
  communicated alternative; never apply an edit to the wrong zone. Validate
  finite numeric values and existing controller constraints without
  inventing unsupported setpoint limits.
- Do not reconfigure a combobox or its selection unnecessarily during user
  interaction. Test unit/zone/mode dropdowns repeatedly with refreshed data
  and a growing-log workload, including after several minutes. Diagnose any
  remaining Windows-specific popdown issue rather than assuming reduced load
  alone fixes it.
- Retest the Run directory picker on Windows. Investigate and report its
  `TclError` separately; surface a useful failure and preserve manual path
  entry. Do not claim it is fixed if the real dialog cannot be exercised.

## Safety and compatibility

- Never connect to a real controller or send real control commands, change
  setpoints, start autotune, or change a run schedule without separate,
  explicit operator authorization. Use fake controllers/services, synthetic
  logs, temporary directories, and fake endpoints.
- Keep controller I/O serialized by the service. Preserve polling, telemetry
  persistence, scheduled run stop/recovery, and existing CLI/JSON Lines
  clients. Never silently retry a potentially mutating command after timeout;
  show rejected/not sent, confirmed success/failure, partial completion, or
  unknown outcome accurately, and explain how to verify state before retrying.
- Preserve units, identity, names, timestamps, stale indication, editable
  values, chart history/gaps, viewer settings, and existing config/log formats.
  Surface errors visibly and in appropriate logs. Bound queues, memory, worker
  lifetimes, and repeated work.
- Keep Python 3.12/Windows support and the current chart safety guard. Add no
  package unless needed; if one is needed, update dependencies, installation
  instructions, and relevant launchers together.
- Preserve user changes. Do not perform destructive git operations, commit
  changes, or modify unrelated files.

## A1 gate and report

Use the Baseline workload to compare callback times and event-loop delay.
Verify delayed/disconnected network calls and growing logs no longer block Tk;
confirm no command is duplicated or mislabeled on timeout. Exercise telemetry,
Configuration, Run, Ramp/Soak, and chart tab selection. Run focused automated
regression tests. Follow patterns in [tests/](../tests/), especially
[test_service_commands.py](../tests/test_service_commands.py),
[test_gui_and_state_reader.py](../tests/test_gui_and_state_reader.py),
[test_chart_panel_gaps.py](../tests/test_chart_panel_gaps.py), and
[test_service_utils.py](../tests/test_service_utils.py). On Windows, use the
configured runner, e.g. `.\.venv\Scripts\python.exe -m pytest -q` with focused
files first.

Update directly affected README, configuration, launchers, and protocol docs.
Report changes, baseline and after measurements with fixture/units/percentiles,
tests, unverified Windows or hardware checks, residual issues, and whether A2
is warranted. Distinguish simulated evidence from manual/real-hardware
validation; do not claim a performance target or Windows dialog test passed
without evidence. Stop at this gate.
