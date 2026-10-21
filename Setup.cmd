@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv 2>nul
  if errorlevel 1 python -m venv .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11 or newer is required'"
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 goto failed
if not exist "settings.json" (
  ".venv\Scripts\python.exe" outreach.py init
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" outreach.py install-browser
if errorlevel 1 goto failed
".venv\Scripts\python.exe" outreach.py doctor
if errorlevel 1 goto failed
echo.
echo Ready. Open Start LinkedIn Outreach.cmd.
pause
exit /b 0
:failed
echo Setup needs attention. Read the error above. Python 3.11+ and internet access are required.
pause
exit /b 1
