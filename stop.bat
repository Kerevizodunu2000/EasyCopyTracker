@echo off
rem Arka planda calisan CopyTracker'i durdurur.
cd /d "%~dp0"
if not exist copytracker.pid goto :none
set /p PID=<copytracker.pid
taskkill /pid %PID% /f >nul 2>nul
if %errorlevel%==0 (
    echo CopyTracker durduruldu.
) else (
    echo Islem bulunamadi - zaten kapali olabilir.
)
del copytracker.pid >nul 2>nul
goto :end
:none
echo CopyTracker calisir durumda gorunmuyor.
:end
pause
