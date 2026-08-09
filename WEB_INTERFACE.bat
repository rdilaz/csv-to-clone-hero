@echo off
rem ===========================================================================
rem  CSV to Clone Hero - web interface (no terminal needed)
rem  Opens a local web page where you can upload CSVs and build the library.
rem ===========================================================================
setlocal
cd /d "%~dp0"

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
    ".venv\Scripts\python.exe" -m pip install -e .
    if errorlevel 1 goto :setup_failed
)

".venv\Scripts\python.exe" -m csv_to_clonehero web %*
pause
exit /b 0

:setup_failed
echo.
echo  Setup failed. Make sure you are connected to the internet and that
echo  Python 3.11+ is installed, then run CHECK_SETUP.bat for details.
echo.
pause
exit /b 1
