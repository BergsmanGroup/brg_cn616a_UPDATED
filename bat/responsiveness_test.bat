@echo off
setlocal

set "REPO_ROOT=%~dp0.."
set "PY=%REPO_ROOT%\.venv\Scripts\python.exe"
set "RUNNER=%REPO_ROOT%\tests\responsiveness.py"

if not exist "%PY%" (
  echo ERROR: Create .venv with Python 3.12 and install requirements.txt first.
  exit /b 1
)
if not exist "%RUNNER%" (
  echo ERROR: Baseline runner is missing from this checkout.
  exit /b 1
)

set "MODE=%~1"
if "%MODE%"=="" set "MODE=quick"
if not "%~2"=="" goto usage
if /i "%MODE%"=="quick" (
  set "MODE=quick"
  goto run
)
if /i "%MODE%"=="full" (
  set "MODE=full"
  goto run
)
:usage
echo Usage: responsiveness_test.bat [quick^|full]
echo Data verification and raw capture are available through tests\responsiveness.py.
exit /b 2
:run
"%PY%" "%RUNNER%" %MODE%
exit /b %ERRORLEVEL%
