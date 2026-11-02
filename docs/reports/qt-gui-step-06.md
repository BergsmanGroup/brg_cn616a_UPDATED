# Qt GUI step 06: recording and scheduled stops

- Date: 2026-11-01
- Automated development gate: PASS — Step 06 focused suites
- Offline user UI gate: DEFERRED BY USER (proceeded to Step 07)
- Hardware validation: NOT RUN — USER ONLY

## Delivered
- Added a Recording / data collection card to Overview with directory picker and
  manual path, optional name, explicit start/stop, active elapsed time, last-data
  timestamp, scheduled-stop status, service outcome, and recovered-interruption
  display. The card explicitly says collection does not start/stop heating or
  profile execution.
- Start/stop/schedule state is never inferred from a button click or service
  acknowledgment. The UI polls `get_run_status` continuously, including while
  another tab is selected, and only enables Start after an authoritative
  inactive response. Unknown results do not retry. Closing the GUI does not
  send `stop_run`.
- Added Pacific-time schedule entry with the existing Tk conversion behavior
  extracted into `py/gui/run_schedule.py`. The exact v1 schedule request uses
  `set_run_schedule`, `enabled`, and offset-bearing `stop_at_iso`.
- Explicit service-level recording commands are authorized without a zone
  target only when the normal writable-service readiness checks pass.
  `--read-only` continues to reject all recording and schedule mutations.
- Offline replay models start/stop/schedule and writes a synthetic JSONL sample
  and run metadata only below its temporary session directory. The typed path
  is never written in replay. A clearly labeled action demonstrates recovered
  interruption; closing replay removes all temporary data.
- No production service, serial port, controller, or repository `logs`/`runs`
  path was accessed. The service handler itself was not changed.

## Decisions and compatibility
- Preserved `start_run`, `stop_run`, `get_run_status`, and `set_run_schedule`
  operation names and payload formats. Recording commands are service-level,
  not zone-bound; the adapter still requires a connected service, controller,
  fresh telemetry, and verified identity for live mutations.
- GUI restart/reconnect reads the active service state; it does not automatically
  start or stop a recording. The service remains authoritative while the GUI is
  hidden or closed.
- The replay accepts a user-entered directory only to exercise the form. It
  creates its simulated files in a private temporary location and displays that
  location, preventing offline tests from writing into user or repository data.

## Automated evidence
| Command/test | Result/count | What it proves / does not prove |
|---|---:|---|
| `QT_QPA_PLATFORM=offscreen; .\.venv\Scripts\python.exe -m unittest -q tests.test_qt_gui_recording` | PASS, 4 | Pacific DST conversion, directory dialog/manual fallback, non-optimistic status, payload shape, temporary replay files, recovery, and stop metadata. |
| `QT_QPA_PLATFORM=offscreen; .\.venv\Scripts\python.exe -m unittest -q tests.test_qt_gui_service_adapter` | PASS, 27 | Fake endpoint checks for exact service-level requests, read-only rejection, delayed/unknown outcomes, status coalescing, hidden-tab updates, and GUI reopen without duplicate start/stop. |
| `QT_QPA_PLATFORM=offscreen; .\.venv\Scripts\python.exe -m unittest -q tests.test_gui_run_panel tests.test_gui_history_reader tests.test_service_commands` | PASS, 34 | Existing Tk behavior, Pacific-time conversion, and patched-fake-controller contracts including JSONL/run metadata shapes, rejected paths, schedule validation, and recovery. |
| `QT_QPA_PLATFORM=offscreen; .\.venv\Scripts\python.exe -m unittest -q tests.test_qt_gui_overview tests.test_qt_gui_foundation` | PASS, 27 | Existing Overview controls, mode isolation, and Qt startup regressions. |
| Compileall, `python -m py.qt_gui --help`, `git diff --check` | PASS | Python syntax, safe CLI help, and whitespace. |
| `QT_QPA_PLATFORM=offscreen; .\.venv\Scripts\python.exe -m unittest -q tests.test_qt_gui_charts` | PASS, 10 | Floating chart remains pinned to its opened zone while Overview focus and shared history update. Existing pyqtgraph `LabelItem.sizeHint` warnings are printed, but the suite completes successfully. |

Expected log output in the fake-controller tests comes from deliberately
injected errors and picker failure cases. Tests use temporary directories and
test-owned ephemeral loopback endpoints; no hardware semantics are inferred.

## User walkthrough — offline
1. From PowerShell at the repository root, run
   `.\bat\gui_qt_start.bat --offline-replay`.
2. On Overview, confirm the **SIMULATED SOURCE** note and the statement that
   recording does not control heating or profile execution. Use Browse or enter
   a directory manually, enter an optional name, and start recording. Confirm
   the card reports active only after replay status arrives and shows the
   temporary output path.
3. Enter a future Pacific time as `YYYY-MM-DD HH:MM[:SS]`, choose **Set
   scheduled stop**, and confirm the displayed schedule. Try an invalid entry
   and confirm no request is sent.
4. Choose **Simulate recovered interruption** and confirm the recovered
   interruption message is labeled simulated. Stop the recording; confirm it
   returns to inactive. Close the GUI and verify replay temporary files are
   cleaned up.

## User-only later hardware checks
**USER-ONLY HARDWARE / NOT RUN.** Against an independently verified existing
service endpoint, the operator should confirm controller identity, COM-port
ownership, destination directory, recording name, and the intended Pacific
schedule before starting collection. Confirm data files and status through the
service, then verify scheduled/manual stop behavior under the operator's
process-specific procedure. Stop on unknown service outcomes, stale status,
unexpected destination, or identity mismatch. Recording must not be interpreted
as starting or stopping heating.

## Feedback and disposition
The user explicitly requested proceeding with Step 07. This defers Step 06's
offline UI walkthrough, not as a walkthrough result. The earlier Step 05
walkthrough remains deferred. No user hardware result is claimed.

## Risks, rollback and next handoff
- The chart suite emits existing pyqtgraph `LabelItem.sizeHint` warnings; its
  current focused run passes. No hardware validation is implied.
- Keep the Tk GUI as the operational fallback. The next prompt is
  `cn616a-qt-07-configuration.prompt.md`; do not begin it until this report's
  automated gate passes and the user accepts, incorporates, or explicitly
  defers the offline review. Stop before Step 07.
