@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 wing_monitor.py
    if errorlevel 1 pause
    exit /b
)
where python >nul 2>nul
if %errorlevel%==0 (
    python wing_monitor.py
    if errorlevel 1 pause
    exit /b
)
echo Install Python 3.10 or newer from https://www.python.org/downloads/windows/
echo Include Tcl/Tk and the Python launcher, then run this launcher again.
pause
