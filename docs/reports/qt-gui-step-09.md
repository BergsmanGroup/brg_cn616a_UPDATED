# Qt GUI step 09: profile execution and autotune

- Date: 2026-11-01
- Automated development gate: PASS — focused Qt and service-contract regressions
- Offline user UI gate: PENDING
- Hardware validation: NOT RUN — USER ONLY

## Delivered
- Added selected-zone and all-monitored-zone profile start/stop. Confirmation
  shows exact zones and command value and warns that
  controller output behavior may change. The UI does not claim pause/resume or
  infer a hardware Run/Idle state.
- Added selected-zone autotune start/stop. Confirmation distinguishes its
  configured setpoint from the trigger and warns about output changes. State
  and setpoint come only from available telemetry.
- Pending commands disable duplicate triggers. Unknown trigger outcomes remain
  visibly unresolved across later snapshots; acknowledgment only clears the
  local warning and neither proves execution nor retries the command. A
  confirmed failure on an all-zone profile request warns that earlier zone
  writes may have applied.
- Added adapter validation for explicit profile zone lists and verified target
  identity. Offline replay changes only temporary synthetic snapshots and
  labels outcomes simulated.

## Decisions and compatibility
- Preserved service v1 operations and controller semantics. The adapter's
  multi-zone authorization exception applies only to
  `set_start_profile`; lists must be explicit, unique valid zone IDs and include
  the anchor target under the verified controller identity.
- Existing identity/writeability checks remain in force. Profile readback is
  explicitly the controller's profile-enable field, not a fabricated execution
  state. No service protocol, Tk behavior, or hardware driver was changed.
- Step 08's offline walkthrough remains deferred by the user's explicit
  instruction to proceed to Step 09. This step's offline UI walkthrough is
  still pending; no acceptance is claimed.

## Automated evidence

All Qt tests ran with `QT_QPA_PLATFORM=offscreen`. Each module ran in its own
Python process because the installed pyqtgraph emits a `LabelItem.sizeHint`
`_sizeHint` error in combined Qt test processes.

| Command/test | Result/count | What it proves / does not prove |
|---|---:|---|
| `python -m unittest -q tests.test_qt_gui_ramp_soak` | PASS, 12 | Confirmation/cancel, exact selected/all-monitored requests, pending lockout, unknown persistence/acknowledgment, no automatic trigger on tab entry/reopen, offline simulation and profile editor regressions. |
| `python -m unittest -q tests.test_qt_gui_service_adapter` | PASS, 30 | Verified-target and explicit-zone-list authorization, rejects malformed/foreign targets, and existing adapter outcomes. |
| `python -m unittest -q tests.test_qt_gui_foundation` | PASS, 14 | Foundation and offline isolation regressions. |
| `python -m unittest -q tests.test_qt_gui_overview` | PASS, 13 | Overview regressions. |
| `python -m unittest -q tests.test_qt_gui_configuration` | PASS, 9 | Configuration regressions. |
| `python -m unittest -q tests.test_qt_gui_charts` | PASS, 10 | Chart and floating-window regressions. |
| `python -m unittest -q tests.test_qt_gui_recording` | PASS, 4 | Recording regressions. |
| `python -m unittest -q tests.test_service_commands` | PASS, 21 | Existing fake-controller service command contracts. |
| `python -m compileall -q py\qt_gui tests\test_qt_gui_ramp_soak.py tests\test_qt_gui_service_adapter.py`; `git diff --check` | PASS | Python syntax and whitespace checks. |

No production service, serial port, or instrument was accessed.

## User walkthrough — offline
1. From PowerShell at the repository root, run
   `.\bat\gui_qt_start.bat --offline-replay`. Confirm the visible
   **OFFLINE REPLAY / NO HARDWARE** indication.
2. Open **Ramp/Soak** and review profile-enable readback, segment/freshness,
   autotune state, and autotune SP.
3. Use **Start selected** and **Start all monitored**. Review exact zones and
   output warning, cancel and verify no request, then confirm and verify the
   simulated result.
4. Repeat for profile stop. Confirm that readback is not described as
   pause/resume or a separate Run/Idle state.
5. Review selected-zone autotune confirmations and verify SP is distinct from
   the trigger. Inspect simulated readback and unresolved-outcome warning; a
   local acknowledgment must say the outcome remains unknown.
6. Switch tabs or recreate the page and confirm no profile/autotune trigger is
   automatically repeated. Do not switch to a live service for this walkthrough.

## User-only later hardware checks
**USER-ONLY HARDWARE / NOT RUN.** Before any live action, the operator must
independently verify service endpoint, controller identity, monitored and
selected zones, serial ownership, and fresh readbacks. Review each exact target
and output-change confirmation before confirming. Observe per-zone status after
profile start/stop and autotune start/stop; do not infer thermal safety or
controller Run/Idle from the GUI. Stop and investigate if identity, target,
readback, or outcome is unexpected, stale, rejected, or unknown. Never retry an
unknown trigger based only on the GUI warning.

## Feedback and disposition
No Step 09 offline UI feedback has been received; walkthrough is pending. Step
08's pending walkthrough was explicitly deferred to continue this requested
step, not accepted.

## Risks, rollback and next handoff
- All-monitored profile requests may partially apply before a later zone fails;
  inspect fresh per-zone status and do not automatically retry.
- Keep the Tk GUI as the operational fallback. Hardware behavior remains
  unverified and user-only.
- Next prompt: `cn616a-qt-10-logs.prompt.md`. Stop here; do not start Step 10
  until the Step 09 offline walkthrough is reviewed or explicitly deferred.
