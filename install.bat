@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title Easy Copy Tracker - Setup
cd /d "%~dp0"

echo.
echo   ============================================
echo      Easy Copy Tracker - Setup
echo   ============================================
echo.

rem --- 1) Is Python available? ------------------------------------------
echo   [1/4] Checking Python...
where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo   ERROR: Python was not found.
    echo.
    echo   Install Python 3.10 or newer first:
    echo     https://www.python.org/downloads/
    echo.
    echo   During setup, TICK the "Add python.exe to PATH" box.
    echo.
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo         Found Python !PYVER!.

rem --- 2) Dependencies --------------------------------------------------
echo   [2/4] Installing the required packages (flask, qrcode)...
python -m pip install --quiet --upgrade pip >nul 2>nul
python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo.
    echo   ERROR: The packages could not be installed. Check your connection.
    pause
    exit /b 1
)
echo         Packages ready.

rem --- 3) Desktop shortcut ----------------------------------------------
echo   [3/4] Creating the desktop shortcut...
set "PYW=pythonw.exe"
where pythonw >nul 2>nul || set "PYW=python.exe"
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Easy Copy Tracker.lnk');" ^
  "$s.TargetPath=(Get-Command '%PYW%').Source;" ^
  "$s.Arguments='\"%CD%\easycopytracker.py\" --open';" ^
  "$s.WorkingDirectory='%CD%';" ^
  "$s.Description='Easy Copy Tracker - clipboard inbox';" ^
  "if (Test-Path '%CD%\docs\easycopytracker.ico') { $s.IconLocation='%CD%\docs\easycopytracker.ico' };" ^
  "$s.Save()" >nul 2>nul
if exist "%USERPROFILE%\Desktop\Easy Copy Tracker.lnk" (
    echo         Shortcut added to the desktop.
) else (
    echo         Could not create the shortcut ^(no problem, use start.bat^).
)

rem --- 4) Start ---------------------------------------------------------
echo   [4/4] Starting Easy Copy Tracker...
start "" "%PYW%" "%CD%\easycopytracker.py"
timeout /t 3 /nobreak >nul
start "" http://localhost:8765

echo.
echo   ============================================
echo      Setup complete.
echo   ============================================
echo.
echo   * Web UI:     http://localhost:8765
echo   * Shortcuts:  Ctrl+Alt+K toggle capture
echo                 Ctrl+Alt+L open the list
echo   * Tray:       next to the clock, right-click for the menu
echo   * To stop:    stop.bat  or the tray menu ^> Quit
echo.
echo   To have it start with Windows, turn that on
echo   under Settings in the web UI.
echo.
pause
