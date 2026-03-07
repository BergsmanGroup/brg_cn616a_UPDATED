# Batch Script Usage

All commands below should be run from the repository root.

## Python Version

This project is pinned to Python `3.12` for GUI stability.
`bat\venv_setup.bat` creates `.venv` using `py -3.12` and will fail fast if Python 3.12 is not installed.

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
