@echo off
rem Stops Easy Copy Tracker CLEANLY: sends it a shutdown request, so the crash
rem shadow is removed and the next start does not show a bogus "crash" banner.
cd /d "%~dp0"

powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'http://127.0.0.1:8765/api/quit' -Method POST -Headers @{'X-EasyCopyTracker'='1'} -UseBasicParsing -TimeoutSec 5 | Out-Null; Write-Host 'Easy Copy Tracker stopped.' } catch { Write-Host 'Easy Copy Tracker does not appear to be running.' }"

pause
