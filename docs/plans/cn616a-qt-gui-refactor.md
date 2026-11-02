# CN616A Qt GUI: two-release implementation plan

**Status:** implementation plan only; this revision does not implement the refactor.  
**Release 1:** make a usable PySide6 + pyqtgraph replacement tonight, using the existing single-controller service.  
**Release 2:** add multi-controller service capabilities later, without restructuring the Qt identity model.

**Execution update:** development and assistant-run validation use repository data
and isolated fakes only. There is no live controller connection. All hardware
testing is reserved for the user; no assistant step may execute it. Use the
[12-step prompt workflow](cn616a-qt-gui-workflow.md) for detailed instructions,
per-step Markdown reports, and frequent user UI review gates. No implementation
step has been executed by creating these instructions.

## 1. Execution decision and scope

Start production Qt implementation on the dedicated branch when instructed. Permission includes new GUI code, dependencies, launchers, and narrowly necessary service fixes. There is **no additional visual-design approval, API-v2 prerequisite, or simulated-only phase gate** before migrating existing working controls.

Keep the Tkinter application and current service operational throughout. Add an explicit Qt launch path instead of replacing the Tk launcher tonight. Aim for functional parity, responsive interaction, honest error states, and reliable recording before visual polish. A partial application must be labeled partial, not declared a completed replacement.

### Essential Release 1

- Four tabs: **Overview, Configuration, Ramp/Soak, Logs**.
- Real telemetry, analysis/PID readbacks, setpoint and existing control operations, service configuration, recording, and profile editing/execution.
- Asynchronous access to the current TCP JSON Lines endpoint and service-owned snapshot/history files; no GUI serial connection.
- Embedded live pyqtgraph chart plus optional floating chart windows sharing the same data source.
- Explicit pending, rejected, not-sent, ambiguous, stale, and missing-data states; preserve unsaved edits.
- Existing service commands, JSON snapshots, JSONL histories, run metadata, and recording recovery behavior remain compatible.
- Qt model identities use `(bus, controller, zone)` from the beginning, even though only one controller is connected.
- Automated fake-service tests and offscreen Qt smoke/interaction tests. Offline
  UI review and eventual hardware checks are separate user-owned acceptance gates;
  hardware availability is not a development prerequisite.

### Optional polish; cut this first

Pixel-perfect mockup styling, animation, themes, arbitrary docks, saved window arrangements, frozen identity columns, rich log search, chart/image/CSV export, installers, and 12/18-zone layout tuning. Do not spend tonight certifying synthetic multi-controller screens.

Basic chart colors, line visibility, time window, zoom/pan/reset, and existing viewer settings are parity work, not an excuse to redesign plotting preferences. Retention estimates can initially be plain text using the existing worker; a richer visualization is optional.

## 2. Repository evidence and shortest practical path

