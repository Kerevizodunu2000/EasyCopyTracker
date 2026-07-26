@echo off
rem CopyTracker'i arka planda baslatir ve listeyi tarayicida acar.
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "CopyTracker" pythonw copytracker.py
) else (
    start "CopyTracker" /min python copytracker.py
)
timeout /t 2 /nobreak >nul
start "" http://localhost:8765
