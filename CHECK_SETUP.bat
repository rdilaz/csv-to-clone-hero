@echo off
rem ===========================================================================
rem  CSV to Clone Hero - check your setup (Python, folders, network, sources)
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

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m csv_to_clonehero doctor
) else (
    rem No venv yet: run with system Python + src on the path (deps may be
    rem missing; doctor will tell you).
    set "PYTHONPATH=%~dp0src"
    %PY% -m csv_to_clonehero doctor
)
echo.
pause
