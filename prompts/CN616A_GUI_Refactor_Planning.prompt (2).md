---
description: 'Repository-aware CN616A Qt GUI refactor planning with visual mockups and isolated interactive prototypes; no production changes.'
agent: 'agent'
---

# Plan the CN616A GUI refactor (review and proposal only)

You are acting as a senior software architect and controls-software reviewer with full access to this repository. **Do not implement the production refactor yet.** Inspect the actual code, dependencies, service protocol, hardware-facing paths, logging and plotting implementation, and tests. Produce a detailed, repository-specific plan and isolated visual design artifacts that can later be used as implementation instructions for a coding agent. Do not assume the concepts below are already supported or necessarily desirable; challenge them where warranted.

## Context and goals

We are building a desktop GUI for an Omega CN616A six-zone temperature controller. The existing GUI uses Tkinter and has suffered responsiveness/freezing issues, especially around network calls, polling, numeric widgets, and embedded matplotlib charts. A performance refactor was considered:

- **Step A (reportedly completed):** background network operations, UI controls disabled while awaiting responses, status-file reader sharing, refresh only visible tabs, chart incremental updates, and reducing expensive Tkinter widget redraws. Verify the actual implementation rather than assuming it is complete.
- **Step B (next/ongoing):** improve service command handling (accept commands between poll steps rather than only once per full cycle, potentially handle multiple clients, possibly combine related set-zone commands, and publish run/status promptly). Review actual status and correctness before proposing further work.
- **Step C (deferred):** moving charts into separate windows/processes was considered, but we prefer not to spend significant effort optimizing the old Tkinter/matplotlib stack if a more capable integrated GUI is imminent.

The long-term direction is a **modern integrated desktop GUI built with Qt (preferably PySide6) and pyqtgraph**, subject to your repository-specific assessment of feasibility, packaging, licensing, maintenance, and performance. Keep live charts integrated in the new GUI, with optional detachable/floating chart windows for selected zones if this can be done cleanly. Distinguish independent Qt windows from independent processes and avoid suggesting multiple Tk roots as the long-term architecture. Keep the hardware-control service separate from the GUI where practical.

## Visual and source references

Review the GUI mockup images supplied alongside this prompt, including the earlier Overview/Configuration/Logs iterations and the latest multi-controller Configuration mockup. Treat these as **concept sketches, not exact specifications**. If images are not attached or available in your context, say so and request them rather than claiming to have examined them. Also review the Omega CN616A manual (M5704, if attached or available), especially communications, controller-wide versus per-zone parameters, Modbus registers, control modes, autotune, alarm behavior, and ramp/soak. Do not assume the manual is part of the repository; identify any missing reference material.

The latest mockup has some conceptual inaccuracies to correct: the PC/service is the Modbus master on an RS485 multidrop bus; CN616A devices have unique slave addresses, rather than one CN616A acting as the controlling master for the others. Avoid representing illustrative PID values or placeholder serial settings as actual hardware state.

## Proposed information architecture

The application should have **four primary tabs only**:

1. **Overview:** system/service health and recording controls, a table of zones across one or more controllers, and a focused-zone control area with setpoint, control mode, current PID gains, and an Autotune action. The zone table selects the focused zone; no redundant Active Zone tab. Separate visually and logically **system/service information and controls** from **zone-specific information and controls**. Include compact live telemetry/charts where useful, without duplicate tabs for Telemetry, Run, or Charts.
2. **Configuration:** communications, discovered/configured controller units, zone metadata and configuration, equilibrium criteria, polling, recording file settings/rotation, and other advanced settings. Organize controller-wide settings separately from per-zone settings. Allow selection of an individual controller and an all-controllers view. Avoid cramming every advanced setting into one permanently expanded screen; propose progressive disclosure or subnavigation as appropriate.
3. **Ramp/Soak:** profile definition, selection, start/stop/hold behavior and per-zone/controller applicability consistent with the hardware and existing service. Identify anything not actually supported.
4. **Logs:** recording history, files, events, errors and diagnostic logs, with clear distinction between telemetry recordings and operational/service logs.

### Overview specifics

