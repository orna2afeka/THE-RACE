@echo off
REM ===================================================================
REM  Start Pit Wall - the big-screen circuit view, on port 8503.
REM
REM  For a TV in the garage. Plain Python, no Streamlit: it serves one
REM  page and one JSON endpoint, reads telemetry.db READ-ONLY on a
REM  thread of its own, and cannot slow, lock or crash the dashboard,
REM  the collector or the profile builder.
REM
REM  Port map:  8000 pit dashboard   8502 profile builder   8503 this
REM             8010 demo dashboard   8504 energy matrix
REM
REM  The page itself is Pit_Dashboard\wall.html, written by
REM  tools\build_zolder_animation.py. It is NOT published to GitHub
REM  Pages: it carries pack voltage, temperatures and lap deltas, and
REM  it stays on the pit LAN.
REM
REM  Reuses the interpreter run_web.bat already found and recorded in
REM  Pit_Web\.deps_stamp, so every pit app runs on the same Python.
REM  Deliberately does NOT run pip and does NOT touch run_web.bat: the
REM  thing that gets the pit dashboard up must stay untouched.
REM ===================================================================
setlocal
cd /d "%~dp0"

set "PYCMD="
if exist "Pit_Web\.deps_stamp" (
    for /f "usebackq tokens=1,*" %%A in ("Pit_Web\.deps_stamp") do set "PYCMD="%%B""
)
if not defined PYCMD set "PYCMD=py -3"
if not exist "Pit_Web\.deps_stamp" (
    echo No .deps_stamp found - run "Start Pit Dashboard.bat" once first so the
    echo Python environment is set up, then come back here.
    echo.
)

if not exist "Pit_Dashboard\wall.html" (
    echo wall.html is missing - building it...
    %PYCMD% tools\build_zolder_animation.py
    echo.
)

REM  No car yet? Run  python tools\pit_wall.py --demo  to drive the page
REM  from the base_210s profile and set the TV up without a session.
REM  The page says DEMO - NOT LIVE DATA the whole time it runs.

echo Starting the Pit Wall on http://localhost:8503
echo The console window that opens lists the LAN addresses a TV can use.
start "Pit Wall" cmd /k %PYCMD% tools\pit_wall.py
timeout /t 3 /nobreak >nul
start "" http://localhost:8503
endlocal
