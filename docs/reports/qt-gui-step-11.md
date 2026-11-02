# Qt GUI step 11: integration and reliability

- Date: 2026-11-02
- Automated software reliability gate: PASS
- Offline user UI gate: PENDING
- Hardware validation: NOT RUN — USER ONLY

## Result

Step 11 adds a reusable, opt-in ten-minute replay exercise and automated Tk
rollback coverage. No application, service protocol, Tk behavior, or hardware
code was changed. The exercise passed all measured software thresholds using
temporary files and the repository's synthetic baseline. It does not certify
interactive UI feel or live instrument behavior.

## Ten-minute responsiveness evidence

| Measure | Result | Acceptance | Status |
|---|---:|---:|---|
| Exercise duration | 600.01 s | 600 s | PASS |
| 100 ms GUI heartbeat delay, p95 | 23.29 ms | <=50 ms | PASS |
| 100 ms GUI heartbeat delay, p99 | 57.32 ms | <=200 ms | PASS |
| Maximum heartbeat delay | 308.67 ms | <=500 ms | PASS |
| Maximum observed heartbeat interval | 408.67 ms | <=500 ms | PASS |
| Telemetry displayed, samples | 299 / 299 | all samples | PASS |
| Ingest-to-chart latency, p95 | 453.33 ms | <=1,000 ms | PASS |
| Ingest-to-chart latency, maximum | 537.64 ms | <=1,000 ms | PASS |
| Samples over the 1 s display limit | 0 | 0 | PASS |
| Zones and tabs exercised | 6 zones; 4 tabs | all six; all tabs | PASS |

Measurement environment:

- Windows 11, build 10.0.26200, x64; Intel64 Family 6 Model 141.
- CPython 3.12.10 from `.venv`; PySide6 6.12.0.
- Qt platform: `offscreen`.

Method: one offline `QtMainWindow` used a 100 ms `QTimer` heartbeat for 600
seconds. The workload appended timestamped six-zone records derived from the
repository baseline every 2 seconds, selected zones and tabs, edited the
Overview and Ramp/Soak forms, set the chart history window to one hour and
toggled viewer visibility twice, exercised simulated recording
start/recovery/stop, and closed and reopened a floating chart. Heartbeat delay
is the positive excess of each
observed callback interval over 100 ms. Percentiles use nearest rank. Telemetry
latency starts after each complete JSONL append and ends when the corresponding
timestamp is present in the embedded plot. A 1.1 second drain period allows the
last appended record to be rendered.

The run retained 315 plotted points, 100 command outcomes (the configured
cap), one history-reader worker, one chart-transform worker, and one floating
window. The floating chart had no child timers. Its history and the embedded
chart shared one reader. The Logs source selected during the timed run had no
rows; the independent Logs overflow test verifies the 5,000-row cap. Tests also
verify the 20,000-point plot cap, bounded reader queues, bounded shutdown, and
worker exit. No RSS measurement was collected; bounded growth is verified
through these data and queue limits rather than a memory-profile claim.

## Reliability and rollback evidence

| Scenario | Offline evidence |
|---|---|
| Delayed responses, connection failure, post-send disconnect, pending writes, queue saturation, shutdown, restart generations, and no command replay | `test_qt_gui_service_adapter.py` with test-owned loopback endpoints bound to OS-assigned ports |
| Malformed snapshots/history, rotation, truncation, restart, and raw/rendered history caps | `test_gui_snapshot_reader.py`, `test_gui_history_reader.py` |
| Log rotation, truncation, malformed/oversized rows, shutdown, and 5,000-row cap | `test_qt_gui_logs.py` |
| Recording status, simulated recovery, and temporary recording files | `test_qt_gui_recording.py` and the timed replay exercise |
| Floating-chart pinning, refresh, lifecycle, no per-window timers, and 20,000-point cap | `test_qt_gui_charts.py` and the timed replay exercise |
| Direct Tk rollback using replay snapshots | `test_qt_gui_reliability.py` directly constructs `CN616AGUI` with an `OfflineReplaySession` directory and a patched no-socket `ServiceClient`; it reads all six synthetic zones and submits no mutation |
| Reopen lifecycle | `test_qt_gui_reliability.py` verifies prior history/log workers have stopped, the new offline session has distinct workers, and reopening submits no mutating command |

