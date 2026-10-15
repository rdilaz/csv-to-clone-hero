@echo off
setlocal
call "%~dp0START_COMPANION.cmd" --configure %*
if not errorlevel 1 pause
