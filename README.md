# THE-RACE-react — the React pit dashboard

This folder holds **one thing**: the React + FastAPI pit dashboard, and the
Python modules it imports. It is a working copy on branch `react-migration`,
kept local and never pushed.

**The race-day software lives in `../THE RACE`.** The Streamlit pit wall, the
car software (`SolarRace_OS`), the spectator page, the deployment files and the
profile builder are all there, and that is where they are run from. None of
them are here, on purpose — two copies of the Streamlit app in two folders was
confusing, and this one is not the one that runs the race.

## Run it

```bash
pip install -r Pit_Web/requirements_web.txt
python Pit_Dashboard/collector.py                  # Firebase -> telemetry.db
python -m uvicorn Pit_Web.api:app --host 0.0.0.0 --port 8000
```

Or just double-click **`Start React Dashboard.bat`**, which is the intended
way. It finds a usable Python, installs the requirements the first time (and
only when they actually change — a SHA-256 stamp of the requirements file plus
the interpreter path), verifies the frontend build and the database are there,
starts the collector and the dashboard in their own windows, waits for the port
and opens a browser. It prints the LAN URLs so a phone can be pointed at it.

Every preflight step reports itself, and every failure says what to do:

```
  [OK] Interpreter  : py -3.12
   [OK] Version      : 3.12.0
   [OK] Packages     : all 11 requirements satisfied
  [OK] Frontend     : Pit_Webrontend\dist
  [OK] Database     : Pit_Dashboard	elemetry.db
```

Delete `Pit_Web/.deps_stamp` to force a reinstall by hand.

See `Pit_Web/README.md` for everything else — replay mode, the cadences, the
offline behaviour, the driver-stint timer.

**Streamlit is not installed and not required.** Nothing here imports it.

## What is in here, and why

| Path | Why it is here |
|---|---|
| `Pit_Web/` | the dashboard: FastAPI backend + React frontend |
| `Pit_Dashboard/collector.py` | the ingest process — no data without it |
| `Pit_Dashboard/db.py` | every telemetry read goes through its helpers |
| `Pit_Dashboard/export.py` | the Excel/CSV writers; never rebuilt in JS |
| `Pit_Dashboard/constants.py`, `pit_config.py` | sections, strategies, paths, the export timezone rule |
| `Pit_Dashboard/metrics.py`, `live_metrics.py` | the metric catalogues the API serves |
| `Pit_Dashboard/strategy_engine.py`, `weather_service.py` | strategy maths and the forecast |
| `Pit_Dashboard/driver_message.py` | the pit's only Firebase write |
| `Pit_Dashboard/memo.py` | a 30-line stand-in for `st.cache_data`, so the two files above need no Streamlit |
| `limits.py`, `drivetrain.py`, `track.py`, `speed_profile.py`, `efficiency.py` | shared with the car; thresholds and physics |
| `profiles/` | the generated speed profiles `constants.load_strategies()` reads |
| `tools/check_limits.py`, `replay_limits.py` | they test `limits.py`, which the tier colours depend on |

## Keeping it in step with the original

Everything except `Pit_Web/` was synced byte-for-byte from `../THE RACE` on
2026-09-14 (commit `d5560e3`), then reduced to the list above. Three files
deliberately differ from the original now:

- `strategy_engine.py`, `weather_service.py` — `@st.cache_data` replaced with
  `memo` so this folder needs no Streamlit
- `metrics.py`, `live_metrics.py` — they exist only here

When the original changes a shared module, re-copy it. If it changes the metric
catalogue, `metrics.py` and `live_metrics.py` must be regenerated to match — and
they will tell you: their drift guards read
`../THE RACE/Pit_Dashboard/pit_dashboard.py` directly (override with
`SOLARRACE_PIT_DASHBOARD`), so the backend refuses to start if the catalogues
disagree with the live Streamlit app next door. If that folder is missing the
guards stay quiet, because they genuinely cannot check.
