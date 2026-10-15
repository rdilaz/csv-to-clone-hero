@echo off
setlocal
title Riff Companion
where py >nul 2>nul
if errorlevel 1 goto plainpython
py -3.12 -I -B -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 2)" >nul 2>nul
if errorlevel 1 goto plainpython
py -3.12 -I -B "%~dp0bootstrap.py" %*
set "RIFF_COMPANION_EXIT=%ERRORLEVEL%"
goto finished
:plainpython
python -I -B "%~dp0bootstrap.py" %*
set "RIFF_COMPANION_EXIT=%ERRORLEVEL%"
:finished
if not "%RIFF_COMPANION_EXIT%"=="0" (
  echo.
  echo See README.txt. This download requires existing Windows x64 Python 3.12.
  pause
)
exit /b %RIFF_COMPANION_EXIT%
