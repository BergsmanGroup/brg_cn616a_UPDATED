@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." >nul
set "REPO_ROOT=%CD%"
set "PY=%REPO_ROOT%\.venv\Scripts\python.exe"
set "SERVICE=%REPO_ROOT%\py\cn616a_service.py"

if not exist "%PY%" (
  echo ERROR: Python interpreter not found at "%PY%"
  echo Run "bat\venv_setup.bat" first.
  popd >nul
  exit /b 1
)

if not exist "%SERVICE%" (
  echo ERROR: Service script not found at "%SERVICE%"
  popd >nul
  exit /b 1
)

"%PY%" "%SERVICE%" %*
set "RC=%ERRORLEVEL%"

popd >nul
exit /b %RC%
