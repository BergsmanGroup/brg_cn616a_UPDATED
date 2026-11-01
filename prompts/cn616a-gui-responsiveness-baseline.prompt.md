---
name: cn616a-gui-responsiveness-baseline
description: "Measure and document the current CN616A GUI responsiveness without implementing fixes."
argument-hint: "Add machine details or constraints for the baseline run"
agent: agent
---

# CN616A GUI responsiveness: Baseline

Measure the current behavior of this Windows-oriented controller application
without changing its behavior. Work from the current repository, not from
assumptions about an earlier version. This prompt performs the first step of
the sequence: Baseline, A1, A2, B, C, D, E. Do not start a later step here.

## Context and hypotheses to verify

Read [README.md](../README.md), inspect the current implementation and tests,
and record which observations are measurements and which are hypotheses:

- [main_gui.py](../py/gui/main_gui.py) creates the notebook and starts panel
  refresh loops. [display_panels.py](../py/gui/display_panels.py) schedules
  `StatePanel` refresh callbacks on the Tk event thread.
- `estimate_log_storage` in [display_panels.py](../py/gui/display_panels.py)
  scans telemetry and analysis log lines. The Configuration refresh may invoke
  it repeatedly, including when its tab is hidden. Measure its contribution
  with realistic current and rotated logs.
- [run_panel.py](../py/gui/run_panel.py), [command_panel.py](../py/gui/command_panel.py),
  and Configuration and Ramp/Soak in [display_panels.py](../py/gui/display_panels.py)
  may perform synchronous TCP operations from GUI callbacks. The Run panel
  may request status during refresh. Confirm actual timing and call sites.
- [cn616a_service.py](../py/cn616a_service.py) queues commands for its main
  loop, which also performs controller polls. The GUI socket timeout and
  service command-completion timeout differ. A timeout does not necessarily
  mean a queued or started controller write did not happen.
- [chart_panel.py](../py/gui/chart_panel.py) loads telemetry and analysis
  history, and its cache is invalidated as logs change. Rendering is already
  isolated in [chart_render_pool.py](../py/gui/chart_render_pool.py); history
  loading, data copying, scheduling, and displaying results still need to be
  measured. Do not describe the current renderer as entirely in-process.
- Zone command fields use focus selection and refresh-driven population;
  investigate Tk click/selection behavior and overwrites during editing.
  Similar controls exist in Ramp/Soak. Determine actual failures.
- Verify telemetry shapes end to end. The normal PID-field list in
  [cn616a.py](../py/cn616a.py) uses flat `sp_abs` and `autotune_sp` names,
  while its error fallback, documented examples, and test fixtures use some
  `_c` names or nested segment fields. Chart history may use truthiness when
  choosing setpoint aliases, which can discard a valid zero. Determine
  supported shapes rather than assuming one example is canonical.
- If present, `logs\cn616a_gui_error.log` and the GUI fault log provide
  historical evidence. Recent entries include folder-picker `TclError`
  reports. Older stacks may reflect older code; do not treat them as current
  reproductions or proof of a particular cause.

Keep the architectural invariant in the README: **only the service owns the
serial port and talks to the controller**. The GUI, chart application, and any
web application are service clients/readers, never independent Modbus owners.

## Measurement procedure

Reproduce reported symptoms with synthetic data or a fake service. Use
temporary fixtures, never operator production logs. Capture enough detail to
compare the same workload later:

- Use the isolated runner at
  [gui_responsiveness_baseline.py](../tests/gui_responsiveness_baseline.py)
  to launch the actual Tk GUI against temporary synthetic files and a
  loopback-only fake JSON Lines endpoint. It never starts or connects to the
  real service. The default fixture is six zones at 2 Hz for one hour, with
  rotated logs and synthetic live appends. Run `--help` for the scenario and
  copy options. If including current logs, use `--copy-logs-from`; it copies
  recognized history files and never writes to the source directory. The
  report includes actual JSONL bytes/newline-delimited lines read by callback,
  not just the fixture's total file sizes.
