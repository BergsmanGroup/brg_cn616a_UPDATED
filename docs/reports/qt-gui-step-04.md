# Qt GUI step 04: embedded and floating charts

Date: 2026-11-01
Automated development gate: PASS
Offline user UI gate: DEFERRED BY USER
Hardware validation: NOT RUN — USER ONLY

## Delivered
- Added an embedded pyqtgraph trend in Overview's focused-zone card with
  independent floating chart windows. All views consume the adapter's one
  history snapshot/reader; floating windows create no sockets, history readers,
  or timers.
- PV, absolute SP, autotune SP, and MAE retain their source timestamps and units.
  Gaps break line segments without adding or deleting history. Controls include
  configurable time windows, line visibility, pan/zoom, Home, and Clear view.
  Clear view resets the viewport only. Current MAE, threshold, and sample count
  are shown when matching analysis data supplies them.
- History parsing remains in the bounded reader worker; chart-series conversion
  runs in one coalesced worker and fans out to embedded/floating views. Reader
  caps remain 100,000 raw and 20,000 rendered points per zone; plotted series
  are capped at 20,000 points.
- Offline replay now owns the same single worker-backed history contract. Its
  generated history identity matches its replay target; service protocol, Tk
  launch/default, and controller ownership are unchanged.

## Decisions and compatibility
- Charts appear in the existing Overview trend area; the embedded chart follows
  the verified Overview selection. A history/config/analysis identity mismatch
  withholds chart data or metrics. Floating views have separate plot navigation
  and visibility controls.
- Existing `viewer` settings and legacy `viewer_*` aliases are applied without
  importing Tk modules. Reader results are never modified by view controls.
- Each floating chart stays on the zone selected when it was opened; the
  embedded chart continues following the Overview selection. Floating charts
  share the history reader and update independently for their pinned zones.
- The default replay uses regular synthetic timestamps, so a visible gap is not
  guaranteed in that walkthrough. Temporary test histories include explicit
  gaps and verify that the rendered connection mask breaks at the original
  timestamps.

## Automated evidence
| Command/test | Result/count | What it proves / does not prove |
|---|---:|---|
| `$env:QT_QPA_PLATFORM='offscreen'; .\.venv\Scripts\python.exe -m unittest -q tests.test_qt_gui_charts` | PASS, 10 | Includes a regression confirming a floated zone stays selected when Overview focus changes and refreshes from new shared history. |
| `$env:QT_QPA_PLATFORM='offscreen'; .\.venv\Scripts\python.exe -m unittest -q tests.test_qt_gui_charts tests.test_gui_history_reader tests.test_qt_gui_overview tests.test_qt_gui_service_adapter tests.test_qt_gui_foundation` | PASS, 62 | Replay timestamps/values, gaps, viewer aliases, window changes, reader caps after 100,001 raw records, malformed lines, append/rotation/truncation, identity gating, float close/reopen, worker-thread transforms, coalescing, and UI heartbeat. |
| `.\.venv\Scripts\python.exe -m compileall -q py\qt_gui tests\test_qt_gui_charts.py tests\test_gui_history_reader.py`; `.\.venv\Scripts\python.exe -m py.qt_gui --help`; `git diff --check` | PASS | Compilation, safe CLI help, and whitespace checks. No hardware or production service was started. |

One timing-sensitive existing service-adapter assertion failed once when run
alone because its asynchronous `get_status` request had not yet reached the fake
endpoint; the isolated retry and final focused suite passed. No tests were
skipped. Expected diagnostic output came from fake endpoints and malformed or
missing temporary snapshots.

## User walkthrough — offline
1. In PowerShell at the repository root, run
   `Remove-Item Env:\QT_QPA_PLATFORM -ErrorAction SilentlyContinue`, then
   `.\bat\gui_qt_start.bat --offline-replay`. Confirm **OFFLINE REPLAY / NO
   HARDWARE**.
2. Select Zone 1 and float its chart. Select Zone 2 in Overview. Confirm the
   embedded chart follows Zone 2 while the floating chart remains on Zone 1,
   with both charts displaying their respective PV and available analysis data.
3. Change the Window selector, toggle SP/autotune/MAE, pan and zoom, then use
   Home to fit loaded history and Clear view to return to the selected window.
   Confirm the display resets do not erase history.
4. Open two floating charts, pan them independently, close one, reopen it, and
   close the application. No floating view should keep the application alive.
5. Inspect a gap when present. The default baseline is regular; gap breaks are
   covered by the automated temporary-history tests if no visible gap appears.
   Close the GUI to remove temporary replay files.

## User-only later hardware checks
**USER-ONLY HARDWARE / NOT RUN.** No poll-rate or controller behavior claim is
made from replay charts. Hardware validation remains for the user.

## Feedback and disposition
The user explicitly requested proceeding with Step 05 before completing the
Step 04 offline walkthrough. The UI review is therefore deferred, not accepted;
no visual or usability result is claimed.

## Risks, rollback and next handoff
Embedded and floating rendering passed offline tests; the user walkthrough and
hardware behavior remain unverified. Keep the Tk GUI as default. The next prompt
is `cn616a-qt-05-zone-controls.prompt.md`; do not start it until this step's UI
feedback is accepted, incorporated, or explicitly deferred.
