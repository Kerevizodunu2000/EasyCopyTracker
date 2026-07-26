@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title CopyTracker - Kurulum
cd /d "%~dp0"

echo.
echo   ============================================
echo      CopyTracker - Kurulum
echo   ============================================
echo.

rem --- 1) Python var mi? -------------------------------------------------
echo   [1/4] Python kontrol ediliyor...
where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo   HATA: Python bulunamadi.
    echo.
    echo   Once Python 3.10 veya uzerini kurun:
    echo     https://www.python.org/downloads/
    echo.
    echo   Kurulum sirasinda "Add python.exe to PATH" kutusunu ISARETLEYIN.
    echo.
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo         Python !PYVER! bulundu.

rem --- 2) Bagimliliklar -------------------------------------------------
echo   [2/4] Gerekli paketler kuruluyor (flask, qrcode)...
python -m pip install --quiet --upgrade pip >nul 2>nul
python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo.
    echo   HATA: Paketler kurulamadi. Internet baglantinizi kontrol edin.
    pause
    exit /b 1
)
echo         Paketler hazir.

rem --- 3) Masaustu kisayolu ---------------------------------------------
echo   [3/4] Masaustu kisayolu olusturuluyor...
set "PYW=pythonw.exe"
where pythonw >nul 2>nul || set "PYW=python.exe"
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\CopyTracker.lnk');" ^
  "$s.TargetPath=(Get-Command '%PYW%').Source;" ^
  "$s.Arguments='\"%CD%\copytracker.py\" --open';" ^
  "$s.WorkingDirectory='%CD%';" ^
  "$s.Description='CopyTracker - pano gelen kutusu';" ^
  "$s.Save()" >nul 2>nul
if exist "%USERPROFILE%\Desktop\CopyTracker.lnk" (
    echo         Masaustune kisayol eklendi.
) else (
    echo         Kisayol olusturulamadi ^(sorun degil, start.bat ile calistirin^).
)

rem --- 4) Baslat --------------------------------------------------------
echo   [4/4] CopyTracker baslatiliyor...
start "" "%PYW%" "%CD%\copytracker.py"
timeout /t 3 /nobreak >nul
start "" http://localhost:8765

echo.
echo   ============================================
echo      Kurulum tamamlandi.
echo   ============================================
echo.
echo   * Arayuz:      http://localhost:8765
echo   * Kisayollar:  Ctrl+Alt+K yakalama ac/kapat
echo                  Ctrl+Alt+L listeyi ac
echo   * Tepsi:       saat yaninda, sag tik ile menu
echo   * Durdurmak:   stop.bat  ya da tepsi menusu ^> Cikis
echo.
echo   Windows acilisinda otomatik baslamasi icin
echo   arayuzdeki Ayarlar bolumunden acabilirsiniz.
echo.
pause
