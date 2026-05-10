@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." >nul
set "REPO_ROOT=%CD%"
set "PY=%REPO_ROOT%\.venv\Scripts\python.exe"
set "CLI=%REPO_ROOT%\py\cn616a_cli.py"
set "CFG=%REPO_ROOT%\logs\cn616a_service_config_state.json"

set "SERVICE_HOST=127.0.0.1"
set "SERVICE_TCP_PORT=8765"

if not exist "%PY%" (
  echo ERROR: Python interpreter not found at "%PY%"
  echo Run "bat\venv_setup.bat" first.
  echo.
  pause
  popd >nul
  exit /b 1
)

if not exist "%CLI%" (
  echo ERROR: CLI script not found at "%CLI%"
  echo.
  pause
  popd >nul
  exit /b 1
)

if defined CN616A_SERVICE_HOST set "SERVICE_HOST=%CN616A_SERVICE_HOST%"
if defined CN616A_SERVICE_TCP_PORT set "SERVICE_TCP_PORT=%CN616A_SERVICE_TCP_PORT%"
if exist "%CFG%" call :load_cfg

call :is_service_up
if errorlevel 1 (
  echo No service detected on %SERVICE_HOST%:%SERVICE_TCP_PORT%.
  popd >nul
  exit /b 0
)

echo Sending shutdown command to service on %SERVICE_HOST%:%SERVICE_TCP_PORT%...
"%PY%" "%CLI%" --host "%SERVICE_HOST%" --port %SERVICE_TCP_PORT% shutdown
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo Shutdown command failed with code %RC%.
  pause
)

popd >nul
exit /b %RC%

:load_cfg
set "CFG_ENV=%TEMP%\cn616a_service_stop_env.cmd"
"%PY%" -c "import json,pathlib,sys; c=(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')).get('config',{})); print('set SERVICE_HOST='+(str(c.get('last_tcp_host','127.0.0.1')).strip() or '127.0.0.1')); print('set SERVICE_TCP_PORT='+str(int(c.get('last_tcp_port',8765) or 8765)))" "%CFG%" > "%CFG_ENV%" 2>nul
if exist "%CFG_ENV%" call "%CFG_ENV%"
if exist "%CFG_ENV%" del "%CFG_ENV%" >nul 2>nul
exit /b 0

:is_service_up
"%PY%" -c "import socket,sys; h=sys.argv[1]; p=int(sys.argv[2]); s=socket.socket(); s.settimeout(0.5); ok=(s.connect_ex((h,p))==0); s.close(); sys.exit(0 if ok else 1)" "%SERVICE_HOST%" "%SERVICE_TCP_PORT%"
exit /b %ERRORLEVEL%