| Existing surface | What to reuse or port |
|---|---|
| [Tk entry point](../../py/gui/main_gui.py) | Current tabs are Telemetry, Run, Configuration, Ramp/Soak, Chart. Fold Telemetry + Run + Chart into Overview; retain the remaining functions in the four-tab layout. Preserve startup options for logs directory, refresh interval, and debug logging. |
| [Command panel](../../py/gui/command_panel.py) | Absolute SP, control **method**, autotune SP, autotune start/stop, pending lockout, and dirty-edit protection. Its generic “Mode” field sends `set_control_method`; do not confuse this with profile control mode. |
| [Display/config/profile panels](../../py/gui/display_panels.py) | Telemetry formatting, service-settings patches, connection actions, system/zone readbacks, retention/viewer settings, and the full 20-row profile editor. Port behavior, not Tk widget classes. |
| [Recording panel](../../py/gui/run_panel.py) | Run directory/name, start/stop, Pacific-time scheduled stop, current status, last-data timestamp, and recovered-run interruption information. “Run” here means recording, not controller Run/Idle. |
| [ServiceClient](../../py/gui/service_client.py) | Already toolkit-independent in implementation: bounded worker, correlated requests, ordered operations, fail-stop on unsuccessful outcomes, and no ambiguous-write retry. Drive `poll_results()` from Qt rather than rewriting the transport. |
| [Snapshot reader](../../py/gui/snapshot_reader.py), [state normalization](../../py/gui/state_values.py) | Existing background reads, five snapshot categories, age/errors, and aliases. Adapt these into typed Qt-facing records. |
| [History reader](../../py/gui/history_reader.py), [retention estimator](../../py/gui/retention_estimator.py) | Background incremental telemetry/analysis ingestion, rotation/truncation handling, bounded data, and background retention work. Wrap their public APIs rather than copying ingestion into a Qt timer. |
| [Service](../../py/cn616a_service.py), [driver](../../py/cn616a.py) | One synchronous serial owner and one controller. Preserve their behavior; the Qt GUI is only another service client. |
| [HTML mockup](../../design/cn616a-gui/mockups/index.html), [Qt prototype](../../design/cn616a-gui/prototype.py), [design notes](../../design/cn616a-gui/README.md) | Reuse layout, typography/color roles, four-tab organization, and same-process floating-chart pattern. Synthetic values/actions are not production functionality. Do not copy demo timers, simulated recording, or activation buttons into the live app. |

### Compatibility facts that determine the implementation

1. The current service accepts `set_sp_abs`, `set_control_method`, `set_control_mode`, `set_autotune_setpoint`, `start_autotune`, `stop_autotune`, configuration/connection commands, profile commands, and recording commands. Release 1 uses these exact operations and payloads.
2. There is **no service operation for writing PID gains**. Show real gains/analysis where supplied; keep unsupported fields read-only. Adding gain writes is Release 2, not required to reproduce a synthetic editable mockup.
3. There is no verified service command for hardware zone Enable/Disable or controller-wide Run/Idle. Omit those actions or visibly label them unavailable. Polling-zone selection is not hardware activation.
4. The service constructs one driver. The existing command panel's `unit` field does **not** make service operations route to another controller. Show the actual configured controller identity, not an editable multi-device target selector.
5. Recording mirrors existing JSONL streams, with run metadata and persisted recovery state; it is not CSV capture. GUI closure must not stop service recording.
6. Ramp/soak polling can be disabled by cadence. Preserve explicit `read_rampsoak` on entry/reload; do not mistake an old profile snapshot for a fresh controller read.
7. Service commands are serialized between poll operations. A long synchronous controller call cannot be interrupted. GUI responsiveness must not depend on service response time.
8. A successful `set_service_config` echoes stored settings; it does not by itself prove a TCP listener rebound or a serial parameter changed. Keep the active endpoint separate from edited settings and verify reachability/identity before switching clients. Do not promise hot reconfiguration absent from the existing service.

## 3. Release 1 architecture: small adapter, not a service rewrite

### Identity model

Define immutable `ControllerKey(bus_id, controller_id)` and `ZoneKey(bus_id, controller_id, zone_id)` records. `controller_id` identifies the configured unit; keep its Modbus address as explicit metadata. For Release 1, use one stable configured logical bus ID and the existing service's unit/address identity, with local zones 1–6. Never use global row numbers as targets.

- Use these keys in table rows, selection, command intents, chart subscriptions, and cached state; labels/names are presentation only.
- Wrap legacy snapshots and histories at the adapter boundary. Do **not** add identity fields to existing on-disk files tonight.
- If identity cannot be established unambiguously, show available readings with an identity warning and disable writes until configuration/readback resolves it. Never default an unknown hardware target to address 1.
- Reject command intents for any controller other than the adapter's single configured target. Translate valid intents into the current zone-based v1 messages; adding an ignored `unit` field is not routing.
- Tag results with endpoint/identity generation. On connection changes, discard obsolete display results, retain/report command outcomes, and prevent old results from updating another target. Do not relabel old history when switching service endpoints/log directories.
- Release 2 can replace the adapter's backend and controller registry without changing widget/model keys. No generic bus manager or plugin framework is needed tonight.

