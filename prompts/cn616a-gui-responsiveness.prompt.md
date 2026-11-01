---
name: cn616a-gui-responsiveness
description: "Implement and validate the CN616A responsiveness refactor in safe, sequential stages."
argument-hint: "phase=baseline|A1|A2|B|C|D|E|through-B|all; include any constraints"
agent: agent
---

# CN616A GUI responsiveness: staged implementation

Improve the responsiveness, correctness, and stability of this Windows-oriented
controller application. Work from the current repository, not from assumptions
about an earlier version. Preserve existing behavior and operator workflows
unless a change is explicitly authorized, and validate each stage before
proceeding.

**This is an implementation prompt.** Nothing in this document runs until the
prompt is invoked. When invoked, follow the requested scope and the gates below.
If the accompanying request asks only for analysis or a plan, do not implement.

## Scope and order

Treat the options as a sequence, not as five unrelated rewrites:

1. **Baseline**: reproduce and measure the current behavior.
2. **A1**: fix the highest-impact GUI stalls and editing problems while
   retaining Tkinter and the existing service protocol.
3. **A2**: finish the Tkinter-side data and chart refactor.
4. **B**: improve service-side command scheduling and responsiveness.
5. **C**: isolate the live chart in a separate plotting application.
6. **D**: migrate the control GUI to PySide6/Qt, reusing the work above.
7. **E**: add a browser-based interface, retaining the service as the
   controller owner.

The original recommendation was to try the easy, high-impact work first.
Stages C, D, and E are larger options, not prerequisites for a responsive
Tkinter GUI. Do not start them merely because they are described here.

Interpret `phase=` in the user's accompanying message:

- If no phase is specified, run **Baseline and A1 only**, report the results,
  and stop. This is the preferred first experiment.
- `phase=baseline` measures and reports only. Add temporary or opt-in
  instrumentation only if necessary to measure; do not implement fixes.
- `phase=A1`, `A2`, `B`, `C`, `D`, or `E` authorizes that stage only. Verify
  its earlier prerequisites in the current repository first; if they are
  missing, report the gap rather than silently implementing extra stages.
- `phase=through-B` authorizes Baseline through B, in order. Likewise,
  `phase=through-A2`, `phase=through-C`, `phase=through-D`, and
  `phase=through-E` authorize the sequence through that stage.
- `phase=all` explicitly authorizes the entire sequence through E, subject to
  the gates and safety restrictions below. It does **not** authorize real
  controller writes, deployment, or exposing a service to a network.

Accept an equivalent plain-language scope. A completed prerequisite must be
supported by code and test evidence, not just an earlier chat summary. At every
gate, stop if the required checks fail. If Windows GUI or hardware validation
is unavailable, distinguish simulated evidence from unperformed operator
validation; do not claim a hardware or long-running test passed. Do not deploy
or expose the application while a required safety check remains unverified.

## Project context and starting hypotheses

Read [README.md](../README.md) and inspect the current implementation and
tests before editing. The following are starting points, not proof that every
reported symptom has the same cause:

- [main_gui.py](../py/gui/main_gui.py) creates the notebook and starts panel
  refresh loops. [display_panels.py](../py/gui/display_panels.py) schedules
  `StatePanel` refresh callbacks on the Tk event thread.
- `estimate_log_storage` in [display_panels.py](../py/gui/display_panels.py)
  scans telemetry and analysis log lines. The Configuration refresh can invoke
  this repeatedly while its form is clean, including when the tab is hidden.
  Measure its contribution with realistic current and rotated logs.
- [run_panel.py](../py/gui/run_panel.py), [command_panel.py](../py/gui/command_panel.py),
  and Configuration and Ramp/Soak in
  [display_panels.py](../py/gui/display_panels.py) perform synchronous TCP
  operations from GUI callbacks. The Run panel also requests status during
  refresh. Confirm actual timing and all call sites.
