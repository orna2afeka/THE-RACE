@echo off
REM ===========================================================================
REM  Start Pit Dashboard.bat - double-click me from the repo root.
REM
REM  Starts the pit dashboard (React + FastAPI) on http://localhost:8000.
REM  Thin forwarder to Pit_Web\run_web.bat, which does the real work: finds a
REM  usable Python, installs dependencies on first run (and only when they have
REM  actually changed), checks the built frontend is present, and starts the
REM  collector and the dashboard in their own windows. See that file for details.
REM
REM  Port map:  8000 pit dashboard   8502 profile builder   8503 pit wall (TV)
REM             8010 demo dashboard   8504 energy matrix
REM ===========================================================================
if not exist "%~dp0Pit_Web\run_web.bat" (
    echo.
    echo   [X] Pit_Web\run_web.bat was not found next to this file.
    echo       Run this from inside the cloned repository, not from a copy of
    echo       just this one .bat.
    echo.
    pause
    exit /b 1
)
call "%~dp0Pit_Web\run_web.bat"
REM Keep the window open if the launcher failed, so the error is readable
REM instead of vanishing with the console.
if errorlevel 1 pause
exit /b %errorlevel%