### Threading and data flow

`Service + existing files -> background readers/client -> Qt adapter -> models -> widgets/charts`

- A thin `QObject` adapter owns the existing client/readers and exposes typed snapshots, command submission, status, and shutdown. Verify helper imports remain free of Tk initialization.
- The client's socket work and readers' file parsing stay in their existing workers. A short Qt timer drains completed results and cached updates on the GUI thread; use queued signals if additional worker delivery is needed. No second transport, asyncio integration, or blocking widget callbacks.
- Normalize/aggregate large histories off the GUI thread. Ignore superseded refresh generations; coalesce status reads; keep command queues bounded and expose full/closed submission failures.
- Maintain recording/service status even while another tab is selected. Refresh expensive profile/config views on demand, without overwriting dirty forms or changing selection.
- Cache one bounded history source per application. Embedded and floating plots share it; floating windows create no sockets, file readers, or independent sampling loops.
- Use `QAbstractTableModel` for keyed telemetry rows and profile segments; a plain form is sufficient for configuration. Avoid a new abstraction for every card.
- GUI shutdown stops timers, disconnects chart subscriptions, and shuts down workers with bounded waits. Report pending/unknown operations; do not shut down the service, stop recording, or replay commands on restart.

### Command safeguards without new migration blockers

- Validate finite numeric values, existing units/ranges/enums, selected target, and profile segment limits before submission. Never treat missing data as zero.
- Preserve operation ordering and report each outcome. The existing three-operation zone update is not atomic; if operation two fails, show operation one's success and operation three's not-sent state.
- Distinguish transport acknowledgment from controller readback. A current v1 `ok` response may only mean the driver call returned. Show “acknowledged; awaiting readback” until a fresh relevant poll matches; do not claim independent readback if that field is unavailable.
- Preserve the client's confirmed-success/rejection, `not_sent`, and `unknown` semantics. A post-send timeout can mean the command executed. Disable duplicate submission while pending; never automatically retry writes, autotune triggers, profile transitions, or recording start/stop.
- For an ambiguous operation, present the target, operation, and error, then refresh status/readback. Where readback cannot disambiguate a trigger, keep the uncertainty visible and require an explicit operator decision, not a retry loop.
- Confirm autotune starts and all-zone/profile transitions with actual targets and impact. Keep recording controls unmistakably separate from thermal-control operations.
- Later user-only hardware checks cover the migrated action on the existing controller; the assistant does not execute them. Unverified future Enable/Disable, Run/Idle, discovery, or multi-device scheduling must **not** block porting existing working controls. Any specific command whose semantics remain ambiguous stays individually unavailable with a reason; the rest of the app can ship.

## 4. Concrete implementation sequence for tonight

Work in small runnable slices, not separate multi-day design projects. The
milestones below describe feature scope; the authoritative execution order is
the [12-step workflow](cn616a-qt-gui-workflow.md). Each prompt ends with tests,
a Markdown report and an offline user walkthrough, then stops. Require a passing
software gate and disposition of user feedback before the next prompt; hardware
acceptance can remain pending throughout. Use repository-derived replay rather
than requiring an existing service.

Prompt mapping: 01 foundation; 02 adapter; 03 Overview; 04 charts; 05 zone
controls; 06 recording; 07 configuration; 08 profile editor; 09 profile execution
and autotune; 10 Logs; 11 reliability; 12 acceptance handoff. This splits the
milestones into smaller user-reviewable units; it does not add Release 2 scope.

