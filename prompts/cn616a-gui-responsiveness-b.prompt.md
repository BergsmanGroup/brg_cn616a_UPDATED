---
name: cn616a-gui-responsiveness-b
description: "Measure and improve CN616A service command scheduling without concurrent controller I/O."
argument-hint: "Add constraints or identify verified A1/A2 prerequisite evidence"
agent: agent
---

# CN616A GUI responsiveness: B — service scheduling

Measure and, only when justified, improve service-side command scheduling and
responsiveness. This is step four: Baseline, A1, A2, B, C, D, E. Read
[README.md](../README.md), inspect the current service, clients, and tests, and verify the completed Baseline, A1,
and A2 gates in the repository. If any prerequisite is absent, report the gap
instead of implementing earlier stages. Work only on service
scheduling/protocol behavior. The service remains the sole owner of the serial
port and controller communication.

## Measure and implement only as supported by evidence

Measure service queue wait, controller I/O duration, and end-to-end command
latency before changing the service. Then, only as justified:

- Reduce head-of-line blocking between long poll steps and queued commands
  without allowing concurrent controller access. If a poll is split into
  smaller steps, preserve safe transaction boundaries and coherent snapshots.
  Use bounded scheduling and fairness so telemetry, configuration, analysis,
  run scheduling, and commands cannot starve each other.
- If concurrent TCP clients are needed, bound clients and request sizes,
  handle slow/disconnected clients and newline-delimited framing, and preserve
  response IDs and compatibility with existing GUI and CLI clients. More
  network concurrency must not mean concurrent serial operations.
- Define and test command lifecycle and timeout semantics. A queued command
  that times out may still run in the current implementation. If cancellation
  before execution or later status lookup is introduced, make the transition
  race-safe and observable. Never pretend that an already-started write was
  cancelled or automatically retry an unknown outcome.
- Consider a combined zone-update operation only if it improves measured
  latency. Preserve existing operations for older clients, maintain ordering,
  validate every field, and return per-operation results. Unless real hardware
  guarantees atomicity, explicitly report partial completion and do not label
  the operation atomic. A service-produced run-status snapshot is optional;
  make freshness and write failures visible.

## Safety and compatibility

- Never connect to a real controller or send real control commands, change
  setpoints, start autotune, or change a run schedule without separate,
  explicit operator authorization. Use fake controllers/services and fake
  clients only.
- Keep all controller I/O serialized by the service. Preserve polling,
  telemetry persistence, scheduled run stop/recovery, existing CLI and JSON
  Lines clients, and protocol compatibility. Do not silently retry a
  potentially mutating command; make timeout, partial, and unknown outcomes
  observable.
- Surface failures explicitly. Bound clients, requests, queues, memory,
  worker lifetimes, and repeated work. Keep Python 3.12/Windows support.
- Do not add a package unless required; update dependencies, installation
  instructions, and launchers together if needed. Preserve user changes; do
  not perform destructive git operations, commit, or modify unrelated files.

## B gate and report

Use fake controllers and concurrent fake clients to test ordering, timeouts
before and during execution, disconnects, retries/deduplication if added,
partial writes, fairness, run scheduling, poll cadence, and backward
compatibility. Verify there is never more than one serial operation in flight.
Report GUI feedback latency, queue wait, and controller completion separately;
do not promise sub-second hardware execution without measurements.

Run focused tests using existing patterns in [tests/](../tests/) and the
configured runner, e.g. `.\.venv\Scripts\python.exe -m pytest -q` with
relevant files. Update directly affected README, configuration, launchers, and
protocol docs. Summarize changes, fixtures, measurements, tests, unverified
hardware/Windows checks, residual risks, and the next step. Do not claim real
hardware validation. Stop at this gate.
