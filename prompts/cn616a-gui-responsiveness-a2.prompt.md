---
name: cn616a-gui-responsiveness-a2
description: "Add shared state snapshots and efficient incremental chart history to the Tkinter GUI."
argument-hint: "Add constraints or identify the verified A1 gate evidence"
agent: agent
---

# CN616A GUI responsiveness: A2

Implement and validate the shared-snapshot and in-window chart stage. This is
the third step: Baseline, A1, A2, B, C, D, E. Read
[README.md](../README.md), inspect current code/tests, and verify A1's changes
and gate evidence in the repository. If A1 prerequisites are missing or
failing, report the gap and stop; do not silently implement A1. Work only on
A2. Preserve existing workflows and the invariant that only the service owns
the serial port and talks to the controller.

## Shared snapshots and chart history

- Consolidate repeated state-file reads into a shared snapshot mechanism with
  bounded background I/O, change detection, explicit parse/read errors, and
  data-age reporting. Read each needed snapshot once per update, distribute
  immutable or safely owned data, and update Tk widgets only on the main
  thread. Recover from missing, replaced, partially written, or stale files;
  do not present old values as fresh.
- Replace repeated full-window chart rescans with incremental history ingestion
  outside the Tk thread. Bound in-memory history and background work while
  preserving access to requested historical windows. Handle appends,
  incomplete JSONL lines, rotation/renames, truncation, restart, duplicate/
  out-of-order records, time zones, missing values, and gaps without silently
  fabricating or dropping valid data. Reload safely when a longer window is
  requested.
- Render only the selected/visible zone as needed. Coalesce obsolete render
  requests, retain the newest pending data, reject stale results after zone,
  size, or configuration changes, and handle worker crashes and shutdown.
  Avoid accumulating processes, image objects, or callbacks. Keep chart
  display and image creation bounded on the Tk thread.
- Preserve PV, absolute setpoint, autotune setpoint, MAE, zone names, colors,
  line visibility/width, history length, timestamps, gaps, zoom, pan, home,
  clear semantics, live metrics, and current safety guards. If downsampling
  is required, preserve extrema and discontinuities and leave raw logs
  unchanged.

## Safety and compatibility

- Never connect to a real controller or send real control commands, change
  setpoints, start autotune, or change a run schedule without separate,
  explicit operator authorization. Use fake services/controllers, synthetic
  data, and temporary directories. Do not modify production logs.
- Keep controller I/O serialized by the service; preserve telemetry,
  scheduling/recovery, CLI and JSON Lines behavior. Do not silently retry
  potentially mutating commands or misrepresent timeout/partial outcomes.
- Preserve units, identities, names, timestamps, stale-data indication,
  editable values, chart/viewer settings, and existing config/log formats.
  Surface errors. Bound memory, queues, workers, and repeated work.
- Keep Python 3.12/Windows support and the current chart safety guard. Add no
  package unless required; update dependencies, installation instructions,
  and launchers together if one is needed.
- Preserve user changes. Do not perform destructive git operations, commit,
  or edit unrelated files.

## A2 gate and report

Test incremental results against a full-read reference for normal, rotated,
partial, truncated, and restarted logs. Measure history bytes processed per
update, selected versus hidden rendering, queue bounds, CPU, and event-loop
latency using the Baseline fixture. Verify chart interaction and worker failure
recovery; perform a long-running Windows soak when available and report its
actual duration. Run focused automated tests using repository patterns and the
configured runner, e.g. `.\.venv\Scripts\python.exe -m pytest -q` with
relevant files. Update directly affected docs/launchers.

Report changes, fixture and before/after measurements with units/percentiles,
test commands/results, unverified Windows/hardware checks, remaining chart
risks, and whether the next stage is warranted. Distinguish simulated tests
from operator validation. Stop and report at this gate; do not implement B.