| Milestone / budget | Implementation and dependency | Tests and acceptance |
|---|---|---|
| **M0: runnable branch setup, 15–25 min** | Capture relevant existing test results; add optional Qt dependencies and an explicit Qt launcher. Keep Tk/default service launch intact. Confirm Python 3.12 environment and reuse the prototype dependency ranges. No exhaustive baseline exercise. | Qt imports and offscreen shell startup succeed; existing Tk imports/startup remain usable. Launcher help works without hardware or service startup. Missing Qt dependencies produce an actionable error. |
| **M1: connected four-tab app, 45–75 min** | After M0, create the Qt entry point, identity records, adapter, telemetry model, and four tab containers. Connect to temporary repository-derived snapshots and an owned fake endpoint; implement missing/stale/error display and runtime logging. Real-service mode is implemented for later user testing, never exercised by the assistant. | Fake snapshots match Tk-normalized values, names, PID/analysis readbacks, timestamps, and zones. Sorting/selection retains `ZoneKey`. Missing/malformed/stale files and service down do not crash or show unlabelled simulated data. A foreign controller key cannot generate a write. Tab switching does no blocking I/O. |
| **M2: first usable operating slice, 60–90 min** | After M1, wire SP/method/autotune-SP updates, recording start/stop/status, and an embedded PV/SP live chart using incremental history. Add optional floating chart creation now using the same plot component. | Fake JSONL server receives exact legacy operations, order, and payloads. Queue-full, rejection, partial sequence failure, and post-send timeout are visible; no automatic retry. Recording status survives tab switches. Embedded/floating plots update from one cache; closing a plot cannot stop recording. This is the first useful fallback stopping point, subject to section 8. |
| **M3: configuration and remaining zone controls, 45–75 min** | After M2, port service polling/connection settings, zone names/list, equilibrium/retention/viewer settings, system/zone readbacks, and autotune start/stop with safeguards. Retain existing service-patch semantics. | Assert configuration patch keys and enum mappings against Tk behavior. Dirty entries survive polling and delayed replies. Connection actions use the active endpoint; an echoed settings patch alone never switches clients or claims listener/serial reconfiguration. Verify any requested new endpoint and identity separately; failed transitions stay explicit. Retention scans remain off-thread. Autotune tests cover confirmation, pending lockout, rejection, unknown, and readback. |
| **M4: complete Ramp/Soak, 60–90 min** | After M3, migrate the 20-row SP/slope/time editor, count, control mode, live state, reload/save, and selected/all-zone start/stop. Submit `read_rampsoak` on entry, including when periodic profile polling is off; development targets only the fake backend. | Test count 0–20 and invalid/non-finite fields, Stop/Hold enum mapping, exact affected-zone lists, partial/error/unknown outcomes, reload without edit clobbering, and active-segment display only when provided. Exercise existing profile service operations against fakes; actual device checks remain user-only and pending. |
| **M5: Logs, parity sweep, handoff, 45–75 min** | After M4, finish bounded diagnostic/history tails and category selection, complete chart series/preferences and recording schedule/recovery UI, then inspect every checklist row. Fix migration bugs before styling. Document launch/rollback and known limitations. | Test Pacific-time schedule conversion/validation and exact `set_run_schedule` payload; reconcile recovered runs from service status. Test log rotation, truncation, malformed lines, disconnected/restarted service, GUI restart during recording, chart gaps/caps, and bounded shutdown. Run the focused regression selections and the responsiveness acceptance below. |

**Original coding timebox: about 4.5–7 hours**, not a guarantee. Twelve staged
sessions, offline replay setup and user feedback increase elapsed time. No
hardware testing is included in assistant work. Reserve the final 30–45 minutes
for validation/handoff; if time is short, steps 01–06 deliver the M2-equivalent
partial UI. Do not skip software safety gates to meet a tonight deadline.

## 5. Files to create, modify, reuse, and preserve

Paths below are the proposed implementation surfaces, not files created by this plan revision.

### Create