- Use `bat\responsiveness_test.bat` on a separate test machine for the
  default quick chart-open normal/delayed comparison. Use `--verify-data`
  for integrity/quantity checks and `--summarize <report.json>` for refined
  metrics without opening a GUI or network. The launcher never starts the
  real service. The default duration is 30 seconds with up to 20 button
  samples; report insufficient samples explicitly, and increase duration
  when necessary. Do not bypass the chart safety guard.
- Retain [the v1 raw reference](../tests/fixtures/cn616a-baseline-v1/README.md)
  unchanged. It is a 3,823-byte gzip of CRC-valid **synthetic** Modbus RTU
  transactions and a frozen register map, not a physical capture or service
  JSON. Sixteen snapshots repeat to generate 7,200 telemetry and 7,200 derived
  analysis rows for one hour at 2 Hz. Only the compressed reference/code is
  versioned; temporary uncompressed GUI logs remain disposable. Record the
  report's raw-input hash and adapter/report version for later comparisons.
- Run the harness from an interactive Windows desktop session. Tab and
  combobox events plus button callbacks are simulated in Tk; this is not
  OS-level mouse/keyboard automation. The folder-picker callback uses a fake
  chooser, so the native Windows dialog still requires a separate manual test.

- Record Windows, Python, and Tk versions; refresh configuration; enabled
  zones; chart history window; and actual log shape.
- Build temporary fixtures including six zones, telemetry at 2 Hz, at least
  one hour of chart history, analysis/config snapshots, and several rotated
  files. Include growing logs and a service that is slow, disconnected, or
  returns errors. Do not assume one fixture represents every deployment.
- Measure Tk event-loop heartbeat delay and individual callback durations;
  report sample count, p50, p95, p99, and maximum. Measure first feedback after
  a click separately from service completion and controller write latency.
  Measure startup/warm-up separately.
- Compare chart never opened, chart open, and chart opened then hidden; small
  versus large log sets; connected, delayed, and disconnected service. Record
  bytes/lines scanned, refresh rates, queue depth, CPU/memory when practical,
  and time for each suspected expensive callback.
- Exercise dropdowns, telemetry tab switching, setpoint/autotune editing, the
  Run folder picker, and a delayed command. Inspect errors and historical
  crash evidence without treating past stack traces as a current reproduction.

Suggested initial acceptance targets on the target Windows machine after
warm-up, using the same representative fixture: event-loop delay p95 below
100 ms and p99 below 250 ms, no recurring multi-second stalls, and visible
button feedback within 150 ms at p95 even when the fake service delays its
response. These are engineering targets, not claims about the existing system.
Record missed targets, workload and machine details, and revise only with an
explicit rationale. Service/controller completion time is separate and cannot
be guaranteed faster than a blocked serial transaction.

## Safety and compatibility

- Do not connect to a real controller, send a real control command, change
  setpoints, start autotune, or change a run schedule without separate explicit
  operator authorization. Use a fake controller/service and temporary data.
- For the automated baseline, do not point the GUI at the live service. The
  harness binds its fake endpoint to loopback and writes that address into its
  temporary service-config snapshot. Live monitoring is optional and is not
  required to establish this controlled baseline.
- Do not modify, delete, or benchmark against production logs. Preserve user
  changes; do not perform destructive git operations, commit, or modify
  unrelated files.
- Do not implement fixes in this stage. Temporary or opt-in instrumentation
  is allowed only when needed to measure and must not alter normal behavior.
- Keep Python 3.12/Windows support and the current chart safety guard. Do not
  add packages for this measurement step.

## Tests and report

Follow existing patterns in [tests/](../tests/), including
[test_service_commands.py](../tests/test_service_commands.py),
[test_gui_and_state_reader.py](../tests/test_gui_and_state_reader.py),
[test_chart_panel_gaps.py](../tests/test_chart_panel_gaps.py), and
[test_service_utils.py](../tests/test_service_utils.py). Use the configured
test runner where useful; on Windows, for example,
`.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_gui_responsiveness_baseline.py"`.

Report the fixture and machine details, measurements and units/percentiles,
test commands/results, unperformed Windows or hardware checks, limitations,
and the recommended next step (A1). Clearly distinguish simulated evidence
from operator validation. Do not claim a target or hardware test passed without
measurement. If the required workload or GUI cannot be exercised, state the
gap rather than inventing results.
