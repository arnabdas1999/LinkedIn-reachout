@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run Setup.cmd first.
  pause
  exit /b 1
)
echo Opening your selected browser in a separate profile for LinkedIn sign-in.
echo Keep this console open until you finish signing in.
echo This command never sends invitations or messages.
echo.
".venv\Scripts\python.exe" outreach.py login
echo.
if errorlevel 1 (
  echo Sign-in did not finish. Check the error above and README troubleshooting.
) else (
  echo Sign-in saved. The browser closed normally after saving your session.
  echo Run a preview, then start background automation from the menu.
)
pause
