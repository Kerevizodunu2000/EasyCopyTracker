@echo off
chcp 65001 >nul
rem CopyTracker'i arka planda baslatir ve listeyi tarayicida acar.
cd /d "%~dp0"

rem Ilk calistirma mi? Paketler yoksa kurulum betigine yonlendir.
python -c "import flask" >nul 2>nul
if errorlevel 1 (
    echo Gerekli paketler kurulu degil. Kurulumu baslatiyorum...
    echo.
    call kurulum.bat
    exit /b
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "CopyTracker" pythonw copytracker.py
) else (
    start "CopyTracker" /min python copytracker.py
)
timeout /t 2 /nobreak >nul
start "" http://localhost:8765
