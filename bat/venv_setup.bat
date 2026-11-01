@echo off
setlocal EnableDelayedExpansion

REM ============================================
REM Configuration
REM ============================================

set PYTHON_VERSION=3.12
set VENV_NAME=.venv
set PYTHON_CMD=
set PYTHON_FOUND_VERSION=

REM Resolve repo root (this file is in repo_root\bat)
set SCRIPT_DIR=%~dp0
pushd "%SCRIPT_DIR%.."
set REPO_ROOT=%CD%

echo Creating virtual environment using Python %PYTHON_VERSION%...

where py >nul 2>nul
if not errorlevel 1 (
  py -%PYTHON_VERSION% -c "import sys; print(sys.version)" >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_CMD=py -%PYTHON_VERSION%"
  )
)

if "%PYTHON_CMD%"=="" (
  if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
    "%LocalAppData%\Programs\Python\Python312\python.exe" -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" > "%TEMP%\cn616a_pyver.txt" 2>nul
    set /p FOUND_VER=<"%TEMP%\cn616a_pyver.txt"
    del "%TEMP%\cn616a_pyver.txt" >nul 2>nul
    if "!FOUND_VER!"=="%PYTHON_VERSION%" (
      set "PYTHON_CMD=%LocalAppData%\Programs\Python\Python312\python.exe"
    )
  )
)

REM Also accept a 3.12 executable exposed as python/python3/python3.12 when
REM the py launcher is unavailable or not on PATH. This includes Store aliases.
if "%PYTHON_CMD%"=="" (
  for %%P in (python.exe python3.exe python3.12.exe) do (
    where %%P >nul 2>nul
    if not errorlevel 1 (
      %%P -c "import sys; raise SystemExit(0 if sys.version_info[0] == 3 and sys.version_info[1] == 12 else 1)" >nul 2>nul
      if not errorlevel 1 set "PYTHON_CMD=%%P"
    )
  )
)

if "%PYTHON_CMD%"=="" (
  for %%P in (python.exe python3.exe python3.12.exe) do (
    where %%P >nul 2>nul
    if not errorlevel 1 if "!PYTHON_FOUND_VERSION!"=="" (
      for /f "tokens=2" %%V in ('%%P --version 2^>^&1') do set "PYTHON_FOUND_VERSION=%%V"
    )
  )
  echo ERROR: Could not locate Python %PYTHON_VERSION%.
  echo Install Python %PYTHON_VERSION% and rerun this script.
  echo Expected commands: py -%PYTHON_VERSION% -V
  echo Expected path: %LocalAppData%\Programs\Python\Python312\python.exe
  if not "!PYTHON_FOUND_VERSION!"=="" echo Detected on PATH: !PYTHON_FOUND_VERSION! - not supported; Python %PYTHON_VERSION% is required.
  popd
  pause
  exit /b 1
)

if exist "%VENV_NAME%" (
  echo Removing existing %VENV_NAME% to rebuild with Python %PYTHON_VERSION%...
  rmdir /s /q "%VENV_NAME%"
)

%PYTHON_CMD% -m venv %VENV_NAME%
IF %ERRORLEVEL% NEQ 0 (
    echo Failed to create virtual environment.
  popd
    pause
    exit /b
)

call %VENV_NAME%\Scripts\activate

echo Upgrading pip...
python -m pip install --upgrade pip

REM OPTION A:
REM pip install pymodbus

REM OPTION B:
pip install -r requirements.txt

echo.
echo Virtual environment setup complete.
echo Environment path: %REPO_ROOT%\%VENV_NAME%
popd
pause