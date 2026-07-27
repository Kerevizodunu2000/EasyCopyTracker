@echo off
chcp 65001 >nul
rem Easy Copy Tracker'i arka planda baslatir ve listeyi tarayicida acar.
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
    start "Easy Copy Tracker" pythonw easycopytracker.py
) else (
    start "Easy Copy Tracker" /min python easycopytracker.py
)
timeout /t 2 /nobreak >nul
start "" http://localhost:8765
