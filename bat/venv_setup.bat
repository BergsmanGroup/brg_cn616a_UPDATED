@echo off
setlocal EnableDelayedExpansion

REM ============================================
REM Configuration
REM ============================================

set PYTHON_VERSION=3.13
set VENV_NAME=.venv

REM Resolve repo root (this file is in repo_root\bat)
set SCRIPT_DIR=%~dp0
pushd "%SCRIPT_DIR%.."
set REPO_ROOT=%CD%

echo Creating virtual environment using Python %PYTHON_VERSION%...

REM Quick version check (major.minor)
for /f "tokens=1,2 delims=." %%a in ('python -c "import sys; print(str(sys.version_info[0])+'.'+str(sys.version_info[1]))"') do (
  set FOUND_VER=%%a.%%b
)

if not "!FOUND_VER!"=="%PYTHON_VERSION%" (
  echo ERROR: python on PATH is !FOUND_VER! but you requested %PYTHON_VERSION%.
  echo Fix PATH order or install Python %PYTHON_VERSION%.
  pause
  exit /b 1
)

python -m venv %VENV_NAME%
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