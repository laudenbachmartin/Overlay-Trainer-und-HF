@echo off
setlocal
cd /d "%~dp0"

REM --- Python finden (py-Launcher bevorzugt) ---
set "PY="
set "PYW="
where py >nul 2>nul && set "PY=py" && set "PYW=pyw"
if not defined PY (
    where python >nul 2>nul && set "PY=python" && set "PYW=pythonw"
)
if not defined PY (
    echo Python wurde nicht gefunden.
    echo Bitte installieren: https://www.python.org/downloads/
    echo Beim Setup "Add python.exe to PATH" anhaken.
    pause
    exit /b 1
)

REM --- Pakete beim ersten Start automatisch installieren ---
%PY% -c "import bleak, usb, libusb_package" >nul 2>nul
if errorlevel 1 (
    echo Erster Start: installiere benoetigte Pakete ...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Installation fehlgeschlagen - siehe README.md, Abschnitt Fehlerbehebung.
        pause
        exit /b 1
    )
)

REM --- Overlay ohne Konsolenfenster starten ---
where %PYW% >nul 2>nul
if errorlevel 1 (
    start "" %PY% overlay.py
) else (
    start "" %PYW% overlay.py
)
exit /b 0
