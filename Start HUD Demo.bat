@echo off
REM ===================================================================
REM  Start HUD Demo - the real driver HUD, driven by a fake car.
REM
REM  No CAN adapter, no GPS, no Firebase, no Pi. Opens the same
REM  RacingDashboard the car runs and feeds all four screens (DS001-DS004),
REM  plus a tour of the real hazards the driver can see.
REM
REM  Keys: M pit message . N clear . T turn warning . P pause
REM        H next hazard . X clear hazard . Alt+F4 quit
REM
REM  Extra options pass straight through, for example:
REM     "Start HUD Demo.bat" --profile dor_265s
REM     "Start HUD Demo.bat" --fullscreen --no-tour
REM
REM  WHY THIS SEARCHES FOR A PYTHON instead of reusing the pit's:
REM  the HUD needs PySide6, and the interpreter the pit dashboard records
REM  in Pit_Web\.deps_stamp does not necessarily have it - on the
REM  team laptop it is Python 3.12 without PySide6, while 3.11 has it.
REM  Reusing it blindly would open a console that just says
REM  "No module named PySide6". So each candidate is tried until one can
REM  actually import it.
REM
REM  Safe to run during a session: the simulator never touches Firebase
REM  or telemetry.db.
REM ===================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PYCMD="

REM 1. the interpreter the pit dashboard already uses, if it has PySide6
if exist "Pit_Web\.deps_stamp" (
    for /f "usebackq tokens=1,*" %%A in ("Pit_Web\.deps_stamp") do set "STAMP=%%B"
    if defined STAMP (
        "!STAMP!" -c "import PySide6" >nul 2>&1 && set "PYCMD="!STAMP!""
    )
)

REM 2. otherwise whatever else is installed
for %%P in ("python" "py -3.11" "py -3.12" "py -3.13" "py -3") do (
    if not defined PYCMD (
        %%~P -c "import PySide6" >nul 2>&1 && set "PYCMD=%%~P"
    )
)

if /i "%~1"=="--which" (
    if defined PYCMD (echo !PYCMD!) else (echo none)
    exit /b 0
)

if not defined PYCMD (
    echo.
    echo  No Python on this laptop can import PySide6, which the HUD needs.
    echo.
    echo  Install it into the Python you want to use, for example:
    echo      python -m pip install PySide6
    echo.
    echo  then double-click this file again.
    echo.
    pause
    exit /b 1
)

echo Starting the HUD demo with: !PYCMD!
echo Keys: M pit message . N clear . T turn . P pause . H next hazard . X clear hazard
start "HUD Demo" cmd /k !PYCMD! tools\hud_sim.py %*
endlocal
