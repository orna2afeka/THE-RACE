# Prompt: React + FastAPI rewrite of the Afeka pit dashboard

Paste everything below the line into a fresh Claude Code session opened at the repo root
(`THE RACE`). It is written to be handed over cold.

---

## Goal

Replace the Streamlit pit dashboard (`Pit_Dashboard/pit_dashboard.py`, ~1800 lines) with a
React frontend served by a FastAPI backend. The car software in `SolarRace_OS/` is **out of
scope — do not touch it.**

Architecture, already decided:

- **FastAPI** backend. **WebSocket** push for live telemetry, so there is no HTTP polling.
- FastAPI also **serves the production React build as static files**, so the pit machine
  needs Python only — no Node.js, no `npm start`, no dev server in production.
- One `.bat` launcher, matching the existing `Start Pit Dashboard.bat`.

Before writing code, read this document fully, then explore the repo and come back with a
plan. Several things below are the result of hard-won bug fixes and will be silently undone
by a naive port.

## Why this rewrite exists

The Streamlit execution model reruns a whole panel to update any part of it. For the History
charts that meant every refresh destroyed the chart and any zoom/pan/hover with it — you
could not examine a graph. Streamlit hashes the entire chart figure into the element id and
the browser uses that id as its React key, so a data change always remounts the plot; `key=`
and Plotly's `uirevision` cannot prevent it.

In React this problem does not exist: new samples get appended to a live chart instance and
zoom lives in client state. **That is the main thing the rewrite buys.** A workaround is
currently in place on the Streamlit side (an explicit freeze mode) — do not port the freeze
machinery as-is. A simple pause toggle is enough once the chart is yours.

## What must NOT regress

These are the parts that took real debugging. Read the comments around each before changing
anything near them.

### 1. Missing readings are `null`, never `0`

Recently fixed across both apps and it is the easiest thing to undo with a stray `|| 0`.

A metric the CAN bus never reported is **unknown**, not zero. `0 °C` reads as a cold motor,
`0 V` as a flat pack, `0 A` as a coasting car, and averaging those zeros corrupted stint
statistics. The convention now:

- The car omits absent keys entirely (`main.py` builds `vehicle_state` from empty dicts and
  only `.update()`s with keys a parser actually produced).
- `LapTracker.snapshot()` publishes `null` for distance/lap/energy until something has
  actually fed them (`_have_distance` / `_have_energy` flags).
- `Pit_Dashboard/db.py` maps absent → `None` → SQL `NULL`.
- `read_history_df()` keeps `None` → pandas `NaN`, so charts draw a **gap** and
  `min`/`mean`/`max` skip it.
- Every displayed value goes through `fmt()` in `pit_dashboard.py`, which renders `None` as
  an **em dash `—`**. Severity helpers (`_over_cond`) treat unknown as neutral — an absent
  reading is not evidence of a safe temperature *or* an alarming one.
- CSV export writes an **empty cell**, never `0`.

In React: keep `null` all the way through JSON, type these fields `number | null`, and render
`—`. Never `value ?? 0`. A chart must break the line at `null`, not plot zero
(`connectNulls: false` or equivalent).

### 2. Do not fork the physics constants into JavaScript

The repo has already been burned by this: `speed_kmh` was open-coded in the pit with
different constants than the HUD used, and the two screens disagreed about the car's speed.
There is a comment recording it at the `state["speed_kmh"]` assignment.

Python owns the constants — `drivetrain.py` (gear ratio, tire diameter, `speed_kmh`,
`distance_metres`), `track.py` (track length, lap distance gates, finish-line radii),
`Pit_Dashboard/constants.py` (temperature thresholds, `STRATEGIES`, section names/risk),
`speed_profile.py`. Do all derivation server-side and expose values already computed, plus a
`GET /api/config` for thresholds and section metadata the UI needs for styling. The frontend
must never re-implement a formula.

Note `drivetrain.TIRE_DIAMETER_METERS` is flagged in its own file as an unmeasured
placeholder. Do not "fix" it or build around it — it is a known open item.

### 3. SQLite is the only source of truth

`collector.py` is the single process that talks to Firebase; it writes `telemetry.db`. The
dashboard **never** opens a Firebase connection — it reads SQLite only. Keep that boundary
exactly. The backend reads the same DB through the existing `Pit_Dashboard/db.py` helpers
(`fetch_samples`, `latest_sample`, `fetch_lap_summary`, `fetch_faults`, `time_bounds`,
`count_samples`, `count_samples_since`) rather than writing new SQL.

Not everything is read-only. These write and must keep working: race start/resume state
(`save_race_state` / `load_race_state`), the driver message panel, "cut lap now", strategy
selection sent to the car, and `clear_history`. `clear_history` is destructive and currently
sits behind a popover-plus-confirm so it cannot be hit mid-race — keep an equivalent guard.

### 4. Cadences exist for a reason

Two tiers, arrived at after the page kept stalling: fast tiles at 2s (fault banner, metric
strip, driver telemetry, sector display), heavy panels at 10s (history charts, weather,
strategy). Weather is cached for an hour; history reads are cached ~8s; fault episodes 30s.

