@echo off
REM ===================================================================
REM  Build Speed Profiles - opens the profile builder on port 8502.
REM
REM  A SEPARATE app from the pit dashboard, and the one tool here that
REM  still runs on Streamlit. It reads telemetry.db read-only, so it
REM  cannot slow, lock or crash the dashboard or the collector.
REM
REM  Uses the interpreter run_web.bat found and recorded in
REM  Pit_Web\.deps_stamp, so every pit app runs on the same Python. The
REM  dashboard's own packages come from there; this adds only what the
REM  builder needs on top (Pit_Dashboard\requirements_profiles.txt), and
REM  only when they are missing. It never touches run_web.bat's stamp.
REM ===================================================================
setlocal
cd /d "%~dp0"

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
    echo Installing the profile builder's packages ^(Streamlit, Plotly^) ...
    %PYCMD% -m pip install --disable-pip-version-check --prefer-binary -r Pit_Dashboard\requirements_profiles.txt
    if errorlevel 1 (
        echo.
        echo   [X] pip install failed - see the messages above.
        pause
        exit /b 1
    )
)

cd /d "%~dp0Pit_Dashboard"
echo Starting the Speed Profile Builder on http://localhost:8502
start "Profile Builder" cmd /k %PYCMD% -m streamlit run profile_builder.py --server.port 8502
timeout /t 4 /nobreak >nul
start "" http://localhost:8502
endlocal
