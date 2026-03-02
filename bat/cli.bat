@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." >nul
set "REPO_ROOT=%CD%"
set "PY=%REPO_ROOT%\.venv\Scripts\python.exe"
set "CLI=%REPO_ROOT%\py\cn616a_cli.py"

if not exist "%PY%" (
  echo ERROR: Python interpreter not found at "%PY%"
  echo Run "bat\venv_setup.bat" first.
  popd >nul
  exit /b 1
)

if not exist "%CLI%" (
  echo ERROR: CLI script not found at "%CLI%"
  popd >nul
  exit /b 1
)

"%PY%" "%CLI%" %*
set "RC=%ERRORLEVEL%"

popd >nul
exit /b %RC%
