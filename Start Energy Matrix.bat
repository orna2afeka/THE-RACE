@echo off
REM ===================================================================
REM  Energy Matrix - what each speed profile COSTS, on port 8504.
REM
REM  Port map:  8000 pit dashboard   8010 demo dashboard
REM             8502 profile builder  8503 pit wall (TV)  8504 this
REM
REM  8504 BECAUSE 8503 IS THE PIT WALL. This launcher shipped on 8503
REM  for about an hour and, with the wall already running, Streamlit
REM  could not bind - so the browser opened on the wall instead and
REM  this app appeared to "be" the pit wall. Hence the check below:
REM  every other launcher here opens the browser on a timer whether or
REM  not the server came up, which turns a busy port into a confusing
REM  page rather than an error.
REM
REM  A SEPARATE app from the pit dashboard and from the Speed Profile
REM  Builder. It opens telemetry.db read-only and writes nothing at
REM  all - not constants.py, not the profiles - so it cannot slow,
REM  lock or crash the dashboard or the collector.
REM
REM  Uses the interpreter run_web.bat found and recorded in
REM  Pit_Web\.deps_stamp, so every pit app runs on the same Python. Its
REM  packages are the profile builder's (Streamlit, Plotly), installed
REM  only when missing. It never touches run_web.bat's stamp.
REM ===================================================================
setlocal
cd /d "%~dp0"

set "PORT=8504"

REM --- is the port already taken? -----------------------------------
REM  LISTENING only: a stray TIME_WAIT from a previous run is not a
REM  server and must not stop this one.
netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo.
    echo   [X] Something is already listening on port %PORT%.
    echo.
    echo       Close that window and try again, or find out what it is:
    echo         netstat -ano ^| findstr :%PORT%
    echo.
    echo       Port map:  8000 pit dashboard   8010 demo dashboard
    echo                  8502 profile builder  8503 pit wall  %PORT% this
    echo.
    pause
    exit /b 1
)

set "PYCMD="
if exist "Pit_Web\.deps_stamp" (
    for /f "usebackq tokens=1,*" %%A in ("Pit_Web\.deps_stamp") do set "PYCMD="%%B""
)
if not defined PYCMD (
    echo No Pit_Web\.deps_stamp found - run "Start Pit Dashboard.bat" once first
    echo so the Python environment is set up. Trying "py -3" for now.
    echo.
    set "PYCMD=py -3"
)

%PYCMD% Pit_Web\check_requirements.py Pit_Dashboard\requirements_profiles.txt >nul 2>&1
if errorlevel 1 (
    echo Installing the packages this needs ^(Streamlit, Plotly^) ...
    %PYCMD% -m pip install --disable-pip-version-check --prefer-binary -r Pit_Dashboard\requirements_profiles.txt
    if errorlevel 1 (
        echo.
        echo   [X] pip install failed - see the messages above.
        pause
        exit /b 1
    )
)

cd /d "%~dp0Pit_Dashboard"
echo Starting the Energy Matrix on http://localhost:%PORT%
start "Energy Matrix" cmd /k %PYCMD% -m streamlit run energy_matrix.py --server.port %PORT%

REM --- only open the browser once the server is actually up ---------
REM  Ten one-second tries. Opening on a fixed timer is what let the
REM  8503 clash show the wrong app: the browser went to the port
REM  regardless of whether this server had come up on it.
set "UP="
for /L %%i in (1,1,10) do (
    if not defined UP (
        timeout /t 1 /nobreak >nul
        netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>&1
        if not errorlevel 1 set "UP=1"
    )
)
if not defined UP (
    echo.
    echo   [!] Port %PORT% never came up - see the "Energy Matrix" window
    echo       for the error. Not opening a browser.
    echo.
    pause
    exit /b 1
)
start "" http://localhost:%PORT%
endlocal
