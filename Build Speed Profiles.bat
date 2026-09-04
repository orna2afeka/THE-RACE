@echo off
REM ===================================================================
REM  Build Speed Profiles - opens the profile builder on port 8502.
REM
REM  A SEPARATE app from the pit wall. It reads telemetry.db read-only,
REM  so it cannot slow, lock or crash the dashboard or the collector.
REM  Deliberately does NOT run pip and does NOT touch run_pit.bat: the
REM  thing that gets the pit wall up must stay untouched.
REM
REM  Reuses the interpreter run_pit.bat already found and recorded in
REM  Pit_Dashboard\.deps_stamp, so both apps run on the same Python
REM  with the same installed packages.
REM ===================================================================
setlocal
cd /d "%~dp0Pit_Dashboard"

set "PYCMD="
if exist ".deps_stamp" (
    for /f "tokens=2*" %%A in (.deps_stamp) do set "PYCMD=%%B"
)
if not defined PYCMD set "PYCMD=py -3"
if not exist ".deps_stamp" (
    echo No .deps_stamp found - run "Start Pit Dashboard.bat" once first so the
    echo Python environment is set up, then come back here.
    echo.
)

echo Starting the Speed Profile Builder on http://localhost:8502
start "Profile Builder" cmd /k %PYCMD% -m streamlit run profile_builder.py --server.port 8502
timeout /t 4 /nobreak >nul
start "" http://localhost:8502
endlocal
