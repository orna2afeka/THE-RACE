@echo off
REM ===========================================================================
REM  run_pit.bat - one-click pit-wall launcher (Windows)
REM
REM  Goal: clone the repo, double-click "Start Pit Dashboard.bat", and the
REM  dashboard comes up. No manual setup, no reading the README first.
REM
REM  It:
REM    1. finds a USABLE Python (py launcher -> PATH -> known install dirs),
REM       rejecting Microsoft Store alias stubs and versions our pinned wheels
REM       do not support;
REM    2. accepts the requirements file under either of its two names;
REM    3. installs packages only when they are actually missing or changed;
REM    4. launches everything as "<python> -m ...", never a bare `streamlit`;
REM    5. waits for the port to LISTEN before opening the browser.
REM
REM  No admin rights needed. The FIRST run needs an internet connection.
REM ===========================================================================
setlocal enabledelayedexpansion
title Pit Dashboard Launcher
pushd "%~dp0"

set "PORT=8501"
set "STAMP=.deps_stamp"
set "KEYFILE=serviceAccountKey.json"
set "PYCMD="
set "REQ="

echo.
echo   ============================================================
echo    SOLAR RACE - PIT WALL LAUNCHER
echo   ============================================================
echo.

REM ---------------------------------------------------------------------------
REM 1) Requirements file. Canonical name is requirements_pit.txt, but everyone
REM    expects requirements.txt, so accept either here or one level up.
REM ---------------------------------------------------------------------------
if exist "requirements_pit.txt"  set "REQ=requirements_pit.txt"
if not defined REQ if exist "requirements.txt"    set "REQ=requirements.txt"
if not defined REQ if exist "..\requirements.txt" set "REQ=..\requirements.txt"
if not defined REQ goto :err_noreq
echo   [i] Requirements  : %REQ%

REM ---------------------------------------------------------------------------
REM 2) Find a usable interpreter.
REM
REM    Two failure modes this exists to prevent, both of which have actually
REM    bitten this project:
REM
REM    * A bare `streamlit` command fails whenever Python's Scripts\ folder is
REM      not on PATH. Everything below therefore runs as `<python> -m <module>`.
REM    * `python.exe` on PATH may be a Microsoft Store alias stub: a 0-byte
REM      reparse point that opens the Store instead of running Python. We check
REM      the file SIZE and skip those without ever executing them.
REM
REM    Version gate: numpy/pandas/matplotlib are pinned to versions that publish
REM    wheels for Python 3.9-3.12 only. On 3.13+ pip falls back to compiling
REM    numpy from source and dies in a wall of MSVC errors. Refusing early with
REM    a clear message is far kinder than that.
REM ---------------------------------------------------------------------------
echo   [i] Looking for Python 3.9-3.12 ...
for %%V in (3.12 3.11 3.10 3.9) do if not defined PYCMD call :try_launcher %%V
if not defined PYCMD call :try_launcher 3
if not defined PYCMD call :try_where python
if not defined PYCMD call :try_where python3
if not defined PYCMD call :try_exe "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYCMD call :try_exe "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYCMD call :try_exe "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PYCMD call :try_exe "%LOCALAPPDATA%\Programs\Python\Python39\python.exe"
if not defined PYCMD call :try_exe "%ProgramFiles%\Python312\python.exe"
if not defined PYCMD call :try_exe "%ProgramFiles%\Python311\python.exe"
if not defined PYCMD call :try_exe "C:\Python312\python.exe"
if not defined PYCMD call :try_exe "C:\Python311\python.exe"
if not defined PYCMD goto :err_nopython

echo   [OK] Interpreter  : %PYCMD%
%PYCMD% -c "import sys;print('   [OK] Version      : '+sys.version.split()[0])"

REM ---------------------------------------------------------------------------
REM 3) Dependency check. Reinstall when the requirements file changed, OR the
REM    interpreter changed, OR a pinned version is not actually installed.
REM
REM    The stamp holds "<sha256 of requirements> <full path to python.exe>", so
REM    switching interpreters correctly forces a reinstall into the new one.
REM    Delete .deps_stamp to force a reinstall by hand.
REM ---------------------------------------------------------------------------
set "REQHASH="
for /f "skip=1 delims=" %%H in ('certutil -hashfile "%REQ%" SHA256') do if not defined REQHASH set "REQHASH=%%H"
set "REQHASH=%REQHASH: =%"

set "PYPATH="
for /f "delims=" %%E in ('%PYCMD% -c "import sys;print(sys.executable)"') do set "PYPATH=%%E"
set "WANT=%REQHASH% %PYPATH%"

set "HAVE="
if exist "%STAMP%" set /p HAVE=<"%STAMP%"

if not "%WANT%"=="%HAVE%" goto :install

REM Stamp matches - but verify the packages are really present, in case someone
REM uninstalled one. This reads local package metadata only; it never goes to
REM the network, so it is fast and works offline. It reads the versions FROM the
REM requirements file, so unlike a hardcoded module list it cannot drift.
%PYCMD% -c "import re,sys;from importlib.metadata import distributions;have={(d.metadata['Name'] or '').lower().replace('_','-'):d.version for d in distributions()};want=[m for m in (re.match(r'([A-Za-z0-9_.\-]+)==([^\s;#]+)',l.split('#')[0].strip()) for l in open(r'%REQ%',encoding='utf-8')) if m];bad=[m.group(1) for m in want if have.get(m.group(1).lower().replace('_','-'))!=m.group(2)];print('   [i] Need install  : '+', '.join(bad)) if bad else print('   [OK] Packages     : already match %REQ%');sys.exit(1 if bad else 0)"
if errorlevel 1 goto :install
goto :deps_done