- The service in [cn616a_service.py](../py/cn616a_service.py) queues commands
  for its main loop, which also performs controller polls. The GUI's socket
  timeout and the service's command-completion timeout differ. A timeout does
  not necessarily mean a queued or started controller write did not happen.
- [chart_panel.py](../py/gui/chart_panel.py) loads telemetry and analysis
  history, and its cache is invalidated as logs change. Rendering is already
  isolated in [chart_render_pool.py](../py/gui/chart_render_pool.py); history
  loading, data copying, scheduling, and displaying results still need to be
  measured. Do not describe the current renderer as entirely in-process.
- The zone command fields use focus selection and refresh-driven population;
  investigate both Tk click/selection behavior and overwrites during editing.
  Similar controls exist in Ramp/Soak. Determine the actual failure rather than
  assuming the focus binding is the only cause.
- Verify telemetry shapes end to end. The normal PID-field list in
  [cn616a.py](../py/cn616a.py) uses flat `sp_abs` and `autotune_sp` names,
  while its error fallback, documented examples, and test fixtures use some
  `_c` names or nested segment fields. Chart history also uses truthiness
  when choosing setpoint aliases, which can discard a valid zero. Determine
  the supported shapes rather than assuming a single example is canonical.
- If present, [cn616a_gui_error.log](../logs/cn616a_gui_error.log) and the GUI
  fault log provide historical evidence. Recent entries include folder-picker
  `TclError` reports. Older stacks may reflect older code. They do not prove
  that overload caused the folder-picker failure or that a historical chart
  crash still occurs in the current implementation.

Keep the architectural invariant in the README: **only the service owns the
serial port and talks to the controller**. The GUI, chart application, and any
web application are service clients/readers, never independent Modbus owners.

## Safety and compatibility rules for every stage

1. Never connect to a real controller, send a real control command, change
   setpoints, start autotune, or change a run schedule during automated work
   without separate explicit operator authorization. Use a fake controller,
   fake TCP service, synthetic logs, and temporary directories. Do not modify,
   delete, or benchmark against the operator's production logs.
2. Keep controller I/O serialized by the service; do not introduce concurrent
   serial access or interrupt a Modbus transaction. Preserve controller
   polling, telemetry persistence, scheduled run stop/recovery, and existing
   CLI and JSON Lines clients.
3. Do not silently retry a potentially mutating command after a timeout.
   Distinguish rejected/not sent, confirmed success, confirmed failure, partial
   completion, and unknown outcome. A timed-out request may still execute.
   Show the operator how to verify actual controller state before retrying.
4. Preserve units, zone and unit identity, names, timestamps, stale-data
   indication, editable values, chart history/gaps, viewer settings, and
   existing configuration and log formats. Make intentional behavior changes
   explicit and document migration or rollback.
5. Surface errors and stale/unavailable data visibly and in appropriate logs.
   Do not replace failures with success-shaped defaults or silently discard
   operator input. Bound queues, memory, worker lifetimes, and repeated work.
6. Keep Python 3.12/Windows support and the current chart safety guard unless
   a separately verified change justifies updating them. Do not add a package
   until its stage needs it; update the dependency manifest, installation
   instructions, and relevant launchers together.
7. Preserve existing user changes. Do not perform destructive git operations,
   commit changes, or modify unrelated files unless explicitly requested.

## Baseline: measure before changing behavior

Reproduce the user's symptoms with synthetic data or a fake service. Capture
enough information to compare the same workload before and after:

- Inspect the Windows/Python/Tk versions, refresh configuration, enabled zones,
  chart history window, and actual log shape. Record which observations are
  measured and which are still hypotheses.
- Build temporary fixtures including six zones, telemetry at 2 Hz, at least an
  hour of chart history, analysis/config snapshots, and several rotated files.
  Include growing logs and a service that is slow, disconnected, or returns
  errors. Do not assume one fixture represents every deployment.