- `py/qt_gui/__init__.py`, `__main__.py`, `main_window.py`: entry point, arguments, four tabs, logging, lifecycle.
- `py/qt_gui/models.py`: identity/value records and keyed table models.
- `py/qt_gui/service_adapter.py`: current-service translation, command outcomes, status and reader lifecycle.
- `py/qt_gui/overview.py`, `configuration.py`, `ramp_soak.py`, `logs.py`: corresponding views and their behavior.
- `py/qt_gui/charts.py`: shared embedded/floating pyqtgraph widget and bounded series presentation.
- `requirements-qt.txt`: include existing `requirements.txt` plus PySide6, pyqtgraph, and NumPy; start from [design dependency ranges](../../design/cn616a-gui/requirements.txt) and record a tested combination. Do not upgrade pymodbus.
- `bat/gui_qt_start.bat`: explicit opt-in launch with the repository interpreter and forwarded arguments; use `python -m py.qt_gui` from the repository root.
- `tests/test_qt_gui_adapter.py`, `test_qt_gui_models.py`, `test_qt_gui_controls.py`, `test_qt_gui_charts.py`, `test_qt_gui_smoke.py`: fake transport/files, identity, parity actions, plotting, and lifecycle. Use existing unittest conventions and Qt's test/event-loop facilities rather than adding a second test framework.

### Modify during implementation, only as needed

- [README](../../README.md) and [batch documentation](../../bat/README.md): optional dependency install, explicit service + Qt launch, parity limitations, and immediate Tk rollback.
- [Service client](../../py/gui/service_client.py), [snapshot reader](../../py/gui/snapshot_reader.py), [history reader](../../py/gui/history_reader.py), [state helpers](../../py/gui/state_values.py), [retention estimator](../../py/gui/retention_estimator.py): reuse unchanged first. If a toolkit-neutral seam is genuinely missing, make a small compatible change with both Tk and Qt regression tests; do not relocate the whole Tk package.
- [Service](../../py/cn616a_service.py) and directly related tests **only** for a reproduced compatibility defect blocking an existing operation. Record the defect, add a failing regression, preserve v1 payloads/formats, and verify Tk/CLI behavior. No scheduler or schema overhaul.

### Preserve as working rollback/reference

- [Tk GUI package](../../py/gui/), including its matplotlib renderer, and [Tk launcher](../../bat/gui_start.bat). No Qt imports in their startup path.
- [Service launcher](../../bat/service_start.bat), [stop launcher](../../bat/service_stop.bat), [CLI](../../py/cn616a_cli.py), [driver](../../py/cn616a.py), [register map](../../cn616a_register_map.json), base [dependencies](../../requirements.txt), and existing test fixtures.
- Existing snapshots, JSONL files, run directories, active-run state, and metadata. No in-place migrations, destructive cleanup, or silent format changes.
- [Design prototype and mockups](../../design/cn616a-gui/): retain as synthetic design references, not the production entry point.

Default startup is disconnected/read-only; `--offline-replay` creates a clearly
labelled temporary repository-data session with no production-endpoint fallback.
Use explicit separate service mode for the user's eventual hardware testing.
The assistant never starts or attaches to the production service, probes its
endpoint, or runs service-starting batch scripts. Keep the Tk launcher unchanged.

## 6. Feature-parity checklist

All unchecked items are implementation obligations, not claims that they already work in Qt. Full Release 1 requires each applicable essential row, or an explicit documented restriction tied to an existing unsupported operation.

