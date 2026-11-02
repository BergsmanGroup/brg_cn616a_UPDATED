# CN616A Control System

## White paper and operator guide

This repository is a Windows-oriented control and observability system for the Omega CN616A temperature controller. It places one Python service in charge of the Modbus RTU serial port, polls controller data on scheduled cadences, exposes commands over a local TCP JSON Lines endpoint, and writes durable snapshots and histories for the GUI and other consumers.

The system is designed around a simple rule: **only the service talks to the controller**. The GUI and CLI never open the COM port directly. They read service-produced state files and send commands to the service.

> Important: the code and supplied batch files are written for Windows. The supported GUI runtime is Python 3.12 because the chart uses TkAgg/Matplotlib. The chart is disabled by default on Python 3.13 and newer.

## Contents

- [Purpose and operating model](#purpose-and-operating-model)
- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Installation](#installation)
- [Starting and stopping the system](#starting-and-stopping-the-system)
- [Using the CLI](#using-the-cli)
- [Service configuration](#service-configuration)
- [State files and logs](#state-files-and-logs)
- [Controller protocol and register map](#controller-protocol-and-register-map)
- [GUI](#gui)
- [JSON Lines protocol](#json-lines-protocol)
- [Testing and development](#testing-and-development)
- [Troubleshooting](#troubleshooting)
- [Operational and safety notes](#operational-and-safety-notes)

## Purpose and operating model

The CN616A is a Modbus RTU device. This project turns its register map into a higher-level application with three user-facing capabilities:

1. **Monitoring**: process value, setpoint, output, control mode, loop status, sensor state, alarms, and equilibrium analysis.
2. **Control**: set absolute setpoints, select PID or ON/OFF control, select standard or ramp/soak mode, and start or stop autotune.
3. **Persistence and review**: write the latest values as JSON snapshots and append historical events as JSONL logs.

A normal session looks like this:

```text
CN616A controller
        ^
        | Modbus RTU over COM port
        v
cn616a_service.py
   |             |
   | TCP JSONL   | JSON snapshots and JSONL history
   v             v
CLI / GUI     logs/ directory
```

The service owns the serial connection and runs the polling loop. A bounded TCP listener accepts concurrent clients, queues commands, and lets the service loop execute them in FIFO order. Only that loop accesses the controller; it checks one queued command at safe boundaries between complete polling operations, so serial transactions never overlap and a command burst cannot indefinitely starve polling or scheduled run stops.

## Architecture

### Driver: `py/cn616a.py`

`CN616A` is the map-driven Modbus driver. It is responsible for:

- Creating and closing the `pymodbus` RTU client.
- Loading `cn616a_register_map.json`.
- Reading telemetry, configuration, and ramp/soak profiles.
- Translating register-map enum values into names such as `PID_CONTROL` and `RAMP_SOAK`.
- Encoding and decoding 16-bit values and big-endian IEEE-754 float32 values.
- Applying retries and the short quiet period required after writes.
- Splitting large reads into safe blocks; PV reads avoid requests larger than the device-safe range.

The driver exposes stable application methods rather than making the service know individual register addresses:

- `read_telemetry(zones)`
- `read_config(zones)`
- `read_rampsoak_all(zones)`
- `set_sp_abs(zone, value_c)`
- `set_control_method(zone, method)`
- `set_control_mode(zone, mode)`
- `set_autotune_setpoint(zones, setpoints)`
- `start_autotune(zones)` and `stop_autotune(zone)`

### Service: `py/cn616a_service.py`

`CN616AService` owns scheduling, persistence, command handling, error state, and connection lifecycle. Command responses and service logs report queue wait, controller-operation duration, and end-to-end service execution time separately.

At startup it:

1. Loads `cn616a_service_config_state.json` when available.
2. Applies command-line overrides for the current process.
3. Starts the TCP command server.
4. Connects to the configured serial port.
5. Performs an initial configuration poll to seed the setpoint cache.
6. Enters the polling loop.

The loop services at most one queued command at each safe boundary, checks scheduled run stops, and schedules four types of work:

| Work | Default cadence | Output |
|---|---:|---|
| Telemetry | 2 Hz, additionally capped by `--poll` | `cn616a_telemetry_state.json` and telemetry JSONL |
| Configuration | 0.2 Hz | Config snapshot; history only when changed |
| Ramp/soak | Disabled, 0 Hz | On demand unless enabled |
| Analysis | 1 Hz | Equilibrium snapshot; history when analysis changes |

Setting a poll frequency to `0` disables that poller. Telemetry is the exception to the simple frequency rule: its period is the smaller of the configured telemetry period and the `--poll` value.

### CLI: `py/cn616a_cli.py`

The CLI is a thin TCP client. It creates one JSON command, sends it to the service, waits for one JSON response, and prints the decoded response. It does not contain controller register logic.

### GUI: `py/gui/`

The Tkinter GUI reads state files directly for display and uses the same TCP command interface for control and service configuration.

### Optional Qt GUI (in progress)

The Qt GUI is an optional, separate launch path; it does not replace the Tk GUI
or start the service. Install its dependencies and launch the repository-only
offline replay from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-qt.txt
.\bat\gui_qt_start.bat --offline-replay
```

The Qt shell provides Overview, Configuration, Ramp/Soak, and Logs tabs. The
un-maximized main window opens at 1500 x 1400 px; floating chart windows remain
1000 x 560 px. The offline replay shows a prominent **OFFLINE REPLAY / NO
HARDWARE** label. Its
generated service-shaped files live in a temporary directory and use synthetic
baseline data; analysis is mock-derived and outcomes are mock-only. The Overview
presents a sortable six-zone table, focused-zone readbacks and charts,
source timestamps, age, and health. The replay is static, so its values become
visibly stale after five seconds rather than appearing live. Zone update controls
are enabled only for the selected, verified target; in replay they exercise
simulated success, rejection, partial, and unknown outcomes without sending a
service or hardware request. The Overview recording card labels recording as
data collection, not controller heating or profile execution. In replay,
start/stop and scheduled-stop requests create only a simulated recording under
the session's temporary directory; the path entered in the form is not written.
The replay can also show a simulated recovered interruption.
Selecting a zone or tab does not apply edits.
Configuration now includes controller/system readbacks, service settings,
retention estimates, and explicit Apply/connect/disconnect/refresh actions.
Stored TCP endpoint fields do not switch the GUI's already selected endpoint.
The Ramp/Soak tab supports reading and editing the 20-row profile, active
segment count, and control mode through the adapter. Profile start/stop and
autotune actions require target-specific confirmation and show unresolved
outcomes separately from available status readbacks. Offline actions are
simulated only. Logs provides source/category selection, bounded background
tails for service diagnostics and JSONL records, a recording-status summary,
recent service activity, and uncaught GUI exceptions. Initial loads read at
most 512 KiB, subsequent refreshes at most 128 KiB, and the view retains at
most 5,000 rows; missing, malformed, and oversized data is surfaced. Without
an explicit mode, the shell is disconnected and read-only and does not probe a
service endpoint. Offline replay never falls back to a normal service endpoint.

An explicit service mode is available for later user testing against an already
running service. Set these PowerShell environment variables to the endpoint and
state/log directory you have independently verified, then opt in:

```powershell
.\bat\gui_qt_start.bat --service --host $env:CN616A_SERVICE_HOST --port $env:CN616A_SERVICE_PORT --logs-dir $env:CN616A_SERVICE_LOGS
```

To use the Qt GUI as a **live read-only viewer** alongside an already-running
service (for example, one that owns COM4), add `--read-only`:

```powershell
.\bat\gui_qt_start.bat --service --read-only --host 127.0.0.1 --port 8765 --logs-dir .\logs
```

This mode reads live service snapshots and recording status but disables the
zone editors and rejects all mutating requests (including recording and schedule
commands) in the adapter itself. It does not start a service or open a serial
port. The status banner identifies **READ-ONLY MODE**. Without
`--read-only`, explicit service mode is writable when the service reports a
connected controller and fresh, matching identity. In that mode the Overview's
Apply action sends the ordered `set_sp_abs`, `set_control_method`, and
`set_autotune_setpoint` requests through that existing service to the selected
zone. Review the displayed controller identity and per-operation result before
proceeding; a service acknowledgment is not controller readback. Live writes
and controller behavior require user-only hardware validation. Recording
controls use `start_run`, `stop_run`, `get_run_status`, and `set_run_schedule`;
the service remains authoritative if the GUI is hidden or closed. Schedule
entry is interpreted as Pacific time and converted to an offset-bearing ISO
timestamp. Recording only mirrors service logs; it does not start or stop
heating or profile execution. Use
`.\bat\gui_start.bat` for the existing operational Tk application. The
[Step 05 report](docs/reports/qt-gui-step-05.md) has the offline control
walkthrough and test evidence. The [Step 06 report](docs/reports/qt-gui-step-06.md)
has the recording and schedule walkthrough and contract-test evidence.
The [Step 10 report](docs/reports/qt-gui-step-10.md) documents Logs and
diagnostics; the [Step 11 reliability report](docs/reports/qt-gui-step-11.md)
records the measured offscreen exercise and remaining UI/hardware gates.

The feature list below describes the existing Tk GUI; Qt migration is in
progress.

The tabs are:

- **Telemetry**: live zone values plus setpoint, control method, and autotune commands.
- **Configuration**: service cadence, zone selection, viewer preferences, and displayed controller configuration.
- **Ramp/Soak**: on-demand 20-segment profile view/editor surface.
- **Chart**: Matplotlib time series for PV, setpoint, autotune setpoint, and mean absolute error.

The GUI refresh rate is loaded from `gui_refresh_hz` in the persisted service configuration when that value is positive. Closing the GUI does not stop the service.

## Repository layout

```text
cn616a_register_map.json       Device register map and enum definitions
requirements.txt               Python dependencies
bat/                           Supported Windows setup and launch wrappers
py/cn616a.py                   Modbus RTU driver
py/cn616a_service.py           Single-owner polling and command service
py/cn616a_cli.py               TCP JSONL CLI client
py/gui/                        Tkinter panels, chart, and state readers
tests/                         Unit tests for driver, service, CLI, and GUI helpers
logs/                          Runtime state, history, and error logs
```

The `logs/` directory may already contain historical runtime data. Treat those files as operational data, not source code.

## Installation

### Prerequisites

- Windows.
- Python 3.12 available as `py -3.12`, or installed at the standard per-user Python 3.12 path.
- An accessible CN616A serial connection and the correct COM port.
- The controller's Modbus unit/slave ID, normally `1`.

### Create the virtual environment

Run from the repository root:

```powershell
.\bat\venv_setup.bat
```

The script removes and rebuilds `.venv`, upgrades `pip`, and installs:

- `pymodbus==2.5.3`
- `pyserial==3.5`
- `matplotlib>=3.5.0`

The exact Modbus package version matters because the driver uses the `pymodbus 2.5.3` synchronous client API and its `unit=` convention.

The wrappers fail fast when `.venv` is missing. Rerun setup if the environment was removed or created with the wrong Python version.

## Starting and stopping the system

### Start the service explicitly

```powershell
.\bat\service_start.bat --port COM4 --unit 1 --tcp-port 8765 --verbose
```

The service defaults to TCP host `127.0.0.1`, TCP port `8765`, telemetry cap `0.5` seconds, and the repository `logs` directory.

On later launches, `service_start.bat` can reuse the last successfully connected serial port and TCP endpoint from `logs\cn616a_service_config_state.json`:

```powershell
.\bat\service_start.bat
```

The batch wrapper also accepts the environment variables `CN616A_SERIAL_PORT`, `CN616A_SERVICE_HOST`, and `CN616A_SERVICE_TCP_PORT`. An explicit `--port` argument takes precedence over the saved serial port. The service wrapper refuses to start a second process when the configured TCP endpoint is already accepting connections.

### Start the GUI

```powershell
.\bat\gui_start.bat
```

The GUI wrapper loads the saved serial and TCP settings, starts the service in a separate console if the endpoint is not already up, waits briefly, and then starts the GUI. The GUI itself does not own the serial connection.

Useful alternatives:

```powershell
.\bat\gui_start.bat --logs-dir .\logs --refresh-interval 1.5
.\bat\gui_start.bat --debug
.\bat\gui_start.bat --allow-unsafe-chart
```

`--allow-unsafe-chart` is intended for runtimes where chart support is disabled by the Python-version guard. Python 3.12 remains the recommended runtime.

### Stop the service cleanly

```powershell
.\bat\service_stop.bat
```

This sends the `shutdown` command to the configured TCP endpoint. A Ctrl+C in the service console also causes a clean serial close.

### Direct Python invocation

The wrappers are recommended because they establish the repository root and virtual-environment paths. For debugging, the equivalent direct forms are:

```powershell
.\.venv\Scripts\python.exe py\cn616a_service.py --port COM4 --unit 1 --tcp-port 8765 --verbose
.\.venv\Scripts\python.exe py\cn616a_cli.py --host 127.0.0.1 --port 8765 get_status
.\.venv\Scripts\python.exe py\gui\main_gui.py --logs-dir .\logs
```

## Using the CLI

Show all CLI help:

```powershell
.\bat\cli.bat --help
```

Check the service:

```powershell
.\bat\cli.bat --host 127.0.0.1 --port 8765 ping
.\bat\cli.bat --host 127.0.0.1 --port 8765 get_status
```

The command response is printed as a Python-style dictionary representation of the decoded JSON response.

### Controller commands

Set an absolute setpoint in the controller's engineering units:

```powershell
.\bat\cli.bat set_sp --zone 1 --value 80
```

Select PID or ON/OFF control:

```powershell
.\bat\cli.bat pid --zone 1
.\bat\cli.bat onoff --zone 1
```

Select standard or ramp/soak control mode. The value must match a register-map enum name, for example:

```powershell
.\bat\cli.bat set_mode --zone 1 --mode STANDARD_CONTROL
.\bat\cli.bat set_mode --zone 1 --mode RAMP_SOAK_1_STOP
```

Configure and run autotune:

```powershell
.\bat\cli.bat autotune_sp --zones 1 --values 80
.\bat\cli.bat start_autotune --zones 1
.\bat\cli.bat stop_autotune --zone 1
```

Batch autotune setpoints are supported when the number of values matches the number of zones:

```powershell
.\bat\cli.bat autotune_sp --zones 1,2,3 --values 80,85,90
```

The CLI deliberately rejects `auto` for `autotune_sp`; autotune writes require explicit zones.

### Read operations

```powershell
.\bat\cli.bat read_config
.\bat\cli.bat read_rampsoak
```

These commands force an immediate service-side read and update the corresponding state file. The response's `changed` field indicates whether the read differed from the previous content for change-filtered history purposes.

### Service configuration commands

Read current settings:

```powershell
.\bat\cli.bat get_service_config
```

Patch only the settings that need to change:

```powershell
.\bat\cli.bat set_service_config --telemetry-hz 4 --config-hz 0.2 --zones 1,2,3
.\bat\cli.bat set_service_config --zones auto --analysis-hz 1 --equilibrium-window-s 30 --equilibrium-threshold-c 0.25
```

Supported CLI service patches include telemetry, configuration, ramp/soak, and analysis frequencies; zone selection; JSONL flush behavior; and equilibrium window/threshold. The GUI can also update viewer settings and other persisted configuration fields.

## Service configuration

`ServiceConfig` is persisted in `logs/cn616a_service_config_state.json`. Important fields include:

| Field | Meaning | Default |
|---|---|---:|
| `telemetry_hz` | Telemetry polling rate before `--poll` cap | `2.0` |
| `config_hz` | Configuration polling rate | `0.2` |
| `rampsoak_hz` | Periodic ramp/soak polling rate | `0.0` |
| `analysis_hz` | Equilibrium-analysis rate | `1.0` |
| `gui_refresh_hz` | GUI refresh frequency | `2.0` |
| `zones_mode` | `auto` or `list` | `auto` |
| `zones_list` | Explicit enabled zones | `1..6` |
| `equilibrium_window_s` | Analysis lookback window | `30.0` |
| `equilibrium_threshold_c` | Maximum average absolute error | `0.25` |
| `flush_each_line` | Flush each JSONL append | `true` |
| `telemetry_retention_s` | User-facing telemetry retention duration; GUI derives file count | `86400` |
| `analysis_retention_s` | User-facing analysis retention duration; GUI derives file count | `86400` |
| `last_serial_port` | Last successful COM port | empty initially |
| `last_serial_params` | Baud, parity, stop bits, byte size, timeout | 115200/N/1/8/1.0 |
| `last_tcp_host`, `last_tcp_port` | Last service endpoint | empty/0 initially |

Zone selection is normalized to zones 1 through 6 by `effective_zones()`. Although the register map describes some 12-zone areas, PID and ramp/soak operations are implemented for zones 1 through 6. Use explicit zone lists when you need to avoid polling inactive or unsupported zones.

Configuration precedence is:

1. `ServiceConfig` defaults.
2. The saved service configuration state file, when valid.
3. Service command-line overrides for the current launch.
4. Runtime patches sent through `set_service_config`.

A runtime patch is persisted as a new service-config snapshot and log event. Connection details are updated after a successful serial connection.

## State files and logs

All runtime outputs go to the service `--out-dir`, defaulting to repository `logs/`. State files are complete latest snapshots written atomically through a temporary file and `os.replace`. JSONL files are append-only records, rotated by size and pruned to the configured archive count.

### Run recovery after service interruption

When a run is active, the service persists `cn616a_active_run_state.json` in the service output directory. It records the run directory, start time, and timestamp of the latest telemetry record mirrored into that run. A normal service shutdown or a crash therefore leaves enough information for the next service instance to resume mirroring into the same run directory. An explicit `Stop Run` still closes the run and clears its recovery state.

When the GUI reconnects to a service that recovered an active run, it displays a warning with the timestamp of the last recorded data, converted to Pacific time. This avoids presenting an inferred downtime interval as exact; the user can compare that timestamp with the current time or the next collected record. If the service crashed before it could write its shutdown marker, the run is still resumed and the last recorded data timestamp remains available.

### Latest-state snapshots

| File | Contents |
|---|---|
| `cn616a_telemetry_state.json` | Timestamp, unit, port, enabled zones, device telemetry, per-zone PV/PID/control data, and bitmaps |
| `cn616a_config_state.json` | System configuration and per-zone alarm, scaling, calibration, and sensor status data |
| `cn616a_rampsoak_state.json` | Twenty ramp/soak segments per enabled zone when read |
| `cn616a_service_config_state.json` | Service settings, viewer settings, connection settings, and the event that produced the snapshot |
| `cn616a_analysis_state.json` | Per-zone average absolute error, equilibrium flag, point count, threshold, and lookback window |

A typical telemetry snapshot is shaped like this:

```json
{
  "ts": "2026-09-08T12:34:56.789-04:00",
  "unit": 1,
  "port": "COM4",
  "zones": [1, 2, 3, 4, 5, 6],
  "telemetry": {
    "device": {"temperature_scale": "DEGREE_C"},
    "bitmaps": {},
    "zones": {
      "1": {
        "pv_c": 78.4,
        "sp_abs_c": 80.0,
        "out_pct": 42.0,
        "control_method": "PID_CONTROL",
        "control_mode": "STANDARD_CONTROL",
        "loop_status": "STANDARD"
      }
    }
  }
}
```

The exact device response may contain additional fields or `null` values when an individual register read fails.

### Historical JSONL logs

The service writes:

- `cn616a_telemetry_log.jsonl`: each telemetry poll.
- `cn616a_config_log.jsonl`: configuration changes only, using a stable content hash.
- `cn616a_rampsoak_log.jsonl`: ramp/soak changes only.
- `cn616a_service_config_log.jsonl`: startup, connection, disconnection, and configuration events.
- `cn616a_analysis_log.jsonl`: analysis changes only.

The defaults in the current code are size thresholds of 10 MB for telemetry and analysis, 5 MB for config and ramp/soak, and 2 MB for service configuration. The GUI exposes telemetry and analysis as a retention duration plus maximum file size; it estimates the required file count as:

```text
estimated records = retention seconds × polling Hz
estimated data = estimated records × average JSONL record size
file count = ceiling(estimated data / maximum file size)
```

The average record size is measured from existing log lines when available, with a conservative fallback for a new installation. Telemetry estimates use the telemetry polling rate. Analysis estimates use the analysis polling rate as an upper bound because analysis records are written only when the calculated result changes. The GUI also displays estimated data and worst-case capacity (`file count × maximum file size`).

The derived file count is persisted as `max_telemetry_log_files` or `max_analysis_log_files`, while the requested duration is persisted separately. If polling frequency, enabled zones, or log shape changes materially, reapply the service settings to refresh the estimate.

Rotation renames the active file with a timestamp such as `cn616a_telemetry_log_20260908_123456.jsonl` and removes the oldest archives beyond the configured limit.

### Error logs

- `cn616a_service_error.log` plus five rotating backups.
- `cn616a_gui_error.log` plus five rotating backups.
- `cn616a_gui_fault.log` for Python fault-handler output and low-level GUI crash diagnosis.

Use `--verbose` on the service to print connection and telemetry-cycle information to its console while persistent errors continue to go to the service error log.

## Controller protocol and register map

The register map is in `cn616a_register_map.json`; the driver does not hardcode the main register layout. It describes an Omega CN616A using:

- Modbus RTU.
- Holding-register reads with function code 3.
- Single-register writes with function code 6 where appropriate.
- Multi-register writes with function code 16 for 32-bit values.
- 40000-based controller indices as used by this project.
- Big-endian register order and most-significant-word-first 32-bit values.

The map defines these main regions:

| Region | Coverage | Purpose |
|---|---|---|
| System | `0x0001` onward | Firmware, units, sensor type, address, active zones, system state, alarms |
| Temperature PV | `0x0100`, step `0x0002` | Per-zone float32 process values, up to 12 described zones |
| Sensor status | `0x0180` and bitmaps | Per-zone status plus sensor/alarm bitmaps |
| Zone registers | `0x0200` through `0x0780` | Alarm setpoints, alarm modes, scaling, and calibration |
| PID registers | `0x0800` through `0x0a80` | Setpoints, gains, output, control method/mode, autotune, loop state for zones 1-6 |
| Ramp/soak profiles | `0x1000` through `0x1500` | 20 segments, each with setpoint, slope, and hold time for zones 1-6 |
| User calibration | `0x1d7c` onward | RTD offset per zone |

A float32 or 32-bit value occupies two registers. The controller's split-register rules are significant: a read must begin at the lower-numbered register, and a write must begin at the base register and include the second register. The driver uses block and pair operations to preserve that ordering.

The map also supplies enum names used by the API, including:

- `control_method`: `ON_OFF_CONTROL`, `PID_CONTROL`
- `control_mode`: `STANDARD_CONTROL`, `RAMP_SOAK_1_STOP`, `RAMP_SOAK_2_HOLD`
- `loop_status`: `STOPPED`, `IDLE`, `STANDARD`, `RAMP_SOAK`, and autotune stages
- `sensor_status`: `VALID`, out-of-range, short-circuit, and open-circuit states
- `segment_state`: `IDLE`, `RAMPING`, `SOAKING`, `HOLDING`

If the map is moved, pass `--map` to the service. Relative map paths are resolved against the repository root by the service. The driver also searches common locations, including the current directory, `py/`, and the repository root.

## GUI

The GUI is intentionally a service client and state viewer rather than a second controller driver.

### Telemetry tab

Shows per-zone live values from `cn616a_telemetry_state.json`. The command panel sends setpoint, control-method, control-mode, and autotune commands through TCP. It does not write the state files itself; the next service poll reflects controller changes.

### Configuration tab

Reads system/zone configuration and available PID readbacks from service
snapshots. Service settings are patched through `set_service_config`, then
persisted by the service. Viewer settings include chart history duration, line
width, colors, and visibility of absolute setpoint, autotune setpoint, and MAE
lines. Connection actions use the currently selected adapter endpoint; saved
host/port values do not switch this GUI or prove listener/serial reconfiguration.

### Ramp/Soak tab

Uses the service's on-demand ramp/soak read path. The register map describes 20 segments per supported PID/profile zone, with setpoint, slope in degrees per minute, and hold time in hours.

### Chart tab

The chart is loaded lazily when selected. A bounded background reader incrementally ingests historical telemetry and analysis data, including rotated logs, and requests a full reload only after truncation/replacement or when a longer history window is selected. Rendering is limited to the visible zone, with one active and one replaceable pending render request. Chart history keeps a bounded in-memory window, preserves extrema and time gaps while downsampling, and reports read errors. It can display PV, setpoint, autotune setpoint, and MAE. On Python 3.13 or newer, the tab is disabled unless `--allow-unsafe-chart` or `CN616A_GUI_ALLOW_UNSAFE_CHART=1` is supplied because of a known TkAgg/Matplotlib crash risk. The recommended solution is Python 3.12.

## JSON Lines protocol

The service accepts TCP connections on the configured host and port. Each request is one UTF-8 JSON object terminated by a newline. Each response is one JSON object followed by a newline.

Example request:

```json
{"id":"a1b2c3d4","op":"set_sp_abs","zone":1,"value_c":80.0}
```

Example response:

```json
{"id":"a1b2c3d4","ok":true}
```

Every command may include an `id`; the service echoes it. The service waits up to `timeout_s` from the request, defaulting to five seconds, for command completion.

The listener serves up to eight active TCP clients concurrently, accepts request
lines up to 64 KiB, and uses a FIFO command queue bounded to 64 pending requests.
Requests with an invalid timeout or full queue are rejected before execution.
`timeout_s` must be greater than zero and at most 30 seconds. A timeout while a
command is still queued cancels it and returns `state: "not_executed"`. Once
execution has started, a timeout returns `state: "unknown"`; the operation may
still finish, and clients must verify controller state before retrying. Client
disconnects cancel requests that have not started; an in-progress operation is
not cancelled or retried.

Completed responses include a `timing` object with `queue_wait_ms`,
`controller_io_ms`, and `end_to_end_ms`. These are service-side measurements,
not estimates of physical controller response or GUI repaint time. A complete
poll/driver operation is not interrupted, so a long individual operation can
still delay the next command; hardware timing has not been measured.

Supported operations are:

| Operation | Purpose |
|---|---|
| `ping` | Health check |
| `get_status` | Connection, last telemetry time, last error, cycle time, last controller-operation duration, enabled zones, and service config |
| `get_service_config` | Return persisted/runtime service configuration |
| `set_service_config` | Apply a JSON object patch |
| `connect_serial` | Connect when disconnected |
| `disconnect_serial` | Close the serial connection |
| `restart_serial` / `refresh_connection` | Rebuild the driver and reconnect |
| `reload_register_map` | Rebuild the driver using the configured map |
| `shutdown` | Stop the service loop and close serial |
| `set_sp_abs` | Write one absolute setpoint |
| `set_control_method` | Write a control-method enum |
| `set_control_mode` | Write a control-mode enum |
| `set_autotune_setpoint` | Write one or multiple autotune setpoints |
| `start_autotune` / `stop_autotune` | Control autotune |
| `read_config` | Force a configuration poll |
| `read_rampsoak` | Force a ramp/soak poll |

The default host is `127.0.0.1`, which keeps the control surface local to the Windows machine. There is no authentication or encryption layer in this protocol. Do not bind it to a network interface or expose the port beyond a trusted host without adding an appropriate security boundary.

## Testing and development

Run the test suite from the repository root after setup:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

### GUI responsiveness baseline

The GUI baseline runner measures the actual Tk application using synthetic
state/history files in temporary directories and a fake JSON Lines endpoint
bound only to `127.0.0.1`. It does not start or connect to
`cn616a_service.py`, open a serial port, issue commands to a controller, or
modify source logs. It automatically exercises Tk tab, combobox, entry, and
button callbacks; the folder-picker callback uses a fake chooser rather than
opening the native Windows dialog.

### Portable performance check (use a separate test machine)

The entire simulation is repository-contained; no existing logs, hardware or
running service are required. In a fresh clone with Python 3.12/Tk installed,
create a new environment (do not rebuild an environment being used by a live
service):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\bat\responsiveness_test.bat quick
.\bat\responsiveness_test.bat full
```

There are only two batch test options:

- **quick** (also the no-argument default): two large-history, chart-open
  sessions with normal and delayed fake service replies, 30 seconds each.
- **full**: all 24 combinations of three chart states (unopened/open/hidden),
  two history sizes (small/one hour), and four service modes
  (normal/delayed/disconnected/error), 60 seconds each, roughly 25 minutes.

Both request 20 simulated button actions per session. Full increases duration
and coverage, not the number of distinct raw snapshots. The launcher never calls
`gui_start.bat`, which would start the real service. Run from a terminal so
results remain visible. Presets and data tools live in Python, not batch.
On other platforms, invoke `tests\responsiveness.py quick` or `full` using
Python 3.12/Tk; full platform parity is not certified.

Terminal output highlights heartbeat p95/p99/max (targets <100/<250 ms),
button feedback p95 (target <150 ms), sample counts, slowest application
callback, JSONL read amplification, startup, CPU and process peak RAM.
Missing chart/data and insufficient sampling are explicitly marked rather
than reported as success. Feedback means time to the next Tk idle opportunity,
not a measured pixel repaint. The first two seconds of heartbeat samples are
reported separately as warm-up in JSON. Use full for better sample coverage;
very slow scenarios can still have insufficient samples. Measurements include
instrumentation overhead; targets are workload/machine-specific.

The GUI sends every service request through one bounded worker shared by its
panels; status polls are coalesced and lower priority than operator actions.
Command feedback distinguishes confirmed success/failure, requests not sent,
and unknown outcomes (which must be verified before retrying). Only the visible
tab refreshes, except Run status/recovery polling, which remains active while
Run is hidden. Configuration retention estimates sample bounded portions of
current and rotated logs off the Tk thread; limited, fallback, or failed
samples are labeled uncertain and do not reduce configured file retention.
Service state snapshots are read once by a shared change-aware background
reader, bounded to 4 MiB per file; panels display snapshot age and stale or
read-error status rather than silently presenting unavailable data as current.
If the native Windows folder picker raises a Tcl error, the Run panel reports
it and leaves manual directory entry available. The synthetic harness replaces
the native picker, so it does not certify the real Windows dialog.

Data tools are separate Python commands, not batch test flags:

```powershell
.\.venv\Scripts\python.exe tests\responsiveness.py verify-data
.\.venv\Scripts\python.exe tests\responsiveness.py summarize C:\path\to\report.json
```

`verify-data` checks integrity, builds/measures the temporary workload, and
reports compressed size **without a GUI or network**. `summarize` prints
refined metrics from an existing JSON report, also without a GUI or network.
Both work even when Tk/Matplotlib GUI dependencies cannot be loaded (data
verification still requires the driver dependencies). Chart runs respect the
existing Python-version safety guard, including ignoring an inherited unsafe
chart override; Python 3.12 is recommended.

### Immutable input and data size

The CN616A transmits **Modbus RTU register responses, not JSON**. Existing
service logs are application-specific representations, not raw wire captures.
The [v1 fixture package](tests/fixtures/cn616a-baseline-v1/README.md) preserves
CRC-valid request/response frames, raw register words, relative sample times,
a frozen device register map, and SHA-256 fingerprints. It is **synthetic,
not captured from the connected unit**. Keep v1 unchanged; add a new version
for new inputs. Current driver decoding and temporary service/GUI JSON
envelopes are disposable adapters; the mock analysis is not device output.

| Data | Quantity |
|---|---|
| Canonical raw reference | 16 six-zone snapshots at 500 ms spacing, plus static register responses |
| Stored reference gzip | **3,823 bytes** (33,272 bytes uncompressed); lossless, immutable |
| Default runtime workload | **7,200 telemetry + 7,200 derived analysis rows**, one hour at 2 Hz |
| Expanded runtime JSONL | **21,475,350 bytes** with this adapter |
| Equivalent gzip of expanded JSONL | **561,226 bytes**, about **38.3x smaller** |

The small eight-second seed repeats with rebased timestamps. It is sufficient
to generate this controlled one-hour workload, not to represent every process
trajectory or an hour of physical observations. Only the compressed seed and
code belong in Git. The GUI reads locally generated **uncompressed** JSONL for
meaningful performance timing. Exact expanded sizes may change with future
adapters; `verify-data` measures them. Report version 2 pins the input hash;
earlier reports used a different synthetic JSON workload and are not
directly comparable.

### Optional new raw reference (not part of quick/full)

Raw capture is implemented in `tests\baseline_reference.py` and called through
the testing entry point. It sends only unit-1 FC03 reads and stores actual
response bytes with CRCs, relative sample times, the frozen register map,
gzip compression and hash manifests. It never changes controller settings,
starts/stops a service, or discovers a port from production settings.

**Do not use it while the live service owns the controller.** Use a separate
controller, or arrange downtime and manually stop its service first.
`--controller-offline` explicitly acknowledges exclusive controller ownership;
it does not stop anything. Serial defaults are 115200 baud, 8N1. Match the
controller's baud rate explicitly if different. A busy port, timeout, exception
response or invalid CRC fails capture rather than substituting synthetic data.

```powershell
.\.venv\Scripts\python.exe tests\responsiveness.py capture --port COM4 --controller-offline --duration-seconds 60 --output-dir tests\fixtures\cn616a-baseline-v2
.\.venv\Scripts\python.exe tests\responsiveness.py quick --reference-dir tests\fixtures\cn616a-baseline-v2
```

The destination must be new; v1 is never overwritten. Capture is separate from
the batch launcher and is never run automatically. It records config/profile
registers once, followed by six-zone telemetry snapshots targeting 2 Hz (slower
if reads take longer). This is not an exhaustive process/state recording.
Replay uses captured values but fixed workload timestamps and a fake service,
not physical response timing. New raw data files are Git-eligible; review,
commit and push a new corpus before sharing it between machines.

By default, the large fixture contains one hour of six-zone telemetry and
analysis at 2 Hz, split across rotated JSONL files, with new synthetic records
appended while the GUI runs. To include existing history, the internal runner's
optional `--copy-logs-from` argument copies recognized JSONL history into the temporary
fixture; the source directory remains read-only. JSON results are saved to a
timestamped file in the OS temporary directory unless `--output` is supplied.
The report includes GUI startup, event-loop and per-callback percentiles,
measured JSONL bytes/lines read by callback, repeated button-feedback latency,
fake-service response timing, log inventory, and CPU/peak working-set
measurements when available. Hardware/controller latency is not measured. Use
the existing test suite separately:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test*responsiveness*.py"
```

The tests cover:

| Test area | Coverage |
|---|---|
| CLI | Argument parsing, zone parsing, and command-message construction |
| Driver helpers | Float/hex conversion, enum construction, register block extraction |
| Service commands | Dispatch, service-config patches, and error propagation with mocked controller behavior |
| Service scheduling | Concurrent fake clients, serialized fake-controller access, FIFO responses, timeout/disconnect lifecycle, and request bounds |
| Service utilities | Frequency conversion, stable hashes, atomic JSON writes, rotation, and config round trips |
| GUI/state reader | Safe JSON reads, zone-name normalization, and chart-version guard behavior |
| Chart gaps | NaN insertion for time gaps in plotted histories |
| GUI baseline infrastructure | Immutable raw input integrity/CRC, offline driver decoding, repeatable six-zone history, fake JSONL service, safe source-log copying, portable CLI and metric summaries |
| Responsiveness presets/capture | Quick/full coverage, alternative corpus replay, exclusive-access acknowledgement, raw FC03 capture and serial failures using fake streams only |

The hardware-facing driver is not exercised against a physical controller by this unit suite. Validate serial settings and controller behavior in a controlled environment before using write commands.

For module-level debugging, run the service with `--verbose`, inspect `get_status`, and read the latest state/error files before changing polling or serial parameters.

## Troubleshooting

### Service will not start

- Confirm `.venv\Scripts\python.exe` exists; rerun `bat\venv_setup.bat` if needed.
- Supply a serial port explicitly: `bat\service_start.bat --port COM4`.
- Check that another process does not own the COM port.
- Check whether the TCP endpoint is already in use. The wrapper treats an accepting endpoint as an already-running service.

### Serial connection fails

- Verify the COM port in Device Manager.
- Confirm the controller's unit ID and serial settings. The default parameters are 115200 baud, no parity, 1 stop bit, 8 data bits, and a 1-second timeout.
- Review `cn616a_service_error.log` and the service console with `--verbose`.
- Use `restart_serial` or stop and restart the service after correcting the physical connection.

### The service is connected but values are missing

- Check `get_status` for `last_error` and `last_telemetry_ts`.
- Inspect `cn616a_telemetry_state.json` for `null` fields and the enabled-zone list.
- Confirm the register map matches the controller firmware and has not been moved without updating `--map`.
- Avoid selecting zones beyond the PID/profile capabilities for control and ramp/soak operations.

### GUI cannot connect or shows stale data

- The GUI reads files and sends TCP commands; it needs both the correct `--logs-dir` and a running service endpoint.
- Confirm that the GUI and service use the same logs directory.
- Check the saved `last_tcp_host`, `last_tcp_port`, and `last_serial_port` in `cn616a_service_config_state.json`.
- Remember that closing the GUI leaves the service running.

### Chart is disabled or crashes

- Use Python 3.12 and recreate the virtual environment.
- On newer Python versions, chart support is intentionally disabled by default.
- Only use `--allow-unsafe-chart` or `CN616A_GUI_ALLOW_UNSAFE_CHART=1` when accepting the runtime risk.
- Review `cn616a_gui_error.log` and `cn616a_gui_fault.log` for startup or Tk callback failures.

### Commands time out

Commands are serialized with polling work and normally have a five-second completion timeout. A timeout marked `not_executed` was cancelled before it began. A timeout marked `unknown` means execution had already started and may have completed; do not retry a write until checking controller state. A timeout can indicate a blocked serial transaction, an unresponsive controller, or a stopped service. Check `get_status`, the service console, and the rotating service error log; restart the serial connection or service after resolving the physical issue.

### Logs grow unexpectedly

Telemetry is logged on every poll. Configuration, ramp/soak, and analysis logs are change-filtered, but a high polling rate or frequently changing values can still produce substantial history. Lower the relevant frequency, adjust rotation settings in `ServiceConfig`, or archive old logs outside the active `logs/` directory.

## Operational and safety notes

- Setpoint, control-mode, and autotune commands write to real controller registers. Use them only when the controller and process are in a known safe state.
- Start with explicit zones for write operations. `auto` is intended for polling selection, not ambiguous control writes.
- Preserve the register map with the deployment; it is part of the driver's runtime contract.
- Back up or archive `logs/` before deleting data needed for analysis or audit.
- Keep the TCP endpoint on localhost unless a separate authenticated and encrypted access layer is provided.
- Treat persisted JSON as operational state. Manually editing it while the service is running can be overwritten by the next atomic snapshot.
- The service is the single serial owner. Do not run an independent Modbus client against the same COM port at the same time.
