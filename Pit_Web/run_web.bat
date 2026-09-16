@echo off
REM ===========================================================================
REM  run_web.bat - starts the React + FastAPI pit dashboard.
REM
REM  Started by "Start Pit Dashboard.bat" at the repo root. It keeps the shape
REM  of the earlier Streamlit dashboard's launcher: same Python discovery, same
REM  dependency stamp, same "each part in its own window" model, so the crew
REM  already knows how it behaves.
REM
REM  PRODUCTION NEEDS PYTHON ONLY. FastAPI static-serves Pit_Web\frontend\dist,
REM  so there is no Node, no npm install and no dev server at the track.
REM
REM  STREAMLIT IS NOT NEEDED. Only the speed-profile builder still uses it, and
REM  "Build Speed Profiles.bat" installs it on top of this environment itself.
REM
REM  COLLECTOR SUPERVISION - decided, not an oversight: collector.py runs in ITS
REM  OWN WINDOW rather than as a child of the API. Supervision would couple the
REM  two lifetimes, so restarting the API to pick up a change would kill the
REM  collector mid-race. A dead collector cannot masquerade as a live dashboard
REM  because the app bar ages the newest sample and goes red past 10 s.
REM ===========================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0.."
set "PORT=8000"
set "REQ=Pit_Web\requirements_web.txt"
set "CHECKER=Pit_Web\check_requirements.py"
set "STAMP=Pit_Web\.deps_stamp"
set "KEYFILE=Pit_Dashboard\serviceAccountKey.json"
set "DIST=Pit_Web\frontend\dist\index.html"

echo.
echo   ============================================================
echo    Afeka Pit Wall - React + FastAPI
echo   ============================================================

REM --- 1) find a usable Python ------------------------------------------------
REM Everything runs as "<python> -m <module>": uvicorn.exe is not necessarily on
REM PATH, and python.exe on PATH may be the Microsoft Store alias stub, which is
REM a 0-byte file that opens the Store instead of running anything.
set "PYCMD="
for %%V in (3.12 3.11 3.10 3.9) do if not defined PYCMD call :try_launcher %%V
if not defined PYCMD call :try_launcher 3
if not defined PYCMD call :try_where python
if not defined PYCMD call :try_where python3
if not defined PYCMD call :try_exe "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYCMD call :try_exe "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYCMD call :try_exe "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PYCMD call :try_exe "%ProgramFiles%\Python312\python.exe"
if not defined PYCMD call :try_exe "%ProgramFiles%\Python311\python.exe"
if not defined PYCMD call :try_exe "C:\Python312\python.exe"
if not defined PYCMD call :try_exe "C:\Python311\python.exe"
if not defined PYCMD goto :err_nopython
echo   [OK] Interpreter  : %PYCMD%
%PYCMD% -c "import sys;print('   [OK] Version      : '+sys.version.split()[0])"

REM --- 2) sanity: are we in the right folder? ---------------------------------
if not exist "%REQ%" goto :err_wrongdir
if not exist "Pit_Web\api.py" goto :err_wrongdir

REM --- 3) dependencies --------------------------------------------------------
REM A stamp of "<sha256 of the requirements file> <full path to python.exe>".
REM Both halves matter: editing the requirements OR switching interpreter must
REM force a reinstall. Delete Pit_Web\.deps_stamp to force one by hand.
set "REQHASH="
for /f "skip=1 delims=" %%H in ('certutil -hashfile "%REQ%" SHA256') do if not defined REQHASH set "REQHASH=%%H"
set "REQHASH=%REQHASH: =%"

set "PYPATH="
for /f "delims=" %%E in ('%PYCMD% -c "import sys;print(sys.executable)"') do set "PYPATH=%%E"
set "WANT=%REQHASH% %PYPATH%"

set "HAVE="
if exist "%STAMP%" set /p HAVE=<"%STAMP%"

if not "%WANT%"=="%HAVE%" goto :install

REM Stamp matches - but verify the packages are really there, in case someone
REM uninstalled one. Reads local package metadata only: no network, so it is
REM fast and works offline. It reads the versions FROM the requirements file, so
REM unlike a hardcoded module list it cannot drift.
REM
REM A real .py file, not an inline `-c "..."`: CMD's own `^` escape and, with
REM delayed expansion on above, its `!` token both get eaten even inside double
REM quotes. See check_requirements.py's header for the exact failure it caused.
%PYCMD% "%CHECKER%" "%REQ%"
if errorlevel 1 goto :install
goto :deps_done

:install
echo   [i] Installing dependencies from %REQ% ...
%PYCMD% -m pip install --disable-pip-version-check --upgrade pip
%PYCMD% -m pip install --disable-pip-version-check --prefer-binary -r "%REQ%"
if errorlevel 1 goto :err_deps
REM Re-verify rather than trusting pip's exit code, and stamp only on success:
REM a half-finished install must not be remembered as good.
%PYCMD% "%CHECKER%" "%REQ%"
if errorlevel 1 goto :err_deps
>"%STAMP%" echo %WANT%
echo   [OK] Packages installed.

