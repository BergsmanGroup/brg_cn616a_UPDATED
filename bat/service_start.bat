@echo off
setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." >nul
set "REPO_ROOT=%CD%"
set "PY=%REPO_ROOT%\.venv\Scripts\python.exe"
set "SERVICE=%REPO_ROOT%\py\cn616a_service.py"
set "CFG=%REPO_ROOT%\logs\cn616a_service_config_state.json"

set "SERVICE_PORT="
set "SERVICE_HOST=127.0.0.1"
set "SERVICE_TCP_PORT=8765"
set "HAS_PORT_ARG=0"

if not exist "%PY%" (
  call :fail "ERROR: Python interpreter not found at \"%PY%\""
)

if not exist "%SERVICE%" (
  call :fail "ERROR: Service script not found at \"%SERVICE%\""
)

if /I "%~1"=="--help" goto run_service
if /I "%~1"=="-h" goto run_service

call :detect_port_arg %*

if defined CN616A_SERIAL_PORT set "SERVICE_PORT=%CN616A_SERIAL_PORT%"
if defined CN616A_SERVICE_HOST set "SERVICE_HOST=%CN616A_SERVICE_HOST%"
if defined CN616A_SERVICE_TCP_PORT set "SERVICE_TCP_PORT=%CN616A_SERVICE_TCP_PORT%"
if "%SERVICE_PORT%"=="" if exist "%CFG%" call :load_cfg

call :is_service_up
if "%ERRORLEVEL%"=="0" (
  echo Service already running on %SERVICE_HOST%:%SERVICE_TCP_PORT%.
  popd >nul
  exit /b 0
)

set "EXTRA_ARGS="
if "%HAS_PORT_ARG%"=="0" (
  if not "%SERVICE_PORT%"=="" (
    set "EXTRA_ARGS=--port \"%SERVICE_PORT%\""
  ) else (
    call :fail "ERROR: Missing serial port. Pass --port COMx, or run GUI once, or set CN616A_SERIAL_PORT."
  )
)

:run_service
"%PY%" "%SERVICE%" %EXTRA_ARGS% %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo Service exited with code %RC%.
  echo If this was launched by double-click, run from a terminal to see full traceback.
  pause
)

popd >nul
exit /b %RC%

:load_cfg
set "CFG_ENV=%TEMP%\cn616a_service_start_env.cmd"
"%PY%" -c "import json,pathlib,sys; c=(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')).get('config',{})); print('set SERVICE_PORT='+str(c.get('last_serial_port','')).strip()); print('set SERVICE_HOST='+(str(c.get('last_tcp_host','127.0.0.1')).strip() or '127.0.0.1')); print('set SERVICE_TCP_PORT='+str(int(c.get('last_tcp_port',8765) or 8765)))" "%CFG%" > "%CFG_ENV%" 2>nul
if exist "%CFG_ENV%" call "%CFG_ENV%"
if exist "%CFG_ENV%" del "%CFG_ENV%" >nul 2>nul
exit /b 0

:is_service_up
"%PY%" -c "import socket,sys; h=sys.argv[1]; p=int(sys.argv[2]); s=socket.socket(); s.settimeout(0.5); ok=(s.connect_ex((h,p))==0); s.close(); sys.exit(0 if ok else 1)" "%SERVICE_HOST%" "%SERVICE_TCP_PORT%"
exit /b %ERRORLEVEL%

:detect_port_arg
set "__PREV="
for %%A in (%*) do (
  if /I "%%~A"=="--port" set "HAS_PORT_ARG=1"
  echo %%~A | findstr /i /r "^--port=.*" >nul && set "HAS_PORT_ARG=1"
  if /I "!__PREV!"=="--port" set "HAS_PORT_ARG=1"
  set "__PREV=%%~A"
)
set "__PREV="
exit /b 0

:fail
echo %~1
echo.
pause
popd >nul
exit /b 1
