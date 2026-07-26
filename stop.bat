@echo off
rem CopyTracker'i TEMIZ sekilde durdurur: uygulamaya kapanma istegi gonderir,
rem boylece golge kopya silinir ve bir sonraki acilista sahte "cokme" uyarisi cikmaz.
cd /d "%~dp0"

powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'http://127.0.0.1:8765/api/quit' -Method POST -Headers @{'X-CopyTracker'='1'} -UseBasicParsing -TimeoutSec 5 | Out-Null; Write-Host 'CopyTracker durduruldu.' } catch { Write-Host 'CopyTracker calisir durumda gorunmuyor.' }"

pause
