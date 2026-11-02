# Qt GUI step 03: read-only Overview telemetry

Date: 2026-11-01
Automated development gate: PASS
Offline user UI gate: PENDING
Hardware validation: NOT RUN — USER ONLY

## Delivered
- Added a prototype-inspired shell and sortable six-zone Overview. The table
  shows names, PV, SP, output, method/loop, MAE and equilibrium; focused
  readbacks include sensor, alarm, autotune, PID, segment and analysis. Units
  are explicit and missing fields show `N/A`.
- Source/mode, identity, timestamp, age, stale and error text identify the
  static, mock-derived replay. Split, scrollable panels support smaller windows.
- Selection sends no commands. Charts, controls, Configuration, Ramp/Soak, and
  Logs remain unfinished.

## Decisions and compatibility
- `TelemetryTableModel` keeps six stable rows, keyed by `ZoneKey` only when
  telemetry identity is known. Sorting and refreshes preserve selection;
  controller changes require reselection. Foreign data and names are withheld.
  Unknown telemetry is readable but unkeyed. Target-and-field dirty-readback
  hooks support later forms; no editors or activation actions were added.
- Generation-checked cached snapshots and an age timer update hidden tabs
  without synchronous file reads or model resets. Adapter API, service
  protocol, state files, Tk launch, and serial ownership are unchanged.
- Fixed mock analysis for the baseline's `sp_abs` alias, missing telemetry
  identity inheriting another category's identity, and old
  `timestamp_pacific` data appearing fresh due to a new file modification time.
  Overview and adapter share the cached source-age calculation.

## Automated evidence
| Command/test | Result/count | What it proves / does not prove |
|---|---:|---|
| `$env:QT_QPA_PLATFORM='offscreen'; python -m unittest -q tests.test_qt_gui_overview tests.test_qt_gui_foundation tests.test_qt_gui_service_adapter tests.test_gui_service_client tests.test_gui_snapshot_reader tests.test_gui_and_state_reader tests.test_gui_state_values tests.test_gui_history_reader` | PASS, 69 | All 16 synthetic samples × six zones, model roles/text, aliases, missing values, freshness boundary, identity, selection, dirty hooks, hidden-tab updates, and no selection commands. Fake services use ephemeral loopback endpoints; no hardware behavior. |
| Same command with `.\.venv\Scripts\python.exe` instead of `python` | PASS, 69 | Launcher Python 3.12.10 and terminal Python 3.13.14 both passed. |
| `python -m compileall -q py\qt_gui tests\test_qt_gui_overview.py tests\test_qt_gui_foundation.py tests\test_qt_gui_service_adapter.py`; `python -m py.qt_gui --help`; `git diff --check` | PASS | Compilation, safe CLI help, and whitespace checks. Pylance syntax checks and diagnostics reported no errors; unused-parameter hints remain. |
| Offscreen renders at 1100 × 720 and 800 × 600 | PASS | Inspected density, layout, scrolling and text using synthetic replay. Segoe UI was registered for offscreen rendering; screenshots were not retained. Not user acceptance. |

The three focused regressions failed before their fixes and passed afterward.
A fixed-delay responsiveness test raced during parallel suites; it now holds
the fake response until more than ten UI heartbeats and a tab switch occur.
No tests were skipped. Expected logs came from injected failures and shutdowns.

## User walkthrough — offline
1. In PowerShell at the repository root, run
   `Remove-Item Env:\QT_QPA_PLATFORM -ErrorAction SilentlyContinue`, then
   `.\bat\gui_qt_start.bat --offline-replay`. Confirm **OFFLINE REPLAY / NO
   HARDWARE**; review the six names, units, colors and density.
2. Select rows, sort columns, and check detail, `N/A` and mock analysis. Resize
   to 800 × 600; use the table and panel scrollbars. Charts and other tabs are
   incomplete.
3. After five seconds, source health should show **STALE** and increasing age,
   including after switching tabs. Close the window to clean up temporary data.
4. Inspect a synthetic error without hardware; expect **Analysis: ERROR** and
   `N/A` MAE:

   ```powershell
   @'
   from dataclasses import replace
   from PySide6.QtWidgets import QApplication
   from py.qt_gui.offline import OfflineReplaySession
   from py.qt_gui.window import QtMainWindow
   app = QApplication([])
   session = OfflineReplaySession()
   window = QtMainWindow(session)
   analysis = session.snapshots()["analysis"]
   window._on_snapshot_updated(replace(analysis, data=None, error="Simulated analysis read failure"))
   window.show()
   app.exec()
   '@ | .\.venv\Scripts\python.exe -
   ```

## User-only later hardware checks
**USER-ONLY HARDWARE / NOT RUN.** With a separately verified running service,
compare named-zone readbacks, units, source freshness, and identity with a
trusted controller display. Stop if identity or values disagree; do not infer
hardware behavior from the synthetic replay.

## Feedback and disposition
The user noted the initial GUI had only four tabs and requested an aesthetically
pleasing, functional experience. The prototype-guided Overview addresses that
concern for this step; charts, controls, and other features remain scheduled for
later prompts. The user's offline review of this step is pending.

## Risks, rollback and next handoff
This is a read-only preview, not a replacement for the operational Tk GUI.
Service/hardware integration and visual acceptance remain unverified. The next
prompt is `cn616a-qt-04-charts.prompt.md`; do not start it until this step's
offline feedback is accepted, incorporated, or explicitly deferred. The next
step should read `overview.py`, the adapter history contract, and its own
prompt; it must preserve the identity and no-synchronous-I/O safeguards.