Tk rollback validation does not call `main()`, `gui_start.bat`, or any
service-starting launcher. The Tk direct constructor normally creates a
service client, so the test replaces it with a no-socket fake; the temporary
replay snapshots are read through the real Tk snapshot reader and panels.

## Test evidence

The combined Qt and shared GUI regression selection ran **142 tests: 141
passed, one skipped**. The skipped case is the ten-minute test, which is opt-in
for ordinary runs; it was then run separately and passed. The selection
included:

- Qt foundation, Overview, Configuration, Recording, Charts, Ramp/Soak, Logs,
  service-adapter, and reliability tests.
- Shared history/snapshot readers, service client, state values, retention
  estimator, Tk run panel, GUI/state reader, and tab-refresh tests.

The ten-minute command was:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
$env:CN616A_QT_TEN_MINUTE_EXERCISE = "1"
.\.venv\Scripts\python.exe -m unittest -v tests.test_qt_gui_reliability.QtReliabilityTests.test_ten_minute_six_zone_replay_responsiveness
```

The combined selection and reliability tests used the repository's Python 3.12
virtual environment. `compileall` for `py\qt_gui` and the new reliability test,
and `git diff --check`, passed. Fake-endpoint timeout and shutdown messages in
the combined test output were expected scenarios; all selected tests passed.
No production endpoint, service-starting launcher, serial port, or instrument
was accessed.

## Release 1 parity and acceptance

| Surface | Implemented | Offline tested | User UI status | Hardware |
|---|---|---|---|---|
| Six-zone readback and Overview controls | Yes | Overview, adapter, and timed replay | Pending | Not run — user only |
| Configuration and viewer settings | Yes | Configuration, adapter, timed replay | Pending | Not run — user only |
| Ramp/Soak and profile safeguards | Yes | Ramp/Soak, adapter, timed replay | Pending | Not run — user only |
| Recording, schedule, and recovery | Yes | Recording, adapter, timed replay | Pending | Not run — user only |
| Embedded/floating charts and bounded series | Yes | Charts, history reader, timed replay | Pending | Not run — user only |
| Logs and diagnostics | Yes | Logs tests, including overflow and rotation | Pending | Not run — user only |
| Offline lifecycle and Tk rollback | Yes | Reliability test and fake-service restart tests | Pending | Not run — user only |

The UI gate remains pending because this automated run was offscreen and does
not replace an operator walkthrough. Step 10's UI gate also remains pending.
Hardware testing is reserved for the user and is not required for this software
gate.

## Suggested user-only offline walkthrough

1. From the repository root, launch `.\bat\gui_qt_start.bat --offline-replay`.
   Confirm **OFFLINE REPLAY / NO HARDWARE** remains visible.
2. Select all six zones, change tabs, and edit Overview and Ramp/Soak fields.
   Apply only simulated settings; confirm no service or hardware status is
   implied.
3. Focus Zone 1, float its chart, select Zone 2 in Overview, and compare the
   pinned floating chart with the main chart. Close and reopen the float.
4. Start and stop a simulated recording, use the simulated recovery action,
   and confirm the Logs summary remains distinct from controller Run/Idle.
5. Close and reopen the offline GUI. Confirm the new temporary session starts
   without replaying prior commands, then inspect Logs sources and notices.

The Tk rollback was verified automatically with a no-socket client and
temporary replay files. Do not use the Tk launcher with replay paths as an
offline-only test: the Tk entry can submit to its configured local service.
Actual hardware and service behavior remain user-only checks.

## Disposition

Step 11 software reliability gate: **PASS**. Offline UI acceptance remains
pending; hardware validation remains **NOT RUN — USER ONLY**. Stop before Step
12.
