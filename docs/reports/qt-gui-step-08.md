# Qt GUI step 08: Ramp/Soak editor

- Date: 2026-11-01
- Automated development gate: PASS — focused editor and affected regressions
- Offline user UI gate: PENDING
- Hardware validation: NOT RUN — USER ONLY

## Delivered
- Added `RampSoakPage` and `ProfileTableModel` with exactly 20 keyboard-editable
  rows for setpoint (°C), slope (°C/min), and time (h). The selected zone is
  named from service settings; cached controller identity is bound to the zone
  target for writes.
- Added readbacks for active segment count (0–20), control mode, current
  segment/state, loop status, profile source timestamp, age, stale state, and
  missing/error state. Standard, Stop-at-end, and Hold-at-end map to the
  existing v1 mode strings.
- Entering the tab and pressing Reload issue asynchronous `read_rampsoak`
  requests with a shared coalescing key. Dirty profile/count/mode edits survive
  snapshots. Zone changes and explicit reload ask before discarding drafts;
  Discard restores the currently cached readback.
- Added explicit Apply mode, Apply count, and Save segment changes actions using
  the exact v1 `set_control_mode`, `set_num_segments`, and
  `set_rampsoak_segments` operations. Empty rows are omitted; blank fields in a
  populated row serialize as zero, matching the existing Tk editor. Numeric
  values must be finite and representable by the driver's float32 register
  encoding. The existing 1–20 profile index and 0–20 count limits are retained;
  no thermal limits were invented.
- Acknowledgments remain distinct from refreshed readbacks. Failure and unknown
  outcomes leave drafts intact. Start/stop controls remain unavailable for
  Step 09.
- Offline profile reads and writes update only temporary synthetic snapshots
  and display simulated outcomes. No service, serial port, endpoint, or
  hardware was contacted.
- Changed the un-maximized main-window default to 1500 x 1400 px. Floating
  chart windows remain 1000 x 560 px. Updated the current Qt README description.

## Decisions and compatibility
- No service, v1 API, controller driver, or Tk behavior was changed. The Qt
  page uses the existing adapter target identity and rejects writes when the
  service is not writable or identity is unknown.
- Read requests do not depend on periodic Ramp/Soak polling being enabled.
  Reloading while dirty requires explicit discard confirmation; tab entry
  refreshes the cache without overwriting local edits.
- The requested Step 07 offline walkthrough remains explicitly deferred by the
  user's instruction to continue. Step 08's own offline walkthrough remains
  pending; no UI acceptance is claimed.

## Automated evidence
| Command/test | Result/count | What it proves / does not prove |
|---|---:|---|
| `python -m unittest -q tests.test_qt_gui_ramp_soak` with `QT_QPA_PLATFORM=offscreen` | PASS, 9 | 20 rows, count boundaries and mode mapping, exact payload serialization including zero and segment 20, invalid/non-finite/float32-overflow rejection, dirty snapshots and zone changes, reload confirmation, outcome handling, patched service-handler dispatch, and offline fake readback. |
| `python -m unittest -q tests.test_qt_gui_foundation` with `QT_QPA_PLATFORM=offscreen` | PASS, 14 | Shown default main window measures 1500 x 1400; tab order and offline isolation remain intact. |
| `python -m unittest -q tests.test_qt_gui_configuration` with `QT_QPA_PLATFORM=offscreen` | PASS, 9 | Configuration parity remains intact. |
| `python -m unittest -q tests.test_qt_gui_overview` with `QT_QPA_PLATFORM=offscreen` | PASS, 13 | Overview regressions remain intact; its control assertions are scoped to the Overview page now that other tabs have editors. |
| `python -m unittest -q tests.test_qt_gui_service_adapter` with `QT_QPA_PLATFORM=offscreen` | PASS, 29 | Adapter authorization, identities, requests, outcomes, and recording integration. |
| `python -m unittest -q tests.test_qt_gui_charts` with `QT_QPA_PLATFORM=offscreen` | PASS, 10 | Floating-chart dimensions and pinned-zone behavior remain unchanged. |
| `python -m unittest -q tests.test_qt_gui_recording` with `QT_QPA_PLATFORM=offscreen` | PASS, 4 | Recording regressions remain intact. |
| `python -m unittest -q tests.test_service_commands` | PASS, 21 | Existing fake-controller service handlers and v1 profile dispatch. |
| `python -m compileall -q py\qt_gui tests\test_qt_gui_ramp_soak.py`; `git diff --check` | PASS | Python syntax and whitespace checks. |

An initial combined multi-module Qt run hit two service-adapter test errors while
the installed pyqtgraph `LabelItem.sizeHint` override raised `_sizeHint`
`AttributeError`s. The affected suites were rerun in isolated processes and all
passed, including all 29 adapter tests; no Ramp/Soak assertions failed in that
combined run, and the warning is emitted from the installed pyqtgraph override.
One Overview assertion that searched the entire
window for `QSpinBox` was scoped to Overview, as Ramp/Soak now legitimately
contains a segment-count spin box.

## User walkthrough — offline
1. From PowerShell at the repository root, run
   `.\bat\gui_qt_start.bat --offline-replay`. Confirm the offline/no-hardware
   banner and the un-maximized 1500 x 1400 px main window.
2. Select **Ramp/Soak**. Confirm the request is simulated, the six-zone
   selection and 20 rows appear, and the sample source age/stale status and
   current-segment fields are visible.
3. Use keyboard navigation to edit a few rows. Enter `0` in one cell and leave
   another blank. Confirm count accepts 0 and 20, while values such as `nan`,
   `inf`, or a value outside the driver's float32 range are rejected without a
   request.
4. With edits pending, switch zones. Choose **No** to preserve the draft, then
   retry and choose **Yes** to discard it. Test **Reload from service** with
   dirty edits and confirm it asks before replacing them.
5. Save a profile, apply a count/mode, then reload. Confirm the temporary fake
   snapshot reflects the saved data and all outcomes/readbacks remain marked
   simulated. No profile start/stop controls should be present.
6. Confirm **Overview** remains at its existing behavior and floating chart
   windows still open at 1000 x 560 px.

## User-only later hardware checks
**USER-ONLY HARDWARE / NOT RUN.** Before any live profile write, the operator
must independently confirm the active service endpoint, controller identity,
selected zone, and serial ownership. Verify fresh service readback after each
write. No real profile write, execution, or thermal behavior was tested here;
do not infer hardware acceptance from the simulated editor.

## Feedback and disposition
The user explicitly requested proceeding despite Step 07's pending offline UI
walkthrough and requested the 1500 x 1400 main-window default. The Step 07
walkthrough is therefore deferred, not observed. Step 08 offline UI feedback is
pending.

## Risks, rollback and next handoff
- Keep the Tk GUI as the operational fallback. Qt profile read/write controls
  passed offline and fake-handler tests; live-controller behavior remains
  user-only.
- Next prompt: `cn616a-qt-09-profile-autotune.prompt.md`. Stop here; do not begin
  Step 09 before user review or explicit deferral of this step's offline
  walkthrough.
