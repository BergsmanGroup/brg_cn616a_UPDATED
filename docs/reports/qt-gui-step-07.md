# Qt GUI step 07: configuration parity

- Date: 2026-11-01
- Automated development gate: PASS — focused Step 07 and affected regressions
- Offline user UI gate: PENDING
- Hardware validation: NOT RUN — USER ONLY

## Delivered
- Replaced the Configuration placeholder with cached system/zone readbacks,
  alarm and scaling fields, available per-zone PID gains, and service settings.
  Settings are grouped in a scrollable form so the main window remains usable
  at its 750 x 700 px un-maximized default.
- Added service polling rates, GUI refresh rate, equilibrium window/threshold,
  log flush and rotation, duration-based retention and estimates, zone monitor
  mode/list, zone names, chart preferences, and stored serial/TCP settings.
  Chart preferences update the Qt charts from the service-config snapshot.
- Added explicit Apply, connect, disconnect, controller-readback refresh, and
  connection-refresh actions through the adapter. Connection sequences preserve
  the v1 `set_service_config`, `connect_serial`, `disconnect_serial`,
  `refresh_connection`, and `read_config` operations.
- Reused the bounded background retention estimator. Moved duration parsing to
  `py/gui/config_values.py` so existing Tk and the Qt form share the same
  parser.
- Dirty edits and their revision are retained across snapshot updates and
  delayed acknowledgments. Invalid numeric, duration, and zone-list input is
  surfaced without submitting a patch. Zone names and monitor lists do not add
  hardware enable/disable controls.
- The actual adapter endpoint remains fixed for the GUI session. Stored TCP
  host/port values are displayed separately; saving them does not switch the
  client or claim listener rebinding. Stored settings acknowledgments do not
  establish serial reconnection.
- Offline connection actions only generate simulated outcomes and update the
  temporary replay's in-memory service settings. They do not contact a port,
  endpoint, or listener. Floating chart windows remain 1000 x 560 px.

## Decisions and compatibility
- Preserved the existing Tk configuration patch keys and enum values, including
  nested `viewer` settings and legacy flat `viewer_*` aliases. The existing
  service settings model and handlers were not changed.
- Service-level settings and connection commands can be submitted to a
  reachable, explicitly writable service even if its controller is currently
  disconnected. Explicit `--read-only` mode rejects those mutations in the
  adapter; offline replay labels the same actions simulated.
- Connect and connection refresh first submit the stored connection settings
  and then the existing service operation. After a definitive outcome, the Qt
  client refreshes service status on its original endpoint. It never probes a
  host/port typed into the form.
- PID gains remain read-only telemetry readbacks. No controller activation,
  zone enable, or unsupported hardware controls were added.
- The requested Step 06 offline walkthrough remains deferred by the user's
  explicit instruction to proceed with Step 07. No Step 06 or Step 07 UI
  walkthrough result is claimed.

## Automated evidence
| Command/test | Result/count | What it proves / does not prove |
|---|---:|---|
| `$env:QT_QPA_PLATFORM='offscreen'; .\.venv\Scripts\python.exe -m unittest -q tests.test_qt_gui_configuration` | PASS, 9 | Patch shape and viewer aliases, invalid input, dirty/snapshot/delayed-ack behavior, readbacks, connection partial failure, background retention estimates, simulated replay settings, chart preference effects, and shared duration parsing. |
| `$env:QT_QPA_PLATFORM='offscreen'; .\.venv\Scripts\python.exe -m unittest -q tests.test_qt_gui_service_adapter` | PASS, 29 | Service-level command authorization, explicit read-only rejection, fake-endpoint connection failure without endpoint switching, and existing adapter/recording outcomes. Tests use an owned loopback endpoint. |
| `$env:QT_QPA_PLATFORM='offscreen'; .\.venv\Scripts\python.exe -m unittest -q tests.test_qt_gui_foundation tests.test_qt_gui_overview` | PASS, 27 | 750 x 700 shown main window, refreshed service snapshots, tab/selection behavior, and overview regressions. |
| `$env:QT_QPA_PLATFORM='offscreen'; .\.venv\Scripts\python.exe -m unittest -q tests.test_qt_gui_charts` | PASS, 10 | Floating charts retain their pinned zone and remain 1000 x 560 px; existing chart/history regressions. The installed pyqtgraph emits `LabelItem.sizeHint` warnings, but tests complete successfully. |
| `$env:QT_QPA_PLATFORM='offscreen'; .\.venv\Scripts\python.exe -m unittest -q tests.test_qt_gui_recording` | PASS, 4 | Recording and schedule UI remain intact after Configuration integration. |
| `.\.venv\Scripts\python.exe -m unittest -q tests.test_gui_retention_estimator tests.test_service_utils tests.test_service_commands` | PASS, 35 | Existing bounded estimator, shared Tk duration parser, and fake-controller service command contracts. |
| Compileall, Pylance file syntax checks/diagnostics, Qt CLI `--help`, and `git diff --check` | PASS | Changed Python syntax and workspace diagnostics, safe CLI help, and whitespace. |

One initial combined Qt run hit a timing-sensitive existing recording
schedule assertion; it passed in the full isolated adapter suite (29 tests) and
the Step 06 recording suite (4 tests). A pre-existing Overview assertion that
counted every `QLineEdit` in the whole window was narrowed to the Overview page
after Configuration added its own settings fields. No live service, endpoint
other than test-owned loopback, serial port, or hardware was accessed.

## User walkthrough — offline
1. From PowerShell at the repository root, run
   `.\bat\gui_qt_start.bat --offline-replay`. Confirm the offline/no-hardware
   banner and the un-maximized window opens at 750 x 700 px.
2. Open **Configuration → Readbacks**. Review the system and zone tabs,
   including alarms, scaling, and any available PID gains. These are replay
   data, not physical instrument readbacks.
3. Open **Service settings**, change a chart preference, and choose **Apply
   settings**. Confirm the status is marked simulated and the Overview chart
   reflects the viewer preference. Try invalid input and confirm it is rejected
   without a simulated request. Edit a value and confirm snapshot refresh does
   not overwrite the draft.
4. Try **Connect**, **Disconnect**, and **Refresh connection** in replay.
   Confirm each remains simulated and the displayed actual endpoint does not
   change to the stored host/port values. The monitor list is a polling
   selection, not hardware activation.
5. Inspect the retention estimate and confirm it is reported asynchronously.
   Close the GUI; replay-only settings are temporary and do not persist.

## User-only later hardware checks
**USER-ONLY HARDWARE / NOT RUN.** The operator may later verify stored settings
against an independently confirmed existing service. Confirm the active service
endpoint separately from stored TCP settings; confirm controller identity and
serial ownership before requesting connect/disconnect/refresh. Verify service
status and fresh snapshots after every transition. A successful settings patch
does not prove listener rebinding or serial reconnection.

## Feedback and disposition
The user requested the main window default to 750 x 700 px and explicitly
requested proceeding with Step 07. The Step 06 offline UI walkthrough is
therefore deferred, not accepted or observed. Step 07's offline walkthrough
remains pending. Hardware validation remains user-only.

## Risks, rollback and next handoff
- Configuration and service-level connection controls are implemented and
  covered by offline/fake-endpoint tests; the user walkthrough and hardware
  transitions remain unverified.
- Keep the Tk GUI as the operational fallback. The next prompt is
  `cn616a-qt-08-profile-editor.prompt.md`; do not begin it until the automated
  gate passes and this step's offline review is accepted, incorporated, or
  explicitly deferred. Stop before Step 08.