| Done | Current Tk surface / behavior | Release 1 destination and acceptance |
|---|---|---|
| [ ] | Telemetry: PV, absolute/autotune SP, output, loop/sensor/alarm status, available PID/analysis/equilibrium values, names, timestamp/age | Overview keyed table + selected-zone detail. Preserve available fields/units and unknown/error state; no synthetic status or zero fill. |
| [ ] | Zone updates: SP, control method, autotune SP | Overview focused controls; exact ordered legacy operations and partial results, finite validation, pending state, edit preservation. |
| [ ] | Autotune start/stop and current state | Overview; explicit start confirmation, correct target, no ambiguous retry. |
| [ ] | System and zone configuration/alarm/scaling readbacks | Configuration; all currently displayed information remains reachable, even via compact detail views. PID readback stays read-only where writes are unsupported. |
| [ ] | Poll rates, GUI refresh, equilibrium window/MAE threshold, flush setting | Configuration; same service patch keys and refresh effects. No invented slope criterion or per-zone equilibrium setting. |
| [ ] | Connection port/serial parameters/TCP endpoint; connect/disconnect/refresh | Configuration; retain current patch/connection sequence, report actual outcome, do not imply a stored serial setting was applied when it was not. |
| [ ] | Zone names and auto/list polling selection | Configuration; same zone normalization and service semantics. “Monitored zones” is not hardware Enable/Disable. |
| [ ] | Rotation limits, telemetry/analysis retention, size estimates | Configuration; same settings and background estimates; no UI-thread log scan. |
| [ ] | Viewer history duration, widths/colors, SP/autotune-SP/MAE visibility | Configuration + charts; preserve nested/legacy viewer settings compatibility and apply changes without rewriting history. |
| [ ] | Run directory/name, browse, start/stop, elapsed/last-data status | Overview Recording card, also summarized in Logs; same `start_run`, `stop_run`, `get_run_status`, JSONL streams, and run metadata. |
| [ ] | Scheduled stop and service restart recovery | Recording controls; Pacific-time entry converted as today, `set_run_schedule`, recovered/interruption presentation, no duplicate start after restart. |
| [ ] | Chart history and live updates for each monitored zone | Overview embedded chart; select any zone, PV/SP/autotune-SP/MAE when available, current metrics, time window, zoom/pan/Home and clear-view action. Clearing affects GUI view only, not service history. |
| [ ] | Optional floating chart (new requirement) | Shared plot widget/cache, independent window lifecycle and focus, no duplicate I/O; at least one working floating window, not arbitrary dock management. |
| [ ] | Ramp/Soak mode, segment count, 20 SP/slope/time rows, read/reload/save | Ramp/Soak; current validation/units and explicit unsaved edits preserved; on-demand read works with cadence zero. |
| [ ] | Selected/all-zone profile start/stop and live progress | Ramp/Soak; exact service target lists, confirmation for transitions, truthful available segment/state readbacks. No invented pause/resume API. |
| [ ] | Existing diagnostics and source-file visibility; dedicated Logs tab is new | Logs; bounded service/GUI diagnostics and relevant JSONL category tails with paths, timestamps, errors and recording status. Rotation does not freeze the window. |
| [ ] | Logging, offline behavior, arguments, and safe lifecycle | Equivalent logs-dir/refresh/debug options, persistent Qt error reporting, bounded workers, no replay on reconnect, Tk/service rollback remains usable. |

## 7. Critical dependencies, tests, and acceptance

### Dependencies and risks

- **Current service compatibility is the critical path.** Freeze v1 wire and storage shapes. Test request payloads against existing service handlers, not just a permissive fake. Endpoint changes, stale snapshots, disabled profile polling, and acknowledgments without independent readback are known integration edges.
- **Do not “fix” ignored unit selection with GUI-side serial routing.** Bind the Qt adapter to the one actual service target; multi-controller routing needs Release 2.
- **Qt environment:** use the repository's Python 3.12 runtime initially. New dependencies are permitted; keep them optional so a Qt installation problem cannot prevent Tk/service startup. Verify Windows wheel availability and Qt platform-plugin startup early.
- **Shared helpers:** preserve callback thread ownership and copy/queue bounds. Cached reads can still incur expensive copying; measure the Qt tick and move large transforms off-thread if needed.
- **Plots/logs:** cap display history and text rows; retain gaps rather than drawing across outages. Reader caps and JSONL rotation behavior must not be bypassed by floating windows. GUI plot downsampling must never change recording cadence or files.
- **Recording safety:** service remains authoritative for active state, schedule, disk errors, and recovery. Starting a recording is not thermal Run. Do not stop or restart capture just because the GUI reconnects.
- **Hardware availability:** there is no hardware connection during development.
  The assistant executes only offline tests with patched controllers/owned fake
  endpoints. The user alone tests actual device behavior and UI feel; pending
  hardware acceptance does not block implementation of existing controls.
