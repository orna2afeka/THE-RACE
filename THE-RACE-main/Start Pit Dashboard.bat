@echo off
REM ===========================================================================
REM  Start Pit Dashboard.bat - double-click me from the repo root.
REM
REM  Thin forwarder to Pit_Dashboard\run_pit.bat, which does the real work:
REM  finds a usable Python, installs dependencies on first run, and starts the
REM  collector and the dashboard. See that file for details.
REM ===========================================================================
if not exist "%~dp0Pit_Dashboard\run_pit.bat" (
    echo.
    echo   [X] Pit_Dashboard\run_pit.bat was not found next to this file.
    echo       Run this from inside the cloned repository, not from a copy of
    echo       just this one .bat.
    echo.
    pause
    exit /b 1
)
call "%~dp0Pit_Dashboard\run_pit.bat"
REM Keep the window open if the launcher failed, so the error is readable
REM instead of vanishing with the console.
if errorlevel 1 pause
exit /b %errorlevel%