:deps_done

REM --- 4) the built frontend --------------------------------------------------
if exist "%DIST%" goto :havedist
echo.
echo   [X] Pit_Web\frontend\dist was not found.
echo       That folder is what FastAPI serves; without it the backend would
echo       start with no user interface. Build it once on a machine with Node:
echo.
echo           cd Pit_Web\frontend
echo           npm install
echo           npm run build
echo.
echo       Then copy dist across. The pit machine itself never needs Node.
echo.
pause
exit /b 1
:havedist
echo   [OK] Frontend     : Pit_Web\frontend\dist

REM --- 5) the telemetry store -------------------------------------------------
REM Absent is survivable: the collector creates it on first run. Say so rather
REM than letting the dashboard come up inexplicably empty.
if exist "Pit_Dashboard\telemetry.db" (
    echo   [OK] Database     : Pit_Dashboard\telemetry.db
) else (
    echo   [!] Database      : none yet - the collector will create it.
)

REM --- 6) Firebase credentials ------------------------------------------------
REM serviceAccountKey.json is a SECRET and is gitignored, so a fresh clone does
REM not have it and collector.py would crash-loop in its own window. Detect it,
REM explain, and start the dashboard anyway: it reads telemetry.db only, so it
REM is still useful for reviewing stored data.
set "HAVEKEY=1"
if not exist "%KEYFILE%" set "HAVEKEY="
if defined HAVEKEY goto :launch
echo.
echo   [!] %KEYFILE% is missing.
echo       It is a secret and is intentionally NOT in the repository.
echo       Starting the DASHBOARD ONLY - no new telemetry will arrive.
echo.

:launch
if not defined HAVEKEY goto :web
echo   Starting pit COLLECTOR (Firebase -^> telemetry.db) ...
start "Pit Collector" cmd /k %PYCMD% -u Pit_Dashboard\collector.py
timeout /t 3 /nobreak >nul

:web
REM 0.0.0.0 so phones and a second laptop on the pit LAN can reach it.
echo   Starting pit WEB DASHBOARD (http://localhost:%PORT%) ...
start "Pit Web" cmd /k %PYCMD% -m uvicorn Pit_Web.api:app --host 0.0.0.0 --port %PORT%

echo   Waiting for the dashboard to accept connections ...
set /a TRIES=0
:waitport
set /a TRIES+=1
netstat -an | find ":%PORT%" | find "LISTENING" >nul
if not errorlevel 1 goto :portup
if %TRIES% GEQ 60 goto :porttimeout
timeout /t 1 /nobreak >nul
goto :waitport

:porttimeout
echo   [!] Port %PORT% never opened. The real error is in the "Pit Web" window.
echo       If Windows Firewall prompted, click Allow and run this again.
goto :done

:portup
start "" http://localhost:%PORT%

:done
echo.
echo   ============================================================
echo    Dashboard : http://localhost:%PORT%
REM The LAN URL, so a phone can be pointed at it
REM without anyone hunting through ipconfig.
for /f "tokens=2 delims=:" %%A in ('ipconfig ^| findstr /c:"IPv4"') do (
    for /f "tokens=1" %%B in ("%%A") do echo    Network   : http://%%B:%PORT%
)
echo.
echo    Each part runs in its own window - close one to stop it.
echo.
echo    KNOWN GOTCHA: campus and venue WiFi often enable client isolation,
echo    which silently blocks other devices from reaching this laptop. Use a
echo    phone hotspot or a dedicated router. It is not an app bug.
echo   ============================================================
echo.
pause
exit /b 0

:try_launcher
if defined PYCMD exit /b
py -%1 -c "import sys" >nul 2>&1 && set "PYCMD=py -%1"
exit /b
:try_where
if defined PYCMD exit /b
where %1 >nul 2>&1 || exit /b
REM The Store alias stub is 0 bytes and exits without running anything, so a
REM successful `where` is not proof of a usable interpreter.
%1 -c "import sys" >nul 2>&1 && set "PYCMD=%1"
exit /b
:try_exe
if defined PYCMD exit /b
if exist %1 set "PYCMD=%1"
exit /b

:err_nopython
echo.
echo   [X] No usable Python found. Install Python 3.11 from python.org and tick
echo       "Add python.exe to PATH", then run this again.
pause
exit /b 1

:err_wrongdir
echo.
echo   [X] This does not look like the THE-RACE-react folder: %REQ% is missing.
echo       Run Start React Dashboard.bat from inside the cloned repository.
pause
exit /b 1

:err_deps
echo.
echo   [X] Installing dependencies failed - the error is above. Nothing was
echo       started, and the dependency stamp was NOT written, so fixing the
echo       problem and running this again will retry the install.
pause
exit /b 1
