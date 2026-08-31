@echo off
rem ===========================================================================
rem  CSV to Clone Hero - local journey web interface (no terminal needed)
rem  Starts the real Flask app and opens the accepted journey at /
rem  Same-Wi-Fi mode: WEB_INTERFACE.bat --lan
rem  Public HTTPS mode: WEB_INTERFACE.bat --public-url https://your-address
rem  Development reload (not for normal use): .venv\Scripts\python.exe -m csv_to_clonehero web --reload --no-browser
rem ===========================================================================
setlocal
cd /d "%~dp0"

set "REPOSITORY=%CD%"
set "PORT_PID="
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue; if ($listener) { $listener | ForEach-Object { $_.OwningProcess } }"`) do set "PORT_PID=%%P"
if defined PORT_PID (
    echo.
    echo  ERROR: Port 8765 is already in use by process ID %PORT_PID%.
    tasklist /FI "PID eq %PORT_PID%"
    echo  Stop that process or use the development command with another port.
    echo.
    pause
    exit /b 2
)

set "PY="
where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py -3"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 set "PY=python"
)
if not defined PY (
    echo.
    echo  ERROR: Python was not found.
    echo  Install Python 3.11 or newer from https://www.python.org/downloads/
    echo  ^(tick "Add python.exe to PATH" during install^), then try again.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  First run: setting things up, this takes a minute...
    echo.
    %PY% -m venv .venv
    if errorlevel 1 goto :setup_failed
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -e ".[legacy-keyring]"
    if errorlevel 1 goto :setup_failed
)

set "PY_EXE=%REPOSITORY%\.venv\Scripts\python.exe"
echo.
echo  Python: "%PY_EXE%"
echo  Repository: "%REPOSITORY%"
echo  Serving real Flask journey app at: http://127.0.0.1:8765/
echo  Flask auto-reload is disabled for this normal launcher.
echo.
"%PY_EXE%" -m csv_to_clonehero web --port 8765 %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo  The real journey app stopped with exit code %EXIT_CODE%.
    echo.
)
pause
exit /b %EXIT_CODE%

:setup_failed
echo.
echo  Setup failed. Make sure you are connected to the internet and that
echo  Python 3.11+ is installed, then run CHECK_SETUP.bat for details.
echo.
pause
exit /b 1