- **Distribution:** track PySide6's applicable LGPL/GPL/commercial obligations and bundled dependency notices before redistributing an installer. Local branch implementation is not blocked on a packaging project.

### Validation commands and evidence

Use the existing unittest runner. During each milestone, run only its Qt test files plus the shared-helper/service tests affected by that slice. At handoff, combine applicable selectors in one discovery run; no new test runner is required.

Baseline and regression sources:

- [Service-client tests](../../tests/test_gui_service_client.py), [snapshot tests](../../tests/test_gui_snapshot_reader.py), [history tests](../../tests/test_gui_history_reader.py), [state-value tests](../../tests/test_gui_state_values.py), [retention tests](../../tests/test_gui_retention_estimator.py).
- [Recording tests](../../tests/test_gui_run_panel.py), [GUI/state tests](../../tests/test_gui_and_state_reader.py), [tab-refresh tests](../../tests/test_gui_tab_refresh.py), [service command tests](../../tests/test_service_commands.py), [service scheduling tests](../../tests/test_service_scheduling.py).
- Reuse synthetic corpus/fake-endpoint concepts from [responsiveness baseline](../../tests/gui_responsiveness_baseline.py) and [fixtures](../../tests/fixtures/cn616a-baseline-v1/); Tk instrumentation itself is not a Qt benchmark.

