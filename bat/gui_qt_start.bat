@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." >nul
set "REPO_ROOT=%CD%"
set "PY=%REPO_ROOT%\.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo ERROR: Python interpreter not found at "%PY%"
  echo Run .\bat\venv_setup.bat and install Qt dependencies with:
  echo .\.venv\Scripts\python.exe -m pip install -r requirements-qt.txt
  popd
  exit /b 1
)

"%PY%" -m py.qt_gui %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