- Use **Record / Recording**, not Run / Running, for data capture. A **red recording dot flashes** while actively recording. Do not confuse recording state with the CN616A hardware's Run/Idle control state; label those distinctly where needed.
- A dedicated **Start/Stop Recording** card is appropriate; initially record **all configured zones** (including all connected controllers) without per-zone recording selection.
- The zones table should show controller identity, local zone index, user-defined zone name, PV, SP, state/mode, **MAE**, and **equilibrium boolean**, plus clear Focus, Deactivate (red square), and Activate (green arrow) actions. Consider scalable grouping/filtering for 6, 12, 18+ zones.
- Focusing a row changes the zone-specific control card and charts, not the hardware state.
- Focused-zone card: edit setpoint, select supported control mode, display actual PID P/I/D constants, and launch Autotune through a confirmation/dialog that asks for an autotune temperature, **defaulting to the current setpoint**. Avoid repeating PV/SP/delta unnecessarily if they are already legible in the zone table.
- **Activation/deactivation safety is unresolved:** do not assume `SP = -9999` is a supported or safe Off command. Investigate whether the controller has a real zone-disable or output-off mechanism, how this interacts with hardware Run/Idle, alarms, profiles, and the ability to restore the previously commanded SP. A 0 °C setpoint is not universally equivalent to output-off. Propose an explicit state model, interlocks, confirmations, feedback and failure behavior. Never claim a zone is off until verified by controller response/state.
- The equilibrium calculation currently uses one global set of thresholds (window duration, MAE and possibly slope); allow for future per-zone overrides without requiring them now. Distinguish software-computed equilibrium from controller status.

### Multi-controller expansion

- The CN616A supports six independent zones per unit and unique Modbus addresses on RS485. Model the identity of a zone as **(bus/connection, Modbus address, local zone 1–6)**; global sequential zone numbers are presentation only.
- Show multiple controller units, their user-defined names, addresses, per-unit online/stale/offline state, last response and communication error counts. Propose add/remove/discovery and address-conflict handling without destructive or automatic address reassignment.
- Treat bus-level serial parameters (port, baud, parity, etc.) separately from per-controller parameters and per-zone parameters. Verify any assumed serial framing, addressing range, scan behavior, and supported Modbus operations against code and manual.
- Prefer a single bus manager/serial owner in the service. Evaluate polling cadence, bus contention, fairness, timeout/retry policy, and command priority as the number of units increases. Avoid concurrent unsynchronized access to the same serial port.
- Recording should capture all zones across all configured controllers with stable identities, timestamps, and explicit missing/offline data semantics; consider compatibility/migration for existing CSV formats and downstream analysis.

## Visual design and prototype phase (required)

The planning review must include **visual exploration**, not just written layout descriptions. The goal is to let the user evaluate the look, information hierarchy, window behavior, and multi-controller scalability **before** approving any production refactor.

1. **Inspect references first.** Review all supplied GUI screenshots/mockups and the manual. Identify which elements are user requirements, which are illustrative placeholders, and which should change based on repository or hardware findings. Do not claim to have inspected images that are unavailable.
2. **Produce high-fidelity mockups for all four tabs**: Overview, Configuration, Ramp/Soak, and Logs. Show at least one single-controller and one multi-controller state (e.g. two units / twelve zones), including online/stale/offline statuses and the controller/zone selector. Where feasible also depict a floating/detached live-chart window. Prioritize readable desktop-density layouts, consistent spacing, restrained blue/neutral palette, and clear control-versus-recording distinction.
3. **Choose an available rendering method honestly.** If a supported image-generation tool is available, use it for polished concept images; otherwise build HTML/CSS mockups and render/screenshoot them using available local tooling. SVG diagrams are acceptable for architecture and wireframes, but **do not present SVG-only diagrams as equivalent to high-fidelity GUI screenshots**. If a rendering/screenshot tool is unavailable, save viewable HTML and explain how to preview it. Never claim to have generated or viewed an image that was not actually produced.
4. **Create an isolated, runnable PySide6 + pyqtgraph prototype** using simulated controller data only. It should demonstrate the proposed navigation, selecting controllers and zones, responsive/resizable layout, representative controls, live-looking synthetic plot updates, and (if feasible) detaching/adding chart windows. Clearly label every simulated reading and action; controls must not send real commands or connect to the production service. Prefer a small standalone prototype with an obvious entry point and instructions over a sprawling unfinished implementation.
5. **Keep all design work separate from production code**, e.g. `design/cn616a-gui/` for mockups, screenshots, prototype, and README. Do not edit the existing Tkinter GUI, service, hardware adapters, real configuration, or dependencies in place. If the prototype needs additional packages, use a separate virtual environment or optional requirements file; never silently change the production environment.
6. **Explain the visual decisions** for each tab, including what was changed from the supplied references and why. Discuss display scaling, small-window behavior, table density at 6/12/18+ zones, keyboard accessibility, status colors, and whether dockable chart panels improve the workflow.
7. **Request a design review gate.** Present the mockups and prototype entry point to the user, enumerate unresolved choices, and **stop before production implementation**. The planning document may recommend a final design, but it must not presume approval.

