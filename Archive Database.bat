@echo off
REM ===========================================================================
REM  Archive Database.bat - double-click me from the repo root, between sessions.
REM
REM  Retires Pit_Dashboard\telemetry.db into its own folder under
REM  Pit_Dashboard\archive\ and leaves a fresh empty one, so the next practice
REM  day or race starts on a small, clean database. Thin forwarder to
REM  tools\archive_db.py, which does the real work and explains itself.
REM
REM  CLOSE THE PIT FIRST. The Pit Web, Pit Collector, pit wall and profile
REM  builder windows all hold the database open; the script refuses to touch
REM  anything while any of them is running, and says so.
REM ===========================================================================
setlocal
cd /d "%~dp0"

if not exist "tools\archive_db.py" (
    echo.
    echo   [X] tools\archive_db.py was not found next to this file.
    echo       Run this from inside the cloned repository, not from a copy of
    echo       just this one .bat.
    echo.
    pause
    exit /b 1
)

REM Short form of the interpreter hunt in Pit_Web\run_web.bat. If neither of
REM these works, that launcher's longer ladder is the place to look -- it knows
REM about the Microsoft Store alias stub and the per-version install paths.
set "PYCMD="
py -3 -c "import sys" >nul 2>&1 && set "PYCMD=py -3"
if not defined PYCMD python -c "import sys" >nul 2>&1 && set "PYCMD=python"
if not defined PYCMD (
    echo.
    echo   [X] No usable Python found on PATH.
    echo       "Start Pit Dashboard.bat" finds one in more places - if that
    echo       launcher works on this laptop, run the script by hand instead:
    echo           python tools\archive_db.py --label practice
    echo.
    pause
    exit /b 1
)

REM Pass any arguments straight through, so this also serves
REM   "Archive Database.bat" --dry-run
REM   "Archive Database.bat" --label prerace
REM With none, label the session for the day it is: practice.
if "%~1"=="" (
    %PYCMD% tools\archive_db.py --label practice
) else (
    %PYCMD% tools\archive_db.py %*
)

REM Always pause: this is run by a person who needs to READ the result,
REM not by another script.
pause
exit /b %errorlevel%