Planned setup and focused commands, from the repository root after implementation:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-qt.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_gui_service_client.py -v
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_qt_gui_*.py" -v
Remove-Item Env:\QT_QPA_PLATFORM
.\.venv\Scripts\python.exe -m py.qt_gui --offline-replay
```

These are planned commands, not validation already performed by this document revision.

### Measurable Release 1 acceptance targets

- On the target Windows machine, a 100 ms GUI heartbeat has p95 delay <=50 ms, p99 <=200 ms, and no stall >500 ms over a 10-minute six-zone exercise with repository-derived replay history, tab changes, edits, and one floating chart. Record environment, measurements, and any failure; do not claim responsiveness from startup alone.
- Repeat with an injected 2-second service delay, post-send disconnect, missing/malformed snapshot, and history rotation. Interaction remains responsive, errors are explicit, and no write is automatically replayed.
- New already-ingested telemetry reaches a visible chart within 1 second at the configured display cadence; controller sampling and socket delays are measured separately. No promise of a faster serial poll rate.
- Preserve existing history caps (100,000 raw / 20,000 rendered points per zone in the current reader); plot no more than 20,000 points per series and retain missing-data breaks. Cap log presentation at 5,000 rows and verify caps after overflow. Floating charts do not multiply ingestion workers.
- During the same exercise, compare normalized Qt/Tk readings from identical snapshots and assert exact request payloads for all migrated controls. Show absence of each unavailable field, not a fabricated value.
- Start/stop/schedule recording in temporary directories, verify existing JSONL/metadata shapes, and restart/reconnect the GUI while recording remains service-owned. Test service recovery with the existing fake-driver recovery facilities.
- Offscreen tests cover startup without service, queued callback ownership, pending-state cleanup, window close, and bounded shutdown. Interactive Windows acceptance additionally checks keyboard focus, readable six-zone layout, chart gestures, and floating-window close/reopen.
- User-only later hardware acceptance: first compare read-only values with Tk,
  then let the user choose safe setpoint/config/autotune/profile/recording checks.
  The assistant documents but does not execute these tests. Mark them NOT RUN
  until the user supplies results; fake tests are not hardware certification.
- Offline rollback: launch Tk directly against temporary replay inputs, never
  through the service-starting launcher. Later the user can verify Tk against
  their actual service/logs. No schema reversal or driver reinstall should be needed.

## 8. Minimum viable stopping point if time runs out

**Target the whole Release 1 tonight; cut polish before parity.** M2 is the minimum useful deliverable, not a four-tab facade declared finished:

- A launchable offline-replay Qt app with implemented current-service adapter,
  four tabs, stable target identities, telemetry/status/age/error display,
  embedded chart and one optional floating window. Real-service use remains untested.
- Functional SP/method/autotune-SP update path with exact v1 payloads, validation, pending/error/unknown feedback, and no retries.
- Recording start/stop/current status using the existing service, without changing its formats or stopping an existing run on GUI exit.
- Passing focused fake-service/offscreen tests for these functions and demonstrated responsive operation under delayed service responses.
- Unfinished configuration, autotune trigger, schedule/recovery UI, Logs enhancements, or profile controls are explicitly marked incomplete/unavailable with instructions to use Tk. No misleading enabled placeholders.

Do not advertise this fallback as full parity. Retain Tk as the operational application for omitted controls, and avoid sending commands from both GUIs simultaneously. If even M2's command safety tests fail, hand off a clearly read-only Qt viewer and keep Tk as the sole control GUI. Preserve all working code/tests and list exact remaining checklist items; do not spend the last hour replacing launch defaults or making an installer.

## 9. Release 2: clearly deferred multi-controller expansion

None of the following is required or to be implemented tonight:

1. **RS485 scheduling:** one serialized bus owner, per-controller polling budgets/fairness, offline-slave isolation, bounded retries/timeouts, measured 1/2/3-controller capacity and command latency. No concurrent serial access per slave.
2. **Service API v2:** explicit bus/controller/local-zone routing, version negotiation, per-target health/freshness/errors, command correlation/partial outcomes, documented acknowledgment/readback semantics, and cancellation only before dispatch. Preserve v1 consumers during migration.
3. **Controller registry and discovery:** stable identities, addresses and display names, duplicate-configuration rejection, read-only discovery, commissioning/conflict reporting, no automatic address reassignment. Discovery is not necessary to operate today's configured controller.
4. **Multi-unit recording:** identity-bearing records, target scope, outage/missing-sample representation, rotation/retention sizing, recovery, and legacy single-unit mapping. No destructive rewrite of old logs; only map v1 records when their origin is unambiguous.
5. **New hardware controls:** zone Enable/Disable, controller Run/Idle, gain writes, address writes, additional alarm/output controls, new batch operations or profile capabilities absent from the current API. Require per-operation service support, target validation, appropriate confirmation/readback and supervised hardware semantics checks. Never substitute a magic/zero setpoint for disable.
6. **Expanded UX and qualification:** actual multi-controller filters/health/group actions, 12/18+ zone tuning, multi-unit performance/soak tests, richer exports/layout persistence and deployment packaging as justified.

Release 1 preserves compatibility through keyed models and an adapter seam, not through speculative v2 wire formats, dormant schedulers, or fake enabled controls.

## 10. Decisions genuinely requiring user input

**No new design decision blocks starting the Release 1 sequence.** The requested four tabs, toolkit, branch permissions, one-controller scope, rollback requirement, and deferred capabilities are settled.

- During development, repository-derived fixtures supply mock paths/identity;
  no actual service, live data or hardware is required. Later the user confirms
  their real service/log path and target identity before enabling real writes.
- All hardware tests, including setpoints, autotune, profiles and connection
  actions, are user-only. The assistant prepares instructions and leaves their
  status pending; it does not connect or execute them.
- If the fallback stopping point is reached, disclose exactly what is missing before treating Qt as the primary control application. Keep the separate Qt launcher and Tk default unless the user later requests a default cutover.

Default implementation choices: explicit offline replay or disconnected startup,
separate opt-in service mode for later user testing, optional Qt dependencies,
shared floating charts, no new hardware APIs, no recording-format migration,
and no installer. Follow the numbered prompts one at a time, not automatically.
