@echo off
rem ===========================================================================
rem  CSV to Clone Hero - dry run (searches and plans, downloads NOTHING)
rem ===========================================================================
setlocal
cd /d "%~dp0"
call "%~dp0BUILD_LIBRARY.bat" --dry-run
