# Batch Script Usage

All commands below should be run from the repository root.

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

## Notes

- `cli.bat` and `service_start.bat` fail fast if `.venv` is missing.
- If needed, rerun setup first: `.\bat\venv_setup.bat`.
