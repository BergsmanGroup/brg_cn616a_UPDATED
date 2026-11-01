---
name: cn616a-gui-responsiveness-e
description: "Add a safe browser interface for CN616A using the service as the sole controller owner."
argument-hint: "Specify local-only needs; remote access requires separate approval"
agent: agent
---

# CN616A GUI responsiveness: E — browser-based interface

Only implement this stage when explicitly in scope. This is step seven:
Baseline, A1, A2, B, C, D, E. Read [README.md](../README.md), inspect the
current GUI, service, protocol, and tests, and verify completed Baseline, A1,
A2, B, C, and D gates in the repository. If prerequisites are missing, report
the gap instead of silently implementing earlier stages. Establish whether
the browser interface is local-only or needs remote access; do not infer remote
exposure from the existence of this stage. Keep the service as sole controller
owner.

## Implementation

- Prefer a separate local web process or clearly isolated web layer that uses
  the existing service API and shared state/history abstractions; do not
  duplicate the serial driver.
- Bind to loopback by default. Protect mutating requests against cross-site
  requests and hostile origins, validate inputs, limit work and connections,
  and do not expose the unauthenticated raw TCP command endpoint. Keep assets
  local/offline by default and do not embed secrets in source or logs.
- Remote access or a non-loopback bind requires separate explicit approval
  and a documented threat model, authentication/authorization, transport
  security, origin/CSRF and WebSocket protections, auditability, and deployment
  review. Invoking this prompt or completing earlier stages does not grant
  that approval.
- Preserve features and command-outcome semantics of the preceding GUI,
  support bounded live updates and chart backpressure, and make stale data and
  disconnects visible. Include startup, shutdown, Windows deployment, and
  rollback instructions.

## Safety and compatibility

- Never connect to a real controller or send real control commands, change
  setpoints, start autotune, or change a run schedule without separate,
  explicit operator authorization. Use a fake service and temporary data.
- Keep controller I/O serialized by the service. Do not duplicate its serial
  driver, silently retry mutating operations, or misrepresent timeout,
  partial, or unknown outcomes.
- Surface errors explicitly and bound clients, requests, work, memory, and
  repeated updates. Preserve existing CLI/protocol behavior, Python
  3.12/Windows support, configuration/log formats, and user changes.
- Add no package unless needed; if needed, update dependencies, installation
  instructions, and launchers together. Do not perform destructive git
  operations, commit, or modify unrelated files.

## E gate and report

Test against a fake service with browser/integration tests, including malformed
and unauthorized requests, origin protections, reconnects, partial command
outcomes, slow clients, long-running updates, and accessibility. Confirm the
default listener is loopback-only and no real controller commands were sent.

Run focused tests using the configured runner and existing test patterns.
Update directly affected README, configuration, launchers, and protocol docs.
Report changes, security-relevant defaults, tests and results, unverified
Windows/hardware checks, residual risks, and any deliberately omitted feature.
Distinguish simulated tests from operator validation. Stop at this gate.
