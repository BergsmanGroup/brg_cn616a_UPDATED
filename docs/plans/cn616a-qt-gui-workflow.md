# Qt Release 1: prompt sequence and handoff contract

**Instructions only. No step has been executed.** This workflow refines the
[two-release plan](cn616a-qt-gui-refactor.md). Release 2 remains out of scope.
Development uses repository data only; the assistant must not connect to hardware
or execute hardware tests. The user owns live-device validation and UI acceptance.

## Run one step at a time

Use VS Code **Chat: Run Prompt...**, or type `/cn616a-qt-01-foundation` in Chat.
The [workspace prompt folder](../../.github/prompts/) contains the complete series.
Invoke exactly one numbered prompt per development session. Read its report and
try its offline walkthrough before starting the next. Do not paste all prompts
into one autonomous implementation request.

| Step | Prompt | Model guidance | User checkpoint |
|---|---|---|---|
| 01 | [Foundation and offline replay](../../.github/prompts/cn616a-qt-01-foundation.prompt.md) | Premium recommended: architecture and isolation boundary | Launch, four tabs, visible offline label, unchanged Tk |
| 02 | [Asynchronous adapter and identities](../../.github/prompts/cn616a-qt-02-adapter.prompt.md) | Premium recommended: threading, outcomes, target identity | Delayed/disconnected replay remains responsive |
| 03 | [Overview telemetry](../../.github/prompts/cn616a-qt-03-overview.prompt.md) | Lower-cost implementation model | Readability, selection, stale/unknown display |
| 04 | [Embedded and floating charts](../../.github/prompts/cn616a-qt-04-charts.prompt.md) | Standard; premium only for measured lifecycle/performance faults | Zoom/pan, gaps, floating-window behavior |
| 05 | [Zone update controls](../../.github/prompts/cn616a-qt-05-zone-controls.prompt.md) | Standard with stable adapter; premium if outcome semantics need changes | Editing, pending/partial/unknown feedback; hardware check deferred |
| 06 | [Recording and schedule](../../.github/prompts/cn616a-qt-06-recording.prompt.md) | Standard | Directory/name, schedule, recovery, recording vs heating |
| 07 | [Configuration parity](../../.github/prompts/cn616a-qt-07-configuration.prompt.md) | Lower-cost with existing patch builders as reference | Dense forms, dirty edits, stored vs applied settings |
| 08 | [Ramp/Soak editor](../../.github/prompts/cn616a-qt-08-profile-editor.prompt.md) | Standard | 20 rows, validation, reload/discard, keyboard use |
| 09 | [Profile execution and autotune](../../.github/prompts/cn616a-qt-09-profile-autotune.prompt.md) | Premium recommended: non-idempotent operations and reconciliation | Confirmations/target clarity in replay; hardware check deferred |
| 10 | [Logs and diagnostics](../../.github/prompts/cn616a-qt-10-logs.prompt.md) | Lower-cost implementation model | Useful failures, categories, bounded tails |
| 11 | [Integration and reliability](../../.github/prompts/cn616a-qt-11-reliability.prompt.md) | Premium recommended: cross-cutting concurrency and parity | Disruptions, long session, shutdown/reopen, rollback |
| 12 | [User acceptance handoff](../../.github/prompts/cn616a-qt-12-handoff.prompt.md) | Lower-cost documentation model; escalate substantive defects | User decides readiness; hardware checklist is user-only |

Model guidance is advisory, not a hardcoded model setting or instruction to spawn
agents. Use the currently selected model. Escalate a small, evidenced failing
case rather than asking a premium model to rediscover the entire repository.
Steps 01–02 deserve the strongest reasoning; do not buy speed by weakening their
threading, identity, or isolation tests.

## Shared execution rules: mandatory for every prompt

### Safe offline boundary

- Never open a serial port, enumerate/probe controllers, scan endpoints, start
  the production service, call service-starting batch scripts, or run capture,
  live-device, or physical hardware tests. A user connecting hardware later does
  not authorize the assistant to use it.
- Test sockets only against a fake loopback server created by that test/session,
  bound to an OS-assigned free port. Never assume `127.0.0.1:8765` is fake.
- Production-service handler tests are allowed only after the controller is
  replaced by a fake **before construction**, with all output in temporary
  directories. This is a software contract test, not a hardware test.
- Preserve the immutable [baseline fixture](../../tests/fixtures/cn616a-baseline-v1/README.md)
  and hashes. Its 16 samples are synthetic, not a physical recording. Reuse
  [baseline replay](../../tests/baseline_reference.py) and existing test
  generators after inspecting their safe call paths. Do not run physical
  `capture` entry points.
- Repository `logs` currently contains diagnostics, not service snapshots or
  telemetry JSONL. Generate missing service-shaped inputs in temporary storage
  from the baseline/current driver offline replay; derive explicitly labeled
  mock outcomes for control/error cases. Never invent a physical capture.