If time/tool constraints prevent producing all four polished screenshots plus the runnable prototype in one pass, explicitly prioritize: (a) Overview and Configuration high-fidelity mockups, (b) the four-tab prototype, (c) Ramp/Soak and Logs polish. Report unfinished deliverables rather than substituting assertions of completion.

## What to investigate in the repository

1. Map the current architecture: entry points, GUI modules, service modules, transport, protocol, polling, command queue, status-file writes, chart/data readers, configuration persistence, CSV/log output, startup/shutdown, packaging and dependencies. Cite **actual file paths, classes, functions and relevant code sections** for every major finding.
2. Verify the state of Steps A and B, and identify any remaining bottlenecks or races. Explain whether a GUI rewrite can proceed independently of service command handling and which service improvements should be done first.
3. Determine whether Qt/PySide6 + pyqtgraph is the best fit, and whether an incremental parallel GUI can use the current service API. Consider Qt model/view tables, signal/slot threading, asynchronous IO boundaries, plot update strategies, detachable dock widgets, persistent layouts, and avoiding UI thread blocking.
4. Audit the **service contract** thoroughly: commands, acknowledgments, idempotency, atomicity, status schema, stale-data detection, multiple clients, error propagation, recording ownership, start/stop transitions, and what happens on connection loss or GUI crash. Identify which changes are necessary, optional, or dangerous.
5. Audit hardware-facing safety: Run vs Idle, zone enable, control modes, setpoint writes, autotune sequence, PID parameters, output shutdown, alarm handling, ramp/soak and controller-wide configuration. **Do not recommend a potentially hazardous control command without verifying hardware semantics.** Where manual/code disagree or are silent, flag hardware testing as a gate.
6. Identify performance and stability risks with 1–N controllers and 6N zones: serial bus bandwidth, poll frequency, plotting buffers, CSV throughput, chart memory, file locking, GUI update rates, and error handling.
7. Examine tests and propose mocked/virtual service and Modbus fixtures, UI smoke tests, integration tests and controlled hardware acceptance tests. Avoid assuming hardware is available during development.

## Required deliverable: a plan, not an implementation

Write a standalone planning document in Markdown, preferably at `docs/plans/cn616a-qt-gui-refactor.md` **only if creating a planning document is appropriate in this workspace**; otherwise provide the complete document in chat. Create the isolated visual artifacts specified above where tooling permits. **Do not edit production runtime code, service code, or production dependencies during this review.**

Structure the document as follows:

1. **Executive recommendation** — recommended architecture and whether to proceed now, after Step B, or in phases.
2. **As-is repository map** — concrete paths, service/GUI boundaries, data and control flows, and Step A/B verification.
3. **Target GUI specification and visual review** — four tabs, annotated high-fidelity mockups and paths to actual generated assets, prototype launch instructions, interactions, state and error displays, single-unit and multi-unit scaling, and explicit departures from the supplied mockups with reasons.
4. **Target architecture and service contract** — module boundaries, UI-to-service API/status schema changes, Qt/pyqtgraph threading and plot design, controller/zone identity model, and migration approach.
5. **Decision matrix** — benefits, costs, risks, alternatives and recommendation for each significant decision, especially **service changes versus GUI-only changes**, rewrite versus adapter, embedded versus detachable charts, and multi-controller support now versus later.
6. **Phased implementation roadmap** — small reviewable milestones, dependency ordering, likely file-level changes, test and acceptance criteria, rollback/compatibility plan, and approximate effort/risk estimates with assumptions.
7. **Safety and correctness checklist** — activation/deactivation, autotune, PID writes, mode transitions, alarms, recording, lost communication and restart recovery.
8. **Open questions and hardware validation gates** — separate blockers from decisions that can safely be deferred.
9. **Visual artifact manifest and approval gate** — paths to each generated image/HTML mockup, prototype code, separate optional requirements, launch instructions, simulation limitations, and a checklist of design decisions for user approval.
10. **Ready-to-use implementation prompts** — draft concise, sequential, repo-specific instructions for a coding agent for each phase. These should instruct the agent to inspect relevant files, make scoped changes, test, and stop at defined review gates. **Do not execute these prompts.**

Be candid about tradeoffs and challenge questionable assumptions. Prefer a maintainable architecture and a stable control service over exact visual fidelity. Clearly distinguish **verified repository facts**, **manual-derived facts**, **design proposals**, and **assumptions requiring validation**. If information is missing, identify precisely what needs to be supplied or tested rather than inventing it.

**Completion rule:** Finish by listing (1) what was verified in the repository, (2) what visual artifacts were actually created and where, (3) any prototype limitations, (4) service risks and unresolved hardware questions, and (5) what the user must approve before implementation. Do not start executing the implementation prompts.
