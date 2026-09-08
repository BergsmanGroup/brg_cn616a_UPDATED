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

The service owns the serial connection and runs the polling loop. TCP commands are accepted by a daemon listener thread, queued, and executed by the service loop. This prevents the CLI and GUI from competing for the COM port.

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

`CN616AService` owns scheduling, persistence, command handling, error state, and connection lifecycle.

At startup it:

1. Loads `cn616a_service_config_state.json` when available.
2. Applies command-line overrides for the current process.
3. Starts the TCP command server.
4. Connects to the configured serial port.
5. Performs an initial configuration poll to seed the setpoint cache.
6. Enters the polling loop.

The loop drains pending commands and then schedules four types of work:

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

The defaults in the current code are size thresholds of 10 MB for telemetry and analysis, 5 MB for config and ramp/soak, and 2 MB for service configuration, with up to 10 current/archive files per stream. The thresholds are configurable in `ServiceConfig`; they are not the multi-gigabyte values used by some older deployments.

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

Reads controller configuration and service settings. Service settings are patched through `set_service_config`, then persisted by the service. Viewer settings include chart history duration, line width, colors, and visibility of absolute setpoint, autotune setpoint, and MAE lines.

### Ramp/Soak tab

Uses the service's on-demand ramp/soak read path. The register map describes 20 segments per supported PID/profile zone, with setpoint, slope in degrees per minute, and hold time in hours.

### Chart tab

The chart is loaded lazily when selected. It reads historical telemetry and analysis data, breaks visible lines across time gaps, and can display PV, setpoint, autotune setpoint, and MAE. On Python 3.13 or newer, the tab is disabled unless `--allow-unsafe-chart` or `CN616A_GUI_ALLOW_UNSAFE_CHART=1` is supplied because of a known TkAgg/Matplotlib crash risk. The recommended solution is Python 3.12.

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

Supported operations are:

| Operation | Purpose |
|---|---|
| `ping` | Health check |
| `get_status` | Connection, last telemetry time, last error, cycle time, enabled zones, and service config |
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

The tests cover:

| Test area | Coverage |
|---|---|
| CLI | Argument parsing, zone parsing, and command-message construction |
| Driver helpers | Float/hex conversion, enum construction, register block extraction |
| Service commands | Dispatch, service-config patches, and error propagation with mocked controller behavior |
| Service utilities | Frequency conversion, stable hashes, atomic JSON writes, rotation, and config round trips |
| GUI/state reader | Safe JSON reads, zone-name normalization, and chart-version guard behavior |
| Chart gaps | NaN insertion for time gaps in plotted histories |

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

Commands are serialized with polling work and normally have a five-second completion timeout. A timeout can indicate a blocked serial transaction, an unresponsive controller, or a stopped service. Check `get_status`, the service console, and the rotating service error log; restart the serial connection or service after resolving the physical issue.

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