- Measure Tk event-loop heartbeat delay and individual callback durations;
  report sample count, p50, p95, p99, and maximum. Measure first feedback after
  a click separately from service completion and controller write latency.
  Measure startup/warm-up separately.
- Compare at least: chart never opened, chart open, and chart opened then
  hidden; small versus large log sets; connected, delayed, and disconnected
  service. Record bytes/lines scanned, refresh rates, queue depth, CPU/memory
  when practical, and time for each suspected expensive callback.
- Exercise dropdowns, telemetry tab switching, setpoint/autotune editing, the
  Run folder picker, and a delayed command. Inspect errors and historical crash
  evidence without treating past stack traces as a current reproduction.

Suggested initial acceptance targets on the target Windows machine after
warm-up, using the same representative fixture: event-loop delay p95 below
100 ms and p99 below 250 ms, no recurring multi-second stalls, and visible
button feedback within 150 ms at p95 even when the fake service delays its
response. These are engineering targets, not claims about the existing system.
Record any missed targets, workload and machine details, and revise only with
an explicit rationale. Service/controller completion time is a separate
measurement and cannot be guaranteed faster than a blocked serial transaction.

## A1: easiest high-impact fixes while retaining Tkinter

Implement the smallest coherent changes that remove measured UI-thread stalls,
then verify the result before attempting the larger chart and data refactor.

### Remove recurring blocking work

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
  For the three zone updates, show which operations succeeded, failed, or have
  an unknown outcome; never claim an all-or-nothing transaction.

### Keep controls responsive and edits safe

- Avoid refreshing hidden panels or repeatedly rewriting unchanged widgets.
  Refresh the selected tab promptly when it becomes visible. Preserve required
  background run-status/recovery behavior and scheduled stops; changing which
  widget paints must not change service behavior. Avoid duplicate timers and
  ensure configuration-rate changes and shutdown cancel the right callbacks.
- Check telemetry values and status fields against actual service snapshots.
  Consistently handle documented or otherwise supported aliases and nested
  fields in the telemetry display, command form, Ramp/Soak status, and chart
  where applicable. Treat numeric zero as a valid value, distinguish it from
  missing data, and show unavailable fields honestly. Add fixtures for the
  normal driver output, supported alternate shapes, missing values, and zero.
- Fix setpoint and autotune entry behavior so focusing an existing value and
  typing `80` replaces `70` with `80`, not `7080` or `7800`. Cover mouse and
  keyboard focus, cursor movement after focus, select-all, deletion, paste,
  negative/decimal input, and field refreshes while focused or dirty. Apply a
  shared fix to equivalent Ramp/Soak fields if they have the same defect.
- Preserve deliberate in-progress edits across refreshes and tab changes.
  Handle zone changes using the established explicit discard behavior or a
  clearly communicated alternative; never apply an edit to the wrong zone.
  Validate finite numeric values and existing controller constraints without
  inventing unsupported setpoint limits.
- Do not reconfigure a combobox or its selection unnecessarily during user
  interaction. Test unit/zone/mode dropdowns repeatedly with refreshed data
  and a growing-log workload, including after several minutes of operation.
  Diagnose any remaining Windows-specific popdown issue rather than assuming
  that reduced load alone fixes it.
- Retest the Run directory picker on Windows. Investigate and report its
  `TclError` separately; surface a useful failure and preserve manual path
  entry. Do not claim it is fixed if the real dialog cannot be exercised.

### A1 gate

Use the Baseline workload to compare callback times and event-loop delay.
Verify that delayed/disconnected network calls and growing logs no longer block
the Tk thread; confirm no queued command is duplicated or mislabeled on a
timeout. Exercise telemetry, Configuration, Run, Ramp/Soak, and chart tab
selection. Run focused automated tests and report the remaining issues and
whether A2 is warranted.

## A2: shared snapshots and efficient in-window chart

