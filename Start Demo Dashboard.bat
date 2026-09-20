@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
REM ===========================================================================
REM  Demo Dashboard.bat - see every feature working, with no car and no radio.
REM
REM  Builds a SEPARATE demo store (demo_telemetry.db) holding a synthetic race
REM  that is already an hour old, then starts the dashboard pointed at it.
REM
REM  IT NEVER TOUCHES Pit_Dashboard\telemetry.db. The demo is a different file
REM  reached through SOLARRACE_DB_PATH, and the collector is NOT started, so
REM  nothing here can write to race data or talk to Firebase.
REM
REM  It also uses port 8010 rather than 8000, so this can run at the same time
REM  as the real dashboard without either one standing on the other.
REM ===========================================================================
set "PORT=8010"
set "DEMODB=%~dp0demo_telemetry.db"
set "DIST=Pit_Web\frontend\dist\index.html"

echo.
echo   ============================================================
echo    Afeka Pit Wall - DEMO  (synthetic data, port %PORT%)
echo   ============================================================

REM --- 1) find a usable Python ------------------------------------------------
REM Same discovery as run_web.bat: python.exe on PATH may be the Microsoft
REM Store alias stub, which is a 0-byte file that opens the Store instead of
REM running anything.
set "PYCMD="
for %%V in (3.12 3.11 3.10 3.9) do if not defined PYCMD call :try_launcher %%V
if not defined PYCMD call :try_launcher 3
if not defined PYCMD call :try_where python
if not defined PYCMD call :try_where python3
if not defined PYCMD call :try_exe "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYCMD call :try_exe "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYCMD call :try_exe "%ProgramFiles%\Python312\python.exe"
if not defined PYCMD goto :err_nopython
echo   [OK] Interpreter  : %PYCMD%

REM --- 2) are we in the right folder, and is the UI built? ---------------------
if not exist "Pit_Web\api.py" goto :err_wrongdir
if not exist "%DIST%" goto :err_nodist
echo   [OK] Frontend     : Pit_Web\frontend\dist

REM --- 3) dependencies --------------------------------------------------------
%PYCMD% Pit_Web\check_requirements.py Pit_Web\requirements_web.txt
if errorlevel 1 (
    echo   [i] Installing dependencies ...
    %PYCMD% -m pip install -r Pit_Web\requirements_web.txt
    if errorlevel 1 goto :err_pip
)

REM --- 4) build the demo store ------------------------------------------------
echo.
echo   Building the demo store ...
%PYCMD% tools\demo_seed.py "%DEMODB%"
if errorlevel 1 goto :err_seed

REM --- 5) keep it live --------------------------------------------------------
REM Without this the newest sample ages past DATA_STALE_AFTER_S within ten
REM seconds and the whole dashboard greys out as STALE - correct behaviour
REM reading as a broken demo. The feeder plays the collector's part, and the
REM sector rows then fill in sector by sector the way they will on track.
echo.
echo   Starting the DEMO car feed ...
start "Demo Car Feed" cmd /k %PYCMD% -u tools\demo_feed.py "%DEMODB%"
timeout /t 2 /nobreak >nul

REM --- 6) launch, pointed at the DEMO store -----------------------------------
echo.
echo   Starting the DEMO dashboard on http://localhost:%PORT% ...
start "Pit Web DEMO" cmd /k set "SOLARRACE_DB_PATH=%DEMODB%" ^&^& %PYCMD% -m uvicorn Pit_Web.api:app --host 127.0.0.1 --port %PORT%

echo   Waiting for it to accept connections ...
set /a TRIES=0
:waitport
set /a TRIES+=1
netstat -an | find ":%PORT%" | find "LISTENING" >nul
if not errorlevel 1 goto :portup
if %TRIES% GEQ 60 goto :porttimeout
timeout /t 1 /nobreak >nul
goto :waitport

:porttimeout
echo   [!] Port %PORT% never opened. The real error is in the "Pit Web DEMO" window.
pause
exit /b 1

:portup
start "" "http://localhost:%PORT%/"
echo.
echo   ============================================================
echo    WHAT TO LOOK AT
echo   ============================================================
echo.
echo    Driver Telemetry tab, "Sector times" - the rework:
echo      * Two rows. "LAST LAP" is the finished lap and never goes blank;
echo        "CURRENT" fills in sector by sector as the car goes round.
echo      * PURPLE  S2 and S6 - the best those sectors have been all race.
echo      * GREEN   faster than the same sector one lap ago.
echo      * YELLOW  slower than one lap ago.
echo      * DASHED cell on the current row - the car drove that sector but the
echo        telemetry did not arrive. A 40 s dropout is seeded in S2.
echo      * PLAIN dash - simply not reached yet. Deliberately different from
echo        the dashed one: "we lost it" and "not there yet" are not the same.
echo      * The LAP column carries the lap total, blank until all nine land.
echo.
echo    "Track position" card - the target speed now names the profile it came
echo      from ("from lap93_293s"). If the car had never reported one it would
echo      say "assuming ... car has not reported" in amber instead.
echo.
echo    Sidebar "Race control" - the race has been running about an hour.
echo      * "Correct start time" opens the backdated-start panel.
echo      * Danger zone has "Reset race clock", reversible for two minutes.
echo      * Stop the race and the sector section empties: sector times only
echo        exist while the race clock runs.
echo.
echo    Sidebar "Driver stint" - 108 minutes into a 2 hour limit, so the
echo      countdown is amber with 12 minutes left and the banner is up.
echo      Press "Driver changed" to reset it, then "Undo".
echo.
echo    History tab - an hour of real curves. Zoom into a chart and watch the
echo      zoom SURVIVE the live appends, which is why this app exists.
echo.
echo    The car is DRIVING: sectors land one at a time, the rows swap when the
echo    lap rolls, and purple moves as the driver takes sectors outright.
echo.
echo   Close the "Pit Web DEMO" and "Demo Car Feed" windows to stop. The demo
echo   store is demo_telemetry.db and can be deleted any time.
echo.
pause
exit /b 0

REM --- helpers ----------------------------------------------------------------
:try_launcher
py -%1 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYCMD=py -%1"
exit /b 0

:try_where
where %1 >nul 2>&1
if errorlevel 1 exit /b 0
%1 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYCMD=%1"
exit /b 0

:try_exe
if not exist %1 exit /b 0
%1 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYCMD=%1"
exit /b 0

:err_nopython
echo.
echo   [X] No usable Python found. Install Python 3.11 or 3.12 and tick
echo       "Add python.exe to PATH".
echo.
pause
exit /b 1

:err_wrongdir
echo.
echo   [X] Run this from inside the cloned repository - Pit_Web\api.py was not
echo       found next to this file.
echo.
pause
exit /b 1

:err_nodist
echo.
echo   [X] Pit_Web\frontend\dist was not found, so there is no user interface
echo       to show. Build it once on a machine with Node:
echo           cd Pit_Web\frontend
echo           npm install
echo           npm run build
echo.
pause
exit /b 1

:err_pip
echo.
echo   [X] Installing dependencies failed. The error is above.
echo.
pause
exit /b 1

:err_seed
echo.
echo   [X] Could not build the demo store. The error is above.
echo.
pause
exit /b 1
