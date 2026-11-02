# Qt GUI step 10: logs and diagnostics

- Date: 2026-11-01
- Automated development gate: PASS — focused log-reader and Qt integration suites
- Offline user UI gate: PENDING
- Hardware validation: NOT RUN — USER ONLY

## Delivered
- Replaced the Logs placeholder with category and source-path selection for
  service diagnostics, telemetry/configuration/Ramp-Soak/analysis/service
  configuration JSONL, recording JSONL, adapter activity, recording summary,
  and uncaught GUI/thread exceptions.
- Added a background reader with bounded initial tail (512 KiB), per-refresh
  reads (128 KiB), record length (64 KiB), rendered text (2,000 characters),
  retained view (5,000 rows), and exception history (100 entries). Reads are
  incremental; source replacement/rotation and truncation reset the tail with
  visible notices. Missing/denied paths, malformed JSONL, oversized records,
  and source-directory errors are visible.
- Connected service status and command outcomes plus the Overview recording
  status signal to Logs independently of the selected tab. Added a persistent
  **View Logs** navigation button that opens service activity.
- Offline sources are labeled **OFFLINE REPLAY / NO HARDWARE** and point to
  temporary generated files. No telemetry is fabricated from diagnostics.
  Logs offers no delete, purge, repair, search, or export actions.

## Decisions and compatibility
- Reads use a dedicated daemon thread and a fixed set of service log names and
  rotation patterns. Active and rotated sources are selectable; each refresh
  reads only the selected source's appended bytes. Service status/outcomes,
  recording summary, and GUI exceptions are bounded in-memory views.
- The application exception hooks preserve the previous `sys.excepthook` and
  `threading.excepthook` reporting behavior while retaining a bounded local
  session history. Recording summary remains service-authoritative and
  explicitly separate from controller Run/Idle.
- No service protocol, Tk behavior, hardware driver, or data-file format was
  changed. User request **"proceed with step 10"** explicitly waives Step 09's
  pending offline UI review; it does not change that gate to accepted. Step 10's
  own offline UI review is pending.

## Automated evidence

All Qt suites ran independently with `QT_QPA_PLATFORM=offscreen`.

| Command/test | Result/count | What it proves / does not prove |
|---|---:|---|
| `python -m unittest -q tests.test_qt_gui_logs` | PASS, 9 | Incremental append/partial rows, rotation/replacement, truncation, missing/denied paths, malformed/oversized rows, 5,000-row cap, category/source changes, hidden-tab replay updates, off-thread reads, exception-hook chaining, and shutdown during a blocked read. |
| `python -m unittest -q tests.test_qt_gui_foundation` | PASS, 14 | Shell tabs, launch and offline isolation remain intact. |
| `python -m unittest -q tests.test_qt_gui_overview` | PASS, 13 | Overview and run-status integration regressions. |
| `python -m unittest -q tests.test_qt_gui_configuration` | PASS, 9 | Configuration integration remains intact. |
| `python -m unittest -q tests.test_qt_gui_recording` | PASS, 4 | Recording status integration and command behavior. |
| `python -m unittest -q tests.test_qt_gui_charts` | PASS, 10 | Chart/window regressions. |
| `python -m unittest -q tests.test_qt_gui_ramp_soak` | PASS, 12 | Ramp/Soak actions and status integration. |
| `python -m unittest -q tests.test_qt_gui_service_adapter` | PASS, 30 | Adapter/window integration against test-owned fake endpoints. |
| `python -m compileall -q py\qt_gui tests\test_qt_gui_logs.py`; `git diff --check` | PASS | Syntax and whitespace checks. |

Total: 101 tests passed. The service-adapter suite printed expected fake-endpoint
timeouts/shutdown diagnostics; it completed successfully. No production service,
serial port, or instrument was accessed.

## User walkthrough — offline
1. From PowerShell at the repository root, run
   `.\bat\gui_qt_start.bat --offline-replay`; confirm the offline/no-hardware
   label and temporary source directory.
2. Leave **Overview** selected and observe its simulated recording status.
   Then open Logs > **Recording summary** and confirm the same status is shown
   without needing a tab-triggered refresh. Use **View Logs** and verify
   service activity displays the current source/state.
3. Switch through telemetry, service configuration, and service diagnostics.
   Check that each source path is explicit, timestamps are visible where
   available, and missing sources or malformed/oversized rows are reported
   rather than mistaken for telemetry. The automated tests exercise malformed
   and oversized temporary records.
4. Select **GUI exceptions** and **Recording JSONL**. Confirm exception rows
   are session-local and recording data remains distinct from heating/profile
   state. Do not select a live service or edit hardware-related files.

## User-only later hardware checks
**USER-ONLY HARDWARE / NOT RUN.** Against an independently verified existing
service, confirm the displayed log directory and filenames before relying on
diagnostics. Compare the recording summary with the service's reported status.
Stop if source identity, read errors, or status is unexpected. Logs is
read-only; it does not repair or delete source files.

## Feedback and disposition
No Step 10 UI feedback has been received. Offline UI gate remains pending.

## Risks, rollback and next handoff
- Tail selection is bounded and intentionally not a full-history search. Large
  backlogs are skipped with a notice; oversized records are discarded with an
  explicit row. The UI is not a replacement for preserving original files.
- Keep the Tk GUI as the operational fallback. Next prompt:
  `cn616a-qt-11-reliability.prompt.md`. Stop here; Step 11 has not started.
