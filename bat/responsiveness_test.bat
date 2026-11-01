@echo off
setlocal

set "REPO_ROOT=%~dp0.."
set "PY=%REPO_ROOT%\.venv\Scripts\python.exe"
set "RUNNER=%REPO_ROOT%\tests\gui_responsiveness_baseline.py"

if not exist "%PY%" (
  echo ERROR: Create .venv with Python 3.12 and install requirements.txt first.
  exit /b 1
)
if not exist "%RUNNER%" (
  echo ERROR: Baseline runner is missing from this checkout.
  exit /b 1
)

if "%~1"=="" (
  "%PY%" "%RUNNER%" --quick
) else (
  "%PY%" "%RUNNER%" %*
)
exit /b %ERRORLEVEL%
