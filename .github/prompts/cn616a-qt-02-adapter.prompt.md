---
description: "Qt step 02: wire bounded asynchronous v1 service adaptation and keyed models, validated only against fakes."
agent: agent
---
# Step 02 — adapter, outcomes and identity

Run only step 02. Premium model recommended. Obey the
[shared workflow](../../docs/plans/cn616a-qt-gui-workflow.md); require the step 01
report/gates. No hardware connections/tests.

## Read narrowly
Read `docs/reports/qt-gui-step-01.md` and its interface handoff, current Qt models,
[ServiceClient](../../py/gui/service_client.py),
[snapshot reader](../../py/gui/snapshot_reader.py),
[state values](../../py/gui/state_values.py), and their focused tests.
Inspect only relevant [service handlers](../../py/cn616a_service.py) for payloads.

## Deliver
- Implement `service_adapter.py` reusing toolkit-neutral client/readers. Socket
  work/file parsing stay off the Qt thread; drain bounded results via Qt event
  loop. Models/widgets are updated only on the GUI thread.
- Adapt five legacy snapshot categories and aliases into keyed values, preserving
  zero, missing, source time, stale/error, and target identity. Unknown identity
  permits reading but blocks writes; foreign keys are rejected.
- Preserve exact v1 requests and response ID/outcome semantics, ordered fail-stop
  sequences, visible full/closed queue results, and no ambiguous-write retries.
  Do not equate transport acknowledgment to controller readback.
- Tag endpoint/session generations, reject stale display updates, retain command
  outcomes, and prevent old histories from appearing under a new controller.
  Keep active endpoint separate from stored configuration.
- Complete explicitly selected service mode for the user's later testing, without
  executing it yourself. Offline mode uses only its owned fake/backend and never
  falls through. Freeze the adapter interface for subsequent prompts.

## Automated gate
Fake loopback listener uses an ephemeral port. Test success, negative response,
connect failure, malformed/partial/mismatched responses, delayed/post-send
disconnect, partial sequences, queue saturation, shutdown, thread ownership,
generation/target mismatch and no replay after restart. Patch the service's
controller before construction for selected exact-handler contract tests.
Run affected client/snapshot/state helper regressions if touched.

## End gate
Write `docs/reports/qt-gui-step-02.md`; include interface signatures and reusable
fake scenarios. Offline user walkthrough: delayed/down endpoint indicators,
responsive tab navigation and close. State hardware integration NOT RUN.
Stop before step 03.
