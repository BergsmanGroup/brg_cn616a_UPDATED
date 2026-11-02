# Isolated CN616A Qt GUI design prototype

This directory is design-only. The prototype uses synthetic data and has no import, socket, serial, file-state, or command path into the production application.

## Visual mockup

Open `mockups\index.html` in a browser. Use **Preview scale** to switch between one, two, and three simulated controllers, and **Controller view** to filter zones. The tabs cover Overview, Configuration, Ramp / Soak, and Logs. The Overview's floating-chart control opens a separate-window concept. It is an HTML dialog for review, not a separate process.

The HTML mockup is the exact-text reference for the screenshots; all displayed values and actions are illustrative. In particular, "Enable" state, setpoints, PID values, statuses, service health, timestamps, profile progress, and recording actions are not controller state.

Rendered screenshots are in `mockups\`:

- `overview-single.png` - one controller / six zones.
- `overview.png` - two controllers / twelve zones.
- `overview-three-units.png` - eighteen zones with online, stale, and offline examples.
- `overview-floating-chart.png` - optional floating-chart concept.
- `configuration.png`, `ramp-soak.png`, and `logs.png` - the remaining primary tabs.

The generated `concept-board.png` is an AI visual inspiration board. It can contain invented or inaccurate text/details; use the local HTML and rendered screenshots as the exact design reference.

### Dense visual revision

The original concept board remains unchanged as the visual reference. The seven
review screenshots are rendered from the revised HTML at 1440 x 1000:

- Compact left navigation, 30 px zone rows, and a chart directly beneath the
  zone table keep telemetry together instead of leaving space beneath short tables.
- Navy section bars establish structure; teal identifies service health and
  trends, blue identifies focused-zone controls, plum identifies recording, and
  violet identifies profile/diagnostic settings.
- Measured PV is blue, target SP is amber, and MAE/PID values are violet.
  Editable controls have tinted backgrounds; read-only bus examples are gray.
- Online/equilibrium, stale, and offline states retain text as well as green,
  amber, and red treatments. Activation actions are color-coded but remain demo
  notices, not hardware commands.

The Qt prototype uses the same compact typography, color roles, tinted controls,
and table hierarchy, with native horizontal tabs and a resizable workspace/rail
splitter rather than duplicating the HTML sidebar.

## PySide6 + pyqtgraph prototype

Python 3.12 is recommended, matching the repository runtime. Create a separate environment so the production `.venv` and `requirements.txt` are untouched:

```powershell
py -3.12 -m venv .venv-cn616a-design
.\.venv-cn616a-design\Scripts\python.exe -m pip install -r design\cn616a-gui\requirements.txt
.\.venv-cn616a-design\Scripts\python.exe design\cn616a-gui\prototype.py
```

For a headless startup smoke check, set `QT_QPA_PLATFORM=offscreen` for the process and pass `--smoke-test`.

Run the isolated visual/interaction regression checks with:

```powershell
.\.venv-cn616a-design\Scripts\python.exe -m unittest discover -s design\cn616a-gui -p test_prototype.py -v
```

These check that all 18 zone rows fit at 1440 x 900 without table scrolling,
plus filtering, focus selection, offline values/colors, recording indication,
the four tabs, and updates in the detached chart. They require only the design
dependencies, not the production service or controller.

The prototype offers one, two, or three simulated units; local zone selection; synthetic plot updates; a detachable chart window; representative controls; and the four requested tabs. No action reaches the service or controller. The sample uses a widget table for speed of review; production should use Qt model/view and a service adapter.

## Scope and limitations

- Readings and statuses are synthetic, not a controller simulation or Modbus emulator.
- The detached chart is another window in one GUI process, not a worker process.
- No command semantics, safety interlocks, persistence, reconnection, real alarm state, or multi-controller service behavior is implemented here.
- A production app must not expose zone activation/deactivation until the service contract and hardware behavior are verified.
- The prototype is not a replacement for the existing Tkinter GUI and does not change production dependencies.
