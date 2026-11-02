# Qt GUI step 02: asynchronous service adapter and identities
Date: 2026-11-01
Automated development gate: PASS
Offline user UI gate: DEFERRED BY USER
Hardware validation: NOT RUN — USER ONLY

## Delivered
- Added `QtServiceAdapter` to reuse `ServiceClient`, `StateSnapshotReader`, and
  `IncrementalHistoryReader`. Socket requests and file parsing remain on worker
  threads; a Qt timer drains bounded results and emits model updates on the GUI
  thread.
- Adapted telemetry, config, ramp/soak, analysis, and service-config snapshots
  with source timestamp, age, stale/error state, legacy aliases, zero/missing
  values, and controller/zone keys. Unknown or conflicting identity permits
  reads but keeps writes read-only.
- Added explicit `--service --host --port --logs-dir` mode. Default startup
  remains disconnected; offline replay stays mock-only and has no service
  fallback. No Qt control widgets have been migrated.
- Preserved request IDs, v1 payloads, ordered fail-stop sequences, queue
  outcomes, and unknown post-send results without automatic retries. A service
  acknowledgment is not presented as controller readback. Shutdown retains
  unresolved and unsent outcomes.
- Added history source identity tracking and immediately withhold a displayed
  history when identity is missing, conflicting, changes, or is unavailable.
  Malformed and untagged history records are not treated as belonging to the
  one identifiable subset. Updated the
  step 01 report: the user explicitly asked to proceed, so its pending UI review
  is recorded as **DEFERRED BY USER**, not accepted.

## Decisions and compatibility
- Frozen adapter signatures: `generation -> int`, `status -> AdapterStatus`,
  `snapshots() -> Mapping[str, AdapterSnapshot]`,
  `outcomes() -> Sequence[CommandOutcome]`,
  `submit(messages: Sequence[Mapping[str, object]], *, target: ZoneKey | None =
  None, coalesce_key: str | None = None) -> SubmissionStatus`,
  `is_current_generation(generation: int) -> bool`, and `shutdown() -> None`.
- `QtServiceAdapter(host, port, logs_dir, *, capacity=32, timeout_s=2.0,
  poll_interval_ms=100, stale_after_s=5.0, history_hours=1.0)` accepts
  `host: str`, `port: int`, and `logs_dir: Path`. It also provides
  `refresh_status() -> ClientSubmitStatus` and `history -> AdapterHistory | None`.
  Endpoint values come only from explicit arguments, never saved service
  configuration.
- Changes to the toolkit-neutral client/history reader are limited to
  caller-polled shutdown outcomes and history identity metadata. Existing Tk
  launch and service serial ownership are unchanged.

## Automated evidence
| Command/test | Result/count | What it proves / does not prove |
|---|---:|---|
| `$env:QT_QPA_PLATFORM='offscreen'; python -m unittest discover -s tests -p test_qt_gui_service_adapter.py -q` | PASS, 19 | Fake ephemeral loopback success/failure, malformed/mismatched responses, queue/sequence/shutdown outcomes, delayed/down indicators, GUI-thread delivery, stale/unknown/foreign identity, endpoint selection, history filtering, and responsive tabs. No live-service behavior. |
| `$env:QT_QPA_PLATFORM='offscreen'; python -m unittest discover -s tests -p test_qt_gui_foundation.py -q` | PASS, 11 | Offline isolation, launcher, four tabs, explicit service argument validation, and prior foundation regressions. |
| `python -m unittest discover -s tests -p test_gui_service_client.py -q` (same command run separately for snapshot, state-reader, state-values, and history-reader test files) | PASS, 27 | Existing bounded client, snapshot parsing/age, state-value, and history behavior. |
| `python -m unittest discover -s tests -p test_service_commands.py -k test_get_status_contains_service_config -k test_set_sp_abs_dispatches_to_controller -k test_controller_error_is_returned -q` | PASS, 3 | Exact `get_status` and `set_sp_abs` handler contracts; `CN616A` was patched to `FakeCtl` before service construction and output was temporary. |
| `python -m compileall -q py\qt_gui py\gui\service_client.py py\gui\history_reader.py tests\test_qt_gui_service_adapter.py tests\test_qt_gui_foundation.py tests\test_gui_history_reader.py`; `python -m py.qt_gui --help`; `git diff --check` | PASS | Syntax compilation, safe CLI help, and whitespace validation. |

Validation used Python 3.13.14 and `QT_QPA_PLATFORM=offscreen`; no tests were
skipped. An exploratory pytest invocation could not initialize because the
repository's top-level `py` package shadows pytest's expected `py.path`;
the repository-standard `unittest` suites above ran successfully. Pylance
reported no errors in changed source files; remaining diagnostics were
severity-4 unused-parameter warnings in test helpers/context-manager methods.
Expected test logs came from intentionally missing snapshots, shutdown, and an
injected negative controller response.

## User walkthrough — offline
1. From PowerShell at the repository root, run
   `$env:QT_QPA_PLATFORM='offscreen'` and
   `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_qt_gui_service_adapter.py" -v`.
   The test-owned ephemeral fake endpoints exercise delayed and down indicators,
   keep the event loop responsive, and verify bounded close; no real endpoint is
   contacted.
2. Run `Remove-Item Env:\QT_QPA_PLATFORM`, then
   `.\bat\gui_qt_start.bat --offline-replay`. Switch through all four tabs and
   close the window. Expect the **OFFLINE REPLAY / NO HARDWARE** notice, no
   enabled controls, and temporary-file cleanup.

## User-only later hardware checks
**USER-ONLY HARDWARE / NOT RUN.** The explicit service launch documented in
README is for later user-directed testing against an independently verified,
already-running service. No service, serial port, or controller was started,
probed, or tested here.

## Feedback and disposition
Step 01 UI review was explicitly deferred by the user's request to proceed.
The user said, "The changes look okay," but noted that the GUI still had "just
four tabs" without the intended aesthetic and functional experience. They
conditionally requested step 03 if those features were planned for later steps.
The prototype and workflow confirm that the shell was temporary; proceeding does
not constitute formal acceptance of the step 02 offline walkthrough.

## Risks, rollback and next handoff
At this handoff the Qt tabs were placeholders; service snapshots and outcomes
were adapted but not yet presented in feature widgets. Tk remained the
operational/default GUI. The user explicitly authorized proceeding to
`cn616a-qt-03-overview.prompt.md`; see the step 03 report for the subsequent
Overview and its pending review.