Build on A1 rather than reintroducing synchronous work:

- Consolidate repeated state-file reads into a shared snapshot mechanism with
  bounded background I/O, change detection, explicit parse/read errors, and
  data-age reporting. Read each needed snapshot once per update, distribute
  immutable or safely owned data, and update Tk widgets only on the main
  thread. Recover from missing, replaced, partially written, or stale files;
  do not present old values as fresh.
- Replace repeated full-window chart rescans with incremental history ingestion
  outside the Tk thread. Bound in-memory history and background work while
  preserving access to requested historical windows. Handle appends, incomplete
  JSONL lines, rotation/renames, truncation, restart, duplicate/out-of-order
  records, time zones, missing values, and gaps without silently fabricating
  or dropping valid data. Reload safely when a longer window is requested.
- Render only the selected/visible zone as needed. Coalesce obsolete render
  requests, retain the newest pending data, reject stale results after zone,
  size, or configuration changes, and handle worker crashes and shutdown.
  Avoid accumulating processes, image objects, or callbacks. Keep chart
  display and image creation bounded on the Tk thread.
- Preserve PV, absolute setpoint, autotune setpoint, MAE, zone names, colors,
  line visibility/width, history length, timestamps, gaps, zoom, pan, home,
  clear semantics, live metrics, and current safety guards. If downsampling is
  required, preserve extrema and discontinuities and leave raw logs unchanged.

### A2 gate

Test incremental results against a full-read reference for normal, rotated,
partial, truncated, and restarted logs. Measure history bytes processed per
update, selected versus hidden rendering, queue bounds, CPU, and event-loop
latency using the Baseline fixture. Verify chart interaction and worker failure
recovery; perform a long-running Windows soak when available and report its
actual duration. Stop and report remaining chart risks.

## B: service command scheduling and protocol

Measure service queue wait, controller I/O duration, and end-to-end command
latency before changing the service. Then, only as justified by evidence:

- Reduce head-of-line blocking between long poll steps and queued commands
  without allowing concurrent controller access. If a poll is split into
  smaller steps, preserve safe transaction boundaries and coherent snapshots.
  Use bounded scheduling and fairness so telemetry, configuration, analysis,
  run scheduling, and commands cannot starve each other.
- If concurrent TCP clients are needed, bound clients and request sizes,
  handle slow/disconnected clients and newline-delimited framing, and preserve
  response IDs and compatibility with existing GUI and CLI clients. More
  network concurrency must not mean concurrent serial operations.
- Define and test command lifecycle and timeout semantics. A queued command
  that times out may still run in the current implementation. If cancellation
  before execution or later status lookup is introduced, make the transition
  race-safe and observable. Never pretend that an already-started write was
  cancelled or automatically retry an unknown outcome.
- Consider a combined zone-update operation only if it improves measured
  latency. Preserve existing operations for older clients, maintain ordering,
  validate every field, and return per-operation results. Unless real hardware
  guarantees atomicity, explicitly report partial completion and do not label
  the operation atomic. A service-produced run-status snapshot is an optional
  alternative to frequent requests; make freshness and write failures visible.

### B gate

Use fake controllers and concurrent fake clients to test ordering, timeouts
before and during execution, disconnects, retries/deduplication if added,
partial writes, fairness, run scheduling, poll cadence, and backward
compatibility. Verify there is never more than one serial operation in flight.
Report GUI feedback latency, queue wait, and controller completion separately;
do not promise sub-second hardware execution without measurements.

## C: separate chart application

Only implement this stage when explicitly in scope. Inventory current chart
features first, then create an independently launchable plotting interface that
reads the existing service data and does not own the serial port.

- Evaluate PySide6 plus pyqtgraph for the supported Python/Windows runtime.
  Update dependencies and launch instructions only after selecting and testing
  a compatible combination. Reuse A2's incremental data handling where practical.
