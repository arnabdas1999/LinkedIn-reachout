@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run Setup.cmd first.
  pause
  exit /b 1
)
if not exist "settings.json" (
  echo Run Setup.cmd first to choose your CSV, templates and browser.
  pause
  exit /b 1
)
:menu
echo.
echo LinkedIn Outreach
echo 1. Sign in - opens your selected browser
echo 2. Preview messages - no sending
echo 3. Check setup - no LinkedIn activity
echo 4. Run once - LIVE, uses configured limits
echo 5. Start background automation - LIVE, browser stays hidden
echo 6. Show progress
echo 7. Stop background automation
echo 8. Exit
choice /c 12345678 /n /m "Choose 1-8: "
if errorlevel 8 exit /b 0
if errorlevel 7 goto stop
if errorlevel 6 goto status
if errorlevel 5 goto watch
if errorlevel 4 goto run
if errorlevel 3 goto doctor
if errorlevel 2 goto preview
if errorlevel 1 goto login
:login
".venv\Scripts\python.exe" outreach.py login
goto done
:preview
".venv\Scripts\python.exe" outreach.py preview
goto done
:doctor
".venv\Scripts\python.exe" outreach.py doctor
goto done
:run
".venv\Scripts\python.exe" outreach.py run --live
goto done
:watch
".venv\Scripts\python.exe" outreach.py start --live
goto done
:status
".venv\Scripts\python.exe" outreach.py status
goto done
:stop
".venv\Scripts\python.exe" outreach.py stop-watch
:done
echo.
pause
goto menu