With a WebSocket you can push the 2s tier as it changes. For history, **do not resend the
whole series every 10s** — send an incremental append of new samples and let the client push
them into the existing chart. That is the entire performance argument for React. Keep a
`GET` endpoint for the initial window load and for changing the time range.

## The features to carry over

Work through `pit_dashboard.py` and reproduce all of it. Summary of the surface:

- **Always-visible top strip** — fault banner (BMS/MMS faults decoded via
  `decode_error_bits`, with `BMS_PROTECTION_BITS` / `MMS_ERROR_BITS`), plus metric tiles:
  speed, motor temp (PT1000 °C with raw Ω beside it), controller temp, battery SoC, battery
  temp, power out, lap distance. Second row: last lap time, last lap energy, total race
  energy.
- **Sidebar** — live/stale indicator with age, time remaining, active lap, lap delta, gap to
  competitor, distance, sector. Race start/resume. Manual lap override. Rival lap count.
  Dark/light toggle. Font-size scaling. Excel export panel.
- **Driver Telemetry tab** — track position card with the velocity-profile target speed for
  the current point, a sector strip (past/current/future segments with warn/crit tints and a
  pulse animation), next-feature panel, sector split times, live GPS map. Note `has_gps` is
  deliberately separate from lat/lon: the map falls back to the Zolder paddock so it has
  somewhere to centre, and `has_gps` is what says whether the pin is real. `0,0` is a real
  place in the Atlantic — never let a placeholder look like a fix.
- **History tab** — one large multi-metric chart (13 available metrics, each with a fixed
  series colour in `HISTORY_CHARTS`), time-window presets, metric picker, raw-units vs
  normalized scale, min/avg/max/now stats per metric, per-range CSV export in two flavours,
  recent-samples table, per-lap energy/time charts, fault-episode history, reset-history.
- **Weather tab** — solar irradiance forecast via `weather_service.fetch_zolder_weather`.
- **Strategy tab** — consumption matrix from `constants.STRATEGIES` and
  `strategy_engine.calculate_all_strategies`, plus a combined graph. **This currently renders
  server-side with matplotlib** (`create_combined_graph` → `st.pyplot`). Decide explicitly:
  either keep serving it as a PNG endpoint (less work, keeps one implementation) or rebuild it
  client-side (interactive, but now two chart stacks). Ask before choosing.

### Chart series colours

`HISTORY_CHARTS` in `pit_dashboard.py` assigns each metric a fixed hex, and
`export.py::_XLSX_COLS` reuses the same hexes so the screen and the exported workbook agree.
Serve that palette from the API; do not retype the hexes in the frontend.

### Exports

`Pit_Dashboard/export.py` has the Excel writer (3 sheets: Data, Charts, Faults, via openpyxl)
and the CSV builders (`history_csv_bytes` with `data` / `report` styles). **Keep these in
Python** and serve the bytes — do not rebuild spreadsheet generation in JavaScript. Two
details in there that matter: `_safe()` neutralises CSV/Excel formula injection on text
cells, and output is encoded `utf-8-sig` because Excel needs the BOM or it mangles the `°C`
and `Ω` in the headers.

## Deployment constraints

- **Runs on a Windows laptop at the track, frequently offline.** No CDNs, no external fonts,
  no runtime package fetches — bundle everything into the build. Verify with the network
  disconnected.
- **Multiple devices watch it over the pit LAN** (phones, tablets, a second laptop). Bind
  `0.0.0.0` and print the LAN URL on startup the way Streamlit does. Known gotcha worth
  documenting in the README: campus and venue WiFi often enable client isolation, which
  silently blocks other devices from reaching the laptop — a phone hotspot or a dedicated
  router is the workaround. Do not spend time debugging this as an app bug.
- **The layout must work at 800×480** as well as on a laptop; that is the panel size the car
  side targets and the pit is often read on a small screen. Test narrow.
- **The `.bat` must handle the collector.** `collector.py` has to be running or there is no
  data. Today it is a separate manual step. Propose one of: the `.bat` launches both
  processes, or FastAPI supervises the collector as a subprocess in its lifespan. Recommend
  one with reasoning — a collector crash must not silently leave a dashboard showing stale
  data as if it were live, which is why the staleness age indicator exists.
- Local network only: no auth, no HTTPS, no multi-tenancy. Do not add them.

## Suggested phasing

Do not attempt this in one pass. Suggested order, with the Streamlit app left working
throughout so there is always a usable dashboard before a race:

1. FastAPI backend exposing the full read API over `db.py` + a `/api/config`, with the
   existing Streamlit app untouched. Verify payloads against what Streamlit displays.
2. WebSocket push for the fast tier; validate `null` handling end to end.
3. React app: top strip + sidebar + Driver Telemetry.
4. History tab — the reason for the rewrite. Prove zoom survives a live append.
5. Weather + Strategy + exports.
6. Static-serve the build from FastAPI, write the `.bat`, test offline and from a phone.
7. Retire the Streamlit app only once the React one has run a full session.

## How to start

Explore first, then produce a plan covering: endpoint list with response shapes, the
WebSocket message protocol, the React state/data-fetching approach, the charting library
choice with reasoning, the project layout, and the build/serve wiring. Flag anything in this
document that turns out to be wrong about the code. Ask before picking the charting library
and before deciding the collector-supervision question.