- Launch the viewer explicitly from the main GUI or independently. A chart
  crash, close, or restart must not terminate the control GUI or service.
  Define process ownership and clean shutdown; do not start a second service
  or unexpectedly connect to a controller.
- Preserve the chart features listed in A2, including historical gaps,
  live/stale data, zoom/pan/home/clear, zone selection, and persisted viewer
  settings. Provide a clear fallback or rollback path until parity is verified.
- Benchmark real interaction and long-history load versus A2, including high
  data rates, rotation, process failure, and closing/reopening the viewer.

### C gate

Run data-equivalence, interaction, launch, failure-isolation, dependency, and
Windows packaging tests. Report any intentionally missing features and do not
retire the existing chart until the replacement is verified.

## D: migrate the control GUI to Qt

Only implement this stage when explicitly in scope and after evaluating A-C.
Do not assume changing frameworks fixes blocking work.

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

### D gate

Run focused automated tests plus a feature-by-feature parity checklist and
the same latency/soak workloads used for A-C. Compare results with the existing
GUI and document any gaps, migration steps, and rollback. Remove the old GUI
only after explicit approval and verified parity.

## E: browser-based interface

Only implement this stage when explicitly in scope. Establish whether the
browser interface is local-only or needs remote access; do not infer remote
exposure from the existence of this stage.

- Keep the service as the sole controller owner. Prefer a separate local web
  process or clearly isolated web layer that uses the existing service API and
  shared state/history abstractions; do not duplicate the serial driver.
- Bind to loopback by default. Protect mutating requests against cross-site
  requests and hostile origins, validate inputs, limit work and connections,
  and do not expose the unauthenticated raw TCP command endpoint. Keep assets
  local/offline by default and do not embed secrets in source or logs.
- Remote access or a non-loopback bind requires separate explicit approval and
  a documented threat model, authentication/authorization, transport security,
  origin/CSRF and WebSocket protections, auditability, and deployment review.
  `phase=all` alone does not grant that approval.
- Preserve the features and command-outcome semantics of the preceding GUI,
  support bounded live updates and chart backpressure, and make stale data and
  disconnects visible. Include startup, shutdown, Windows deployment, and
  rollback instructions.

### E gate

Test against a fake service with browser/integration tests, including malformed
and unauthorized requests, origin protections, reconnects, partial command
outcomes, slow clients, long-running updates, and accessibility. Confirm the
default listener is loopback-only and no real controller commands were sent.

## Test and reporting expectations

- Follow existing tests in [tests/](../tests/), including the fake controller in
  [test_service_commands.py](../tests/test_service_commands.py), GUI/state
  tests in [test_gui_and_state_reader.py](../tests/test_gui_and_state_reader.py),
  chart gap tests in [test_chart_panel_gaps.py](../tests/test_chart_panel_gaps.py),
  and retention tests in [test_service_utils.py](../tests/test_service_utils.py).
  Add focused regression tests for each changed behavior, not only unit tests
  for helpers. Use a temporary log directory and fake endpoints.
- Use the repository's configured test runner; for example, on Windows use
  `.\.venv\Scripts\python.exe -m pytest -q` with the relevant test files first,
  then broaden coverage when warranted. If a tool or display is unavailable,
  report it and use an existing suitable alternative rather than inventing a
  passing result.
- Include actual GUI interaction tests on Windows for focus, dropdowns, tab
  switching, modal dialogs, chart controls, and shutdown when the environment
  supports them. Separate automated simulations from manual/real-hardware
  validation. A simulated long run is not proof of a real-time soak.
- Update directly affected README, configuration, launchers, and protocol
  documentation as stages change behavior. Do not write a separate planning
  file unless the user asks for one.
- At each stop, report: stage completed, changes, benchmark fixture and
  before/after results with units and percentiles, test commands/results,
  unverified Windows or hardware checks, residual risks, and the next
  recommended stage. Do not claim performance targets or feature parity
  without measured evidence.
