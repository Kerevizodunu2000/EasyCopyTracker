@echo off
rem Easy Copy Tracker'i TEMIZ sekilde durdurur: uygulamaya kapanma istegi gonderir,
rem boylece golge kopya silinir ve bir sonraki acilista sahte "cokme" uyarisi cikmaz.
cd /d "%~dp0"

powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'http://127.0.0.1:8765/api/quit' -Method POST -Headers @{'X-EasyCopyTracker'='1'} -UseBasicParsing -TimeoutSec 5 | Out-Null; Write-Host 'Easy Copy Tracker durduruldu.' } catch { Write-Host 'Easy Copy Tracker calisir durumda gorunmuyor.' }"

pause
