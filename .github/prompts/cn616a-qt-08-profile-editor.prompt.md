---
description: "Qt step 08: port the 20-row Ramp/Soak editor and read/save contract; no physical profile execution."
agent: agent
---
# Step 08 — Ramp/Soak editor

Run only step 08 using the [shared workflow](../../docs/plans/cn616a-qt-gui-workflow.md).
Require step 07 gates. Standard model; use existing semantics rather than designing
a new profile language.

## Read narrowly
Read `docs/reports/qt-gui-step-07.md`, `RampSoakPanel` in
[display panels](../../py/gui/display_panels.py), profile handlers/tests in
[service](../../py/cn616a_service.py), and replay profile data.

## Deliver
- `ramp_soak.py` and profile table model: zone identity, 20 SP/slope/time rows,
  segment count 0–20, Standard/Stop-at-end/Hold-at-end mode, available current
  state/segment and source age. Preserve existing units/type hints.
- Read on tab entry/reload via `read_rampsoak` even when periodic cadence is zero;
  asynchronous and coalesced, no endless reload or overwriting dirty rows.
- Mode/count/save use exact `set_control_mode`, `set_num_segments`,
  `set_rampsoak_segments` payloads. Validate finite values and existing driver
  constraints; do not invent thermal limits.
- Explicit selected-zone discard/reload handling; acknowledge and read back
  honestly. Profile start/stop buttons stay unavailable until step 09.

## Automated gate
Test row count, count boundaries, Stop/Hold mapping, serialized segment indices
and values against patched service handlers, invalid/non-finite fields, zero,
dirty edit preservation, zone changes, cadence-zero refresh, missing profile
data, failure/partial/unknown outcomes and readback freshness.

## End gate
Write `docs/reports/qt-gui-step-08.md`. Offline walkthrough: keyboard entry,
20-row navigation, validation, change zone while dirty, reload, save and fake
readback. Profile writes to a real controller are USER-ONLY / NOT RUN.
Stop before step 09.
