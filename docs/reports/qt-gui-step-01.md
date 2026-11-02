# Qt GUI step 01: foundation and safe offline replay
Date: 2026-11-01
Automated development gate: PASS
Offline user UI gate: PENDING
Hardware validation: NOT RUN — USER ONLY

## Delivered
- Added an optional Qt dependency file and `py.qt_gui` launch path with exactly
  Overview, Configuration, Ramp/Soak, and Logs tabs. The foundation has no
  enabled controls; Tk remains the operational GUI.
- Added `--offline-replay`, which decodes the repository's immutable synthetic
  baseline through the existing offline reference adapter and writes
  service-shaped snapshots plus telemetry/mock-analysis JSONL into a temporary
  directory. Closing the window removes that directory. The raw fixture is
  never changed.
- The window prominently identifies **OFFLINE REPLAY / NO HARDWARE** and states
  that analysis is mock-derived and outcomes are mock-only. Without the explicit
  flag, it shows **DISCONNECTED / READ-ONLY** and does not probe an endpoint.
- Added a Qt launcher that forwards arguments and never starts the service.
  README and batch usage now document the optional foundation and its limits.

## Decisions and compatibility
- `ControllerKey(bus_id, controller_id)`, `ZoneKey(bus_id, controller_id,
  zone_id)`, and `ControllerIdentity(key, modbus_address)` are immutable,
  explicit identity records. The replay has exactly one configured identity;
  the address is metadata, not a row number or routing mechanism.
- `QtAdapter` defines a status property, snapshots, outcomes, and shutdown.
  `OfflineReplaySession` implements that boundary for this step. It has no
  command backend and returns no outcomes; controls remain unavailable.
- Offline output is derived from the versioned raw Modbus baseline. It does not
  import or execute the Tk UI/benchmark runner, modify existing service schemas,
  read saved endpoint settings, start a service, or connect to a controller.
  Real-service adapter work is deferred to step 02.

## Automated evidence
| Command/test | Result/count | What it proves / does not prove |
|---|---|---|
| `python -m unittest discover -s tests -p test_qt_gui_foundation.py -v` with `QT_QPA_PLATFORM=offscreen` | PASS, 9 tests | Four tabs, mode/provenance, CLI/help and missing-dependency behavior, repository fixture hash preservation, temporary cleanup, bounded close, immutable identities, launcher forwarding, Tk/service import independence, and actual offline CLI startup with serial/socket constructors and endpoint calls guarded to fail. Does not prove live-device behavior or user acceptance. |
| `python -m compileall -q py\qt_gui tests\test_qt_gui_foundation.py` | PASS | New Python files compile. |
| `python -m py.qt_gui --help` | PASS | Help is available without a service or hardware. |
| `git diff --check` | PASS | No whitespace errors. |

Validation ran under Python 3.13.14 with PySide6 available. The first test run
found the environment lacked the repository's base `pymodbus`; installing
`requirements.txt` resolved it. No test skips remain. `pyqtgraph` is declared
but not imported by this shell because chart work is out of scope.

## User walkthrough — offline
1. From the repository root, install the optional dependencies with
   `.\.venv\Scripts\python.exe -m pip install -r requirements-qt.txt`.
2. Launch `.\bat\gui_qt_start.bat --offline-replay`. Expect the prominent
   **OFFLINE REPLAY / NO HARDWARE** and synthetic/mock provenance labels.
3. Resize the window and switch through Overview, Configuration, Ramp/Soak,
   and Logs. Expect four tabs and clear notices that controls/features are not
   migrated; no control is enabled.
4. Close the window. Expect prompt shutdown and cleanup of the temporary
   replay files. `.\bat\gui_qt_start.bat --help` should print help without
   starting a service.

## User-only later hardware checks
**USER-ONLY HARDWARE / NOT RUN.** No device check is appropriate for this
read-only foundation; no real-service mode or controller operations exist.
Do not treat synthetic values as a hardware validation result.

## Feedback and disposition
Offline user review is pending. No user observations have been received.

## Risks, rollback and next handoff
The Qt window is an explicitly incomplete shell, not a replacement for Tk.
Continue to use `.\bat\gui_start.bat` for the operational application. Next
prompt: `cn616a-qt-02-adapter.prompt.md`; do not begin it until this report's
offline UI review is accepted, incorporated, or explicitly deferred. Read
`py/qt_gui/adapter.py`, `py/qt_gui/offline.py`, and
`py/qt_gui/__main__.py` as the implementation boundary.
