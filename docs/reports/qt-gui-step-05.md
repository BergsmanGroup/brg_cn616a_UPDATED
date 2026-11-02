# Qt GUI step 05: zone update controls

Date: 2026-11-01
Automated development gate: PASS
Offline user UI gate: PENDING
Hardware validation: NOT RUN — USER ONLY

## Delivered
- Added focused-zone absolute setpoint, control-method, and autotune-setpoint
  fields with explicit Apply and Discard actions. PID and ON/OFF map to the
  existing `control_method` enum; the UI labels this separately from profile
  `control_mode`.
- Apply targets the selected identity-keyed `ZoneKey`; there is no editable unit
  selector and no `unit` field is sent. The ordered request is exactly
  `set_sp_abs`, `set_control_method`, `set_autotune_setpoint`. No autotune trigger
  was added.
- Finite values are checked at Apply, including the controller's float32 range.
  Drafts remain scoped to each zone across telemetry refreshes and selection
  changes; Discard reloads the latest cached readback. Fields and Apply are
  disabled unless the selected target is verified and writable, or is an
  explicitly labeled offline replay.
- Per-operation outcomes show acknowledged, rejected, not-sent, and unknown
  states. A partial sequence is identified as non-atomic; no automatic retry
  occurs. After live acknowledgments, the draft remains until fresh telemetry
  reconciles the result: an exact match clears it, while a mismatch leaves the
  draft for operator review. A mismatch or unknown result is not reported as
  success.
- Offline replay offers deterministic simulated success, rejection, partial,
  and unknown scenarios. These outcomes never contact a service or instrument.

## Decisions and compatibility
- Reused the frozen adapter submission/outcome contract and existing v1 JSON
  operations. Service, Tk, driver, serial ownership, and endpoint startup
  behavior are unchanged.
- The default and offline-replay modes cannot write to a service. Explicit
  service mode must report a reachable service, connected controller, fresh
  telemetry, and matching identity before controls enable.
- `--service --read-only` is available for viewing an already-running service.
  The mode disables controls and enforces write rejection inside
  `QtServiceAdapter`, not only in the widgets.
- Service acknowledgments are not treated as controller readback. A fresh,
  matching telemetry snapshot is required to clear a successful live draft.
  Ambiguous outcomes remain visible and are never retried automatically.

## Automated evidence
| Command/test | Result/count | What it proves / does not prove |
|---|---:|---|
| `QT_QPA_PLATFORM=offscreen; .\.venv\Scripts\python.exe -m unittest -q tests.test_qt_gui_overview tests.test_qt_gui_foundation` | PASS, 27 | Zone targeting, exact order, numeric validation including NaN/infinity, zero and negative values, dirty refresh/zone retention/discard, pending controls, readback mismatch/match, replay isolation and outcome scenarios. |
| `QT_QPA_PLATFORM=offscreen; .\.venv\Scripts\python.exe -m unittest -q tests.test_qt_gui_service_adapter` | PASS, 24 | Exact Qt Apply payloads against a test-owned ephemeral fake endpoint, read-only mode rejects writes, per-operation partial results, no retry, queue saturation, delayed and unknown adapter outcomes. |
| `QT_QPA_PLATFORM=offscreen; .\.venv\Scripts\python.exe -m unittest -q tests.test_qt_gui_charts` | PASS, 9 | Existing chart and history fan-out regressions. |
| `.\.venv\Scripts\python.exe -m unittest -q tests.test_gui_history_reader tests.test_service_commands` | PASS, 31 | Reader regressions and exact `set_sp_abs`, `set_control_method`, and `set_autotune_setpoint` handler contracts with the controller patched to a fake before service construction. |
| Compileall, `python -m py.qt_gui --help`, `git diff --check` | PASS | Changed Python syntax, safe help, and whitespace. |

Final focused evidence totals 91 passing tests with no skips, including CLI
read-only flag validation and an adapter/UI test that confirms a connected fake
service remains read-only and receives no mutation request. An earlier
combined-run attempt exposed timing-sensitive fake-endpoint assertions; those
tests now wait for the corresponding request/outcome before asserting order.
One initial multi-module process also exited with a Windows access-violation code
without a test traceback; that did not recur in the final suite runner. Test
diagnostics were from fake endpoints, temporary snapshots, and deliberately
injected errors. No live service, serial port, or hardware was accessed.

## User walkthrough — offline
1. From PowerShell at the repository root, run
   `.\bat\gui_qt_start.bat --offline-replay`. Confirm the **OFFLINE REPLAY / NO
   HARDWARE** banner and select a zone.
2. Edit the absolute SP, PID/ON/OFF method, and autotune SP fields. Change
   zones and return; confirm drafts remain attached to their original zone.
   Choose **Discard edits** to reload cached values.
3. Choose each **SIMULATED OUTCOME** scenario and click **Apply to selected
   zone**. Confirm per-operation success, rejection/fail-stop, partial, and
   unknown feedback is labeled simulated and that the UI says no service or
   hardware request occurred.
4. Close the window. No temporary replay files or service process should remain.

## User-only later hardware checks
**USER-ONLY HARDWARE / NOT RUN.** The user/operator must independently verify
the running service endpoint and its COM4 ownership, confirm the displayed
controller identity and selected zone, and follow the process-specific safety
procedure. Only operator-selected values and actions should be used. Confirm
fresh controller readback after any service acknowledgment. Stop on unknown or
unexpected outcomes, identity mismatch, stale data, or mismatching readback;
do not blindly retry.

## Feedback and disposition
The user explicitly requested proceeding with Step 05 before completing the
Step 04 offline walkthrough; that review is recorded as deferred, not accepted.
Step 05's offline editing review is pending. No user hardware result is claimed.

## Risks, rollback and next handoff
Configuration, recording/scheduling, Ramp/Soak, autotune triggers, and Logs are
not migrated. Keep the existing Tk GUI as the operational fallback. Next prompt:
`cn616a-qt-06-recording.prompt.md`; do not begin it until Step 05 automated gates
pass and its offline UI feedback is accepted, incorporated, or explicitly
deferred by the user. Hardware validation remains user-only.