:install
echo.
echo   Installing packages from %REQ% ...
echo   (first run only, or after requirements/Python change - needs internet)
echo.
%PYCMD% -m pip install --disable-pip-version-check --upgrade pip
%PYCMD% -m pip install --disable-pip-version-check --prefer-binary -r "%REQ%"
if errorlevel 1 goto :err_pip
>"%STAMP%" echo %WANT%
echo.
echo   [OK] Packages installed.

:deps_done

REM ---------------------------------------------------------------------------
REM 4) Firebase credentials. serviceAccountKey.json is a SECRET and is
REM    deliberately gitignored, so a fresh clone does not have it and
REM    collector.py would crash-loop in its own window with a confusing
REM    traceback. Detect it, explain, and start the dashboard anyway - it reads
REM    telemetry.db only, so it is still useful for reviewing stored data.
REM ---------------------------------------------------------------------------
set "HAVEKEY=1"
if not exist "%KEYFILE%" set "HAVEKEY="
if defined HAVEKEY goto :launch
echo.
echo   [!] %KEYFILE% is missing from:
echo         %CD%
echo       It is a secret and is intentionally NOT in the repository.
echo       Ask the team for it, drop it in that folder, then run this again.
echo       Starting the DASHBOARD ONLY - no new telemetry will arrive.
echo.

:launch
if not defined HAVEKEY goto :dash
echo   Starting pit COLLECTOR (Firebase -^> telemetry.db) ...
start "Pit Collector" cmd /k %PYCMD% -u collector.py
timeout /t 3 /nobreak >nul

:dash
echo   Starting pit DASHBOARD (http://localhost:%PORT%) ...
start "Pit Dashboard" cmd /k %PYCMD% -m streamlit run pit_dashboard.py

REM Wait for the port to actually accept connections rather than guessing with
REM a fixed sleep - on a cold start Streamlit can take much longer than 5s.
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
echo   [!] Port %PORT% never opened. The real error is in the "Pit Dashboard"
echo       window. If Windows Firewall prompted, click Allow and rerun.
goto :done

:portup
start "" http://localhost:%PORT%

:done
echo.
echo   ============================================================
echo    Dashboard : http://localhost:%PORT%
echo    Each part runs in its own window - close one to stop it.
echo   ============================================================
echo.
popd
endlocal
exit /b 0

REM ===========================================================================
REM  Subroutines
REM ===========================================================================

:try_launcher
REM %1 = version for the py launcher (3.12, or plain 3). The launcher lives in
REM C:\Windows and is on PATH even when python.exe is not, so it is tried first.
where py >nul 2>nul
if errorlevel 1 goto :eof
py -%~1 -c "import sys;sys.exit(0 if (3,9)<=sys.version_info[:2]<=(3,12) else 1)" >nul 2>nul
if errorlevel 1 goto :eof
set "PYCMD=py -%~1"
goto :eof

:try_where
REM %1 = command to resolve on PATH. Tests EVERY hit, not just the first, since
REM the first is often the Store stub.
for /f "delims=" %%A in ('where %~1 2^>nul') do if not defined PYCMD call :try_exe "%%~fA"
goto :eof

:try_exe
REM %1 = full path to a candidate python.exe
if not exist "%~1" goto :eof
REM Microsoft Store alias stubs are 0-byte reparse points that open the Store.
REM Reject by SIZE, so we never execute one and never pop the Store window.
for %%A in ("%~1") do if %%~zA EQU 0 goto :eof
"%~1" -c "import sys;sys.exit(0 if (3,9)<=sys.version_info[:2]<=(3,12) else 1)" >nul 2>nul
if errorlevel 1 goto :eof
set PYCMD="%~1"
goto :eof

REM ===========================================================================
REM  Error exits
REM ===========================================================================

:err_noreq
echo.
echo   [X] No requirements file found. Expected one of:
echo         %CD%\requirements_pit.txt
echo         %CD%\requirements.txt
echo       Did the clone finish? Nothing was started.
echo.
pause
popd
endlocal
exit /b 1

:err_nopython
echo.
echo   [X] No usable Python found.
echo.
echo       This project needs Python 3.9 - 3.12 (64-bit).
echo       3.13 and newer do NOT work yet: the pinned numpy / pandas /
echo       matplotlib versions publish no wheels for them, so pip would try to
echo       build them from source and fail.
echo.
echo       Install Python 3.12 from https://www.python.org/downloads/
echo       Tick "Add python.exe to PATH" and "py launcher" during install.
echo       Avoid the Microsoft Store version.
echo.
echo       Already installed? See what this machine has:   py -0p
echo.
pause
popd
endlocal
exit /b 1

:err_pip
echo.
echo   [X] pip install failed - the error is above. Nothing was started.
echo       Usual causes: no internet on this first run, a corporate proxy
echo       blocking pypi.org, or a full disk. Fix and run this again.
echo.
pause
popd
endlocal
exit /b 1