- The offline UI must visibly say **OFFLINE REPLAY / NO HARDWARE**. Mutating
  controls in replay go only to its fake backend and show simulated outcomes.
  The fake can prove request/UI behavior, not thermal dynamics or hardware
  semantics. Preserve realistic timestamps/gaps; do not refresh old data silently.
- Offline mode must have no fallback to a normal service endpoint. Require an
  explicit separate service mode for the user's eventual device testing; switching
  mode requires a fresh adapter/session, not an in-place fake-to-live toggle.
- Document a repeatable offline launch. Stop agent-owned test servers at the end.
  For user walkthroughs provide a command that the user starts; do not leave
  background services running silently.

### Scope and economy

- Read this contract, the current prompt, and the previous step's report first.
  Read only linked source ranges needed for the task; inspect handler signatures
  and existing tests before adding behavior. Do not reread the entire plan,
  manual, image collection, or all earlier reports every step.
- Use the main plan's identity, compatibility, safeguards, and parity sections
  when relevant. Reuse existing helpers; do not import Tk widget modules into Qt.
  Preserve single service serial ownership, v1 API/files, Tk/default launch,
  driver, and pymodbus version. No Release 2 work or speculative framework.
- A service change needs a reproduced blocker, a focused failing software test,
  a minimal compatible fix, and related Tk/CLI regressions. Otherwise keep service
  code unchanged. Do not add hardware capabilities to make a mockup editable.
- Run the smallest relevant unittest selection, then widen only for affected
  integration. Use existing tools. Never run broad discovery without checking
  its device/network side effects. Install dependencies only after manifest
  changes or an actual missing-dependency failure.
- No automatic commits, toolkit rewrite, dependency upgrade spree, nested agents,
  or unsolicited visual redesign. Preserve unrelated worktree changes.
- Keep handoffs compact: paths/symbols, decisions, test commands, failures and next
  inputs. Record evidence, not a transcript. Do not create large JSONL fixtures,
  screenshots, or benchmark dumps in git by default.

### Definition of done and gates

1. Implement only this prompt. Focused offline tests pass, or report a blocked
   gate with exact failures; do not label a broken stage complete.
2. Produce `docs/reports/qt-gui-step-NN.md` using the template below, even if blocked.
   The directory/report is created during implementation, not as a fabricated
   completion record now.
3. Give the user a short offline walkthrough with exact commands and expected
   behavior. Mark device-dependent checks **USER-ONLY HARDWARE / NOT RUN**.
4. End the development turn. **Do not run the next prompt automatically.**
5. Before starting the next step, the previous report must have a passing automated
   gate. User UI feedback must be accepted, incorporated, or explicitly deferred
   by the user. An explicit request to continue can waive a pending offline UI
   review, but cannot fabricate its results or waive failing software safety tests.
   If a review blocks progress, stop with that report rather than implementing
   an unrelated next feature.
6. Hardware acceptance can remain pending through all development steps. It is
   not a prerequisite for implementing the next existing control. Only the user
   can record hardware results or authorize operational cutover.

## Required per-step report template

Target 400–800 words, less for simple steps. Include only relevant detail.

```markdown
# Qt GUI step NN: <title>
Date:
Automated development gate: PASS | BLOCKED
Offline user UI gate: PENDING | ACCEPTED | DEFERRED BY USER | CHANGES REQUESTED
Hardware validation: NOT RUN — USER ONLY [or user-provided evidence with attribution]

## Delivered
- Behavior and files changed; what is intentionally unfinished.
## Decisions and compatibility
- Adapter/model interfaces introduced or changed; safeguards; service/API/file effects.
## Automated evidence
| Command/test | Result/count | What it proves / does not prove |
|---|---|---|
Include failures/skips, environment, measured thresholds where applicable.
## User walkthrough — offline
Numbered launch/action/expected-result checks; no device connection required.
## User-only later hardware checks
Relevant feature, expected observation, prerequisites and stop condition.
Mark NOT RUN; do not prescribe unverified safe temperatures or process limits.
## Feedback and disposition
User observations quoted/attributed; fixes, explicit deferrals, or pending.
## Risks, rollback and next handoff
Exact next prompt; prerequisite status; small set of files/symbols to read.
```

Report software correctness, offline usability, and hardware acceptance separately.
Do not use “fully validated” for an offline-only result. A skipped relevant
software test is not a pass. No pre-filled report should imply user acceptance.

## Release status and stopping points

- Steps 01–04: usable offline viewer/charts, not a control replacement.
- Steps 01–06: minimum useful operating UI in replay, with zone updates and
  recording contract tests. Configuration/profile/autotune/Logs still incomplete.
- Steps 01–11: target software feature parity; use the plan checklist to identify
  omissions. Step 12 records **software-ready / hardware acceptance pending**,
  not a claim that hardware works.
- Keep Tk default throughout. Prefer fewer completed stages with honest reports
  over unfinished parallel features. User review adds elapsed time; a one-night
  full finish is an ambition, not guaranteed by these smaller gates.
