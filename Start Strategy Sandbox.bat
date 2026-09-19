@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
REM ===========================================================================
REM  Start Strategy Sandbox.bat - the Strategy screen, planned from numbers you
REM  type in.
REM
REM  WHY THIS EXISTS. The strategy search takes three things: how much charge
REM  is in the pack, how much of the race is left, and which lap the car is on.
REM  On race day all three come from the car. When the Pi has been silent for
REM  hours the stored SoC is whatever it was before it went quiet, so the plan
REM  on the real dashboard is a plan for a car that no longer exists.
REM
REM  This opens the dashboard on the DEMO store, straight on the Strategy tab,
REM  where "Edit inputs" lets those three be typed. The plan that comes back is
REM  the real engine on the real matrix - only its inputs are yours.
REM
REM  IT IS NOT A DEMO OF THE CAR. No feed is started, so every other screen
REM  here is a frozen hour-old snapshot and says so. That is the point: nothing
REM  on this page is pretending to be live.
REM
REM  IT CANNOT REACH THE CAR OR THE PUBLIC PAGE, and that is enforced, not
REM  assumed. A demo backend reads a different SQLite file, but Firebase has
REM  only ONE car and one spectator page, so a separate store alone never
REM  made it safe. api.car_link() refuses every command off the pit's real
REM  store - Send to car, Cut lap, driver messages, trip reset, and the
REM  green flag, which zeroes the car's laps, distance and energy and would
REM  have reset what docs/index.html shows the public. check_sandbox.py
REM  proves it, and proves the real dashboard still sends.
REM
REM  It never touches Pit_Dashboard\telemetry.db and never starts the
REM  collector. Port 8011, so it can run beside the real dashboard (8000)
REM  and the full demo (8010) without standing on either.
REM ===========================================================================
set "PORT=8011"
set "DEMODB=%~dp0demo_telemetry.db"
set "DIST=Pit_Web\frontend\dist\index.html"

echo.
echo   ============================================================
echo    Afeka Pit Wall - STRATEGY SANDBOX  (typed inputs, port %PORT%)
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

REM --- 4) the demo store, built only if it is not there yet --------------------
REM The backend needs A store to open. This one is not read for anything the
REM sandbox shows once the inputs are typed, so an existing one is left exactly
REM as it is - including a matrix edit or a start time set here earlier.
if exist "%DEMODB%" (
    echo   [OK] Demo store   : already built, left alone
) else (
    echo.
    echo   Building the demo store ...
    %PYCMD% tools\demo_seed.py "%DEMODB%"
    if errorlevel 1 goto :err_seed
)

REM --- 5) launch, pointed at the DEMO store -----------------------------------
REM NO demo feed. Nothing writes to the store while this is open, so every
REM reading outside Strategy stays frozen and the page marks itself STALE,
REM which is the truth.
echo.
echo   Starting the strategy sandbox on http://localhost:%PORT% ...
start "Strategy Sandbox" cmd /k set "SOLARRACE_DB_PATH=%DEMODB%" ^&^& %PYCMD% -m uvicorn Pit_Web.api:app --host 127.0.0.1 --port %PORT%

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
echo   [!] Port %PORT% never opened. The real error is in the "Strategy Sandbox" window.
pause
exit /b 1

:portup
start "" "http://localhost:%PORT%/?tab=Strategy"
echo.
echo   ============================================================
echo    HOW TO USE IT
echo   ============================================================
echo.
echo    It opens on the Strategy tab. Press "Edit inputs" and type:
echo      * Battery SoC      - what the pack is actually at, in percent.
echo      * Time remaining   - 840, or 14:00. Both mean 840 minutes left.
echo      * Lap              - the lap the car is on. Same override as the
echo                           sidebar's "Manual lap"; not a second one.
echo    Leave a field empty and that one comes from the store as before.
echo.
echo    An amber line above the table names every value that was typed and
echo    what it is standing in for, and it stays up while the panel is closed.
echo    "Back to the car's values" clears the lot.
echo.
echo    "Edit matrix" beside it is a different thing and it DOES write:
echo    it changes what a lap costs, in Pit_Dashboard\constants.py, for every
echo    dashboard. "Edit inputs" writes nothing anywhere.
echo.
echo    The other tabs are an hour-old frozen snapshot and are marked STALE.
echo    For the moving demo with sectors and history, use
echo    "Start Demo Dashboard.bat" instead.
echo.
echo   Close the "Strategy Sandbox" window to stop.
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
