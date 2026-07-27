@echo off
chcp 65001 >nul
rem Starts Easy Copy Tracker in the background and opens the list in the browser.
cd /d "%~dp0"

rem First run? If the packages are missing, hand over to the setup script.
python -c "import flask" >nul 2>nul
if errorlevel 1 (
    echo The required packages are not installed. Starting setup...
    echo.
    call install.bat
    exit /b
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "Easy Copy Tracker" pythonw easycopytracker.py
) else (
    start "Easy Copy Tracker" /min python easycopytracker.py
)
timeout /t 2 /nobreak >nul
start "" http://localhost:8765
