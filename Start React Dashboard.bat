@echo off
REM ===========================================================================
REM  Start React Dashboard.bat - double-click me from the repo root.
REM
REM  Thin forwarder to Pit_Web\run_web.bat, which does the real work: finds a
REM  usable Python, installs dependencies on first run (and only when they have
REM  actually changed), checks the frontend build is present, and starts the
REM  collector and the dashboard. See that file for details.
REM
REM  The Streamlit pit wall is NOT in this folder - it lives in ..\THE RACE and
REM  is started from there. Both can run at once: this on 8000, Streamlit 8501.
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
