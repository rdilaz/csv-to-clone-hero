@echo off
rem ===========================================================================
rem  CSV to Clone Hero - build the library (downloads charts)
rem  Double-click this file. First run sets up a Python environment for you.
rem ===========================================================================
setlocal
cd /d "%~dp0"

rem Locate Python: prefer the "py" launcher, fall back to "python".
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

rem First run: create the virtual environment and install dependencies.
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  First run: setting things up, this takes a minute...
    echo.
    %PY% -m venv .venv
    if errorlevel 1 goto :setup_failed
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -e .
    if errorlevel 1 goto :setup_failed
    echo.
    echo  Setup complete.
    echo.
)

".venv\Scripts\python.exe" -m csv_to_clonehero build %*
set "EXITCODE=%errorlevel%"
echo.
if %EXITCODE%==0 (
    echo  Done. Check the reports folder for details.
) else (
    echo  Something went wrong ^(exit code %EXITCODE%^). Scroll up for details,
    echo  or run CHECK_SETUP.bat to diagnose your setup.
)
echo.
pause
exit /b %EXITCODE%

:setup_failed
echo.
echo  Setup failed. Make sure you are connected to the internet and that
echo  Python 3.11+ is installed, then run CHECK_SETUP.bat for details.
echo.
pause
exit /b 1
