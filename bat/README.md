# Batch Script Usage

All commands below should be run from the repository root.

## Python Version

This project is pinned to Python `3.12` for GUI stability.
`bat\venv_setup.bat` creates `.venv` using `py -3.12` (or a `python`, `python3`,
or `python3.12` command that resolves to Python 3.12) and will fail fast if
Python 3.12 is not installed. Python 3.13 and newer are not an equivalent
replacement for the GUI runtime because the chart safety guard disables chart
support there by default.

## 1) Create/update virtual environment

```powershell
.\bat\venv_setup.bat
```

Creates/updates `.venv` and installs packages from `requirements.txt`.

## 2) Run CLI (always via `.venv`)

Show help:

```powershell
.\bat\cli.bat --help
```

Ping running service:

```powershell
.\bat\cli.bat --host 127.0.0.1 --port 8765 ping
```

Read status:

```powershell
.\bat\cli.bat --host 127.0.0.1 --port 8765 get_status
```

## 3) Start service (always via `.venv`)

Show help:

```powershell
.\bat\service_start.bat --help
```

Start on COM4:

```powershell
.\bat\service_start.bat --port COM4 --unit 1 --tcp-port 8765 --verbose
```

Start using last saved serial port (`logs\cn616a_service_config_state.json`) or `CN616A_SERIAL_PORT`:

```powershell
.\bat\service_start.bat
```

If the service is already running, `service_start.bat` reports that and exits without starting a second copy.

## 3b) Stop service

```powershell
.\bat\service_stop.bat
```

This sends a clean `shutdown` command to the currently running service endpoint.

## 4) Start GUI (always via `.venv`)

Show help:

```powershell
.\bat\gui_start.bat --help
```

Start GUI with default logs dir:

```powershell
.\bat\gui_start.bat
```

`gui_start.bat` also auto-starts the service if it is not already running.
It uses saved values from `logs\cn616a_service_config_state.json` (`last_serial_port`, `last_tcp_host`, `last_tcp_port`).

Start GUI with custom logs dir and refresh interval:

```powershell
.\bat\gui_start.bat --logs-dir .\logs --refresh-interval 1.5
```

Optional environment overrides for service startup:

```powershell
$env:CN616A_SERIAL_PORT = "COM4"
$env:CN616A_SERVICE_HOST = "127.0.0.1"
$env:CN616A_SERVICE_TCP_PORT = "8765"
.\bat\gui_start.bat
```

## Notes

- `cli.bat`, `service_start.bat`, and `gui_start.bat` fail fast if `.venv` is missing.
- If needed, rerun setup first: `.\bat\venv_setup.bat`.
- `service_start.bat` and `gui_start.bat` now pause on startup errors so double-click launches do not instantly disappear.
- `gui_start.bat` starts the service as a separate process; closing the GUI does not stop the service.

## 5) Isolated responsiveness test

On a test machine, without a controller or service:

```powershell
.\bat\responsiveness_test.bat quick
.\bat\responsiveness_test.bat full
```

These are the only batch options. No arguments runs the quick comparison: one large-history chart-open session
with normal fake service replies and one with delayed replies. Each session
lasts 30 seconds. Full runs 24 combinations of chart/history states and
normal/delayed/disconnected/error replies, 60 seconds each (~25 minutes).
The Python entry point `tests\responsiveness.py` defines both workloads;
batch only selects the repository virtual environment and forwards the mode.
Refined terminal metrics and a JSON report identify
missed targets, insufficient sampling, slow callbacks and history-read
amplification. This launcher **never calls `gui_start.bat` or starts a service**.

Data verification, saved-report summaries and optional raw capture are Python
commands, not batch flags:

```powershell
.\.venv\Scripts\python.exe tests\responsiveness.py verify-data
.\.venv\Scripts\python.exe tests\responsiveness.py summarize C:\path\to\report.json
.\.venv\Scripts\python.exe tests\responsiveness.py capture --help
```

Capture requires a separate controller or a manually stopped service,
an explicit port, and `--controller-offline`. It is never run by quick/full.
See the repository README for capture and versioned replay instructions.

The verification and summary commands do not open a GUI or network listener.
The test's immutable compressed wire reference and clone setup instructions
are documented in [the fixture package](../tests/fixtures/cn616a-baseline-v1/README.md).
