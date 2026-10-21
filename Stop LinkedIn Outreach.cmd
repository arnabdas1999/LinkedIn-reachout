@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" outreach.py stop-watch
pause
