# React + FastAPI pit dashboard — execution plan

**Read this file first, then `docs/REACT_MIGRATION_PROMPT.md` in full.** This document is
the *how and where*; that one is the *what and why not to break it*. Neither replaces the
other, and the older one has gone stale in specific ways listed in section 3 below.

Written 2026-08-23. Race is at Circuit Zolder; the team travels ~14 September. Budget is one
week to build, two weeks to test.

---

## 1. Where the work happens — a separate folder, not this one

**The existing folder is the race-day backup and must keep working.** Do not build the React
app inside it. The Streamlit dashboard is currently the only way to run a race, and it stays
that way until the React one has survived a full multi-hour session.

The plan is *not* to retire Streamlit at all. Two dashboards, both working, one known-good.

### Create the working copy

Use `git clone`, not a file copy — you want history, branches and a push path:

```bash
cd ~/Desktop/Afeka/Year\ 3/Solar\ race
git clone "THE RACE" THE-RACE-react
cd THE-RACE-react
git checkout -b react-migration
```

No spaces in the new folder name, deliberately. The existing path has a space in it and it
has cost time in shell quoting more than once.

The clone shares no files with the original, so nothing you do can break the backup. When a
phase is done, `git push origin react-migration` — never merge to `main` until after the
race.

### Which telemetry.db to develop against

`telemetry.db` is gitignored, so the clone arrives with **no database**. Two modes:

**Development (default).** Copy a snapshot in, so nothing you do can touch race data:

```bash
cp "../THE RACE/Pit_Dashboard/telemetry.db.20260823_071308.rpmfix.bak" \
   Pit_Dashboard/telemetry.db
```

That snapshot has 46,836 real samples across two months, which is enough to exercise every
chart, every gap, and the timezone switch.

**Integration testing.** Point the new backend at the *live* database read-only, so both
dashboards show the same car at once. `Pit_Dashboard/pit_config.py` currently hardcodes:

```python
SQLITE_PATH = os.path.join(_HERE, "telemetry.db")
```

Add an environment override in the clone (`SOLARRACE_DB_PATH`), and open it with
`file:...?mode=ro` from the backend. SQLite in WAL mode allows many readers alongside the
collector's single writer, so this is safe — but only if the backend never writes. The
write endpoints (section 6) must therefore be pointed at the dev copy, never at the live DB
during a race.

### Running both at once

| App | Port | Launcher |
|---|---|---|
| Streamlit (backup) | 8501 | `Start Pit Dashboard.bat` in the ORIGINAL folder |
| FastAPI + React | 8000 | new launcher in the clone |

Streamlit's port is set in `Pit_Dashboard/.streamlit/config.toml`. Leave it alone; give
FastAPI 8000.

---

## 2. Decisions already made — do not re-litigate these

These were decided with the team on 2026-08-23. Reopening them costs a day.

| Decision | Choice | Why |
|---|---|---|
| Chart library | **Plotly.js** | Only option where the existing chart config ports over: fixed series colours, two-axis plan, unified hover, box-select-to-zoom, and `rangebreaks`. `extendTraces` appends without remounting and zoom survives natively — the bug was *Streamlit remounting the element*, not Plotly. If SVG becomes a bottleneck, switch the traces to `scattergl` for WebGL; that is the performance escape hatch. |
| Strategy tab graph | **Serve matplotlib as a PNG endpoint** | Rebuilding it client-side means duplicating strategy maths into JavaScript, which is what the brief forbids and what burned this repo when `speed_kmh` was forked. One implementation, server-side. |
| Live Metrics tab | **Port it** | Postdates the brief. It is a flat grid of tiles off the WebSocket, no charts — the easiest tab to build and a good early win. |
| Sequencing | **Prove the chart on day 1** | Spike the riskiest assumption first. If zoom-survives-append fails, that costs a day, not the week. |
| Frontend stack | **Vite + React + TypeScript** | The brief calls for `number \| null` typing, which only means anything in TS. Node v24 / npm 11 confirmed present on the dev machine. |
| Excel/CSV export | **Stay in Python** | `export.py` is 588 lines including formula-injection defence and a BOM for Excel's sake. Serve the bytes; never rebuild spreadsheets in JS. |

---

## 3. What `REACT_MIGRATION_PROMPT.md` does NOT know

The brief was written before a large refactor on 22–23 August. It is still correct about
architecture and about what must not regress, but several specifics it names no longer
exist. **Trust this section over that file where they disagree.**

### 3.1 Thresholds moved to `limits.py` and changed shape

The brief mentions `_over_cond` and temperature thresholds in `constants.py`. Both are gone.

`limits.py` at the repo root is now the single source for every threshold that drives a
colour, for nine metrics rather than three temperatures:

```python
Threshold = namedtuple("Threshold", "warn crit low_side full_scale")
def classify(value, threshold) -> "normal" | "warning" | "critical"
TIER_COLOURS = {NORMAL: None, WARNING: "#ff6500", CRITICAL: "#ff2020"}
```

- `low_side=True` inverts the comparison, for SoC and pack voltage where LOW is dangerous.
- `crit=None` means amber-only, never red — used by `MOTOR_CURRENT` on purpose.
- `full_scale` is the gauge arc saturation, validated at import so a threshold can never sit
  above its own scale again.
- `_validate()` raises at import on a transposed or unrenderable threshold.

`_over_cond` was deleted. So was the duplicate `render_metric` / `render_sector_display` in
`pit_dashboard.py` — `Pit_Dashboard/ui.py` is now the only definition of both.

**For the API:** `GET /api/config` must serve these from `limits.py`. Do not retype a
threshold or a tier colour in TypeScript.

### 3.2 Tier vocabulary changed

`SECTION_RISK` in `constants.py` now uses `"warning"` / `"critical"`, not `"warn"` / `"crit"`.
The CSS classes followed: `.sector-seg.current-warning` and `.current-critical`. The short
spelling was a silent-failure trap — a mismatched string fell through to "normal" and simply
lost the colour with no error.

### 3.3 The pit CSS takes injected tier colours

`apply_theme()` now injects `--pit-warning` and `--pit-critical` into `:root` from
`limits.TIER_COLOURS`, and the light theme overrides them with darker variants
(`#b35400` / `#c62828`) because `#ff6500` on white is about 2.9:1 contrast. **The tier and
its threshold are shared; only the light-mode rendering differs.** Carry that distinction —
do not "simplify" it to one hex.

### 3.4 There is a sixth tab: Live Metrics

25 metrics grouped by subsystem (Motion, Motor, Controller, Battery, Energy, Lap &
Distance), declared as data in `LIVE_METRIC_GROUPS` with an `_assert_no_duplicates()` guard
that raises at import. Two of the tiles show *known-broken* fields on purpose, clearly
labelled — the controller's SoC estimate (always 0) and the BMS pack voltage. Keep the
labels; a diagnostics view that hides data is less useful.

### 3.5 Exports are in local time, not UTC

The brief says nothing about this because it did not exist. `Pit_Dashboard/pit_config.py`
now holds:

```python
EXPORT_TZ_BEFORE = "Asia/Jerusalem"
EXPORT_TZ_AFTER  = "Europe/Brussels"
EXPORT_TZ_SWITCH_LOCAL = "2026-09-14T00:00:00"    # read in EXPORT_TZ_BEFORE
```

Each row is rendered in the timezone the team was in when it was recorded. The Excel Time
column is a real `datetime` cell with a number format (not a string), plus a **Zone** column
beside it, because openpyxl refuses timezone-aware datetimes and a workbook spanning two
zones is unreadable without it. `tzdata` is pinned in `requirements_pit.txt` — Windows ships
no system tz database.

If the web UI ever formats a timestamp itself, it must use the same rule. Prefer serving
pre-formatted strings from `pit_config.export_local()`.

### 3.6 The GPS map is satellite imagery

`_car_map_deck` no longer uses the Carto basemap. It points `map_style` at
`app/static/esri_satellite.json` — a MapLibre raster style served by Streamlit itself
(`server.enableStaticServing = true`). Two dead ends are recorded in the code comments and
worth not repeating: a deck.gl `TileLayer` renders nothing without a JS `renderSubLayers`
callback, and passing the style inline as a dict throws `mapStyle?.indexOf is not a function`.

In React you are not using pydeck, so this gets simpler — use MapLibre GL JS directly with
the same Esri World Imagery tile URL. No API key is involved. Keep the opaque
`#0c1624` background layer under the imagery so an offline pit LAN gets a dark panel with a
live dot rather than a void.

### 3.7 The History chart elides dead time

`_hist_rangebreaks()` finds spans with no samples and hands them to Plotly's
`xaxis.rangebreaks`. On the full history that takes the axis from 59 days to 13 hours of
actual recorded time — 0.94% of the original — so each session expands ~100× instead of
compressing to a 1px sliver. **Port this.** Plotly.js takes `rangebreaks` in the same shape,
so it is nearly a copy. Without it a wide window is unreadable.

### 3.8 Physics constants changed, and one is still open

`drivetrain.py`: `GEAR_RATIO` is `112/22 = 5.0909` (the spec sheet's value; a brief 144/22
was the *belt* tooth count, not the pulley). New: `RPM_REPORT_SCALE = 0.5` because the
controller reports exactly half the true motor RPM, and `CONTROLLER_SPEED_DIVISOR = 6.5455`,
which is **not** the gear ratio and must never be conflated with it again.

`Pit_Dashboard/db.py::_vehicle_speed` is now era-aware across three regimes, because the
controller was reconfigured mid-history. Do not simplify it.

⚠️ **Open item:** whether the ×2 RPM correction is right, or whether the gearing is. The
telemetry pins the *product* but cannot separate them; it needs a physical measurement
(rotate the wheel one turn, count motor turns). `TIRE_DIAMETER_METERS` is also still an
unmeasured placeholder. Do not build around either — expose whatever Python computes.

### 3.9 New tools worth knowing

| Tool | What it does |
|---|---|
| `tools/check_limits.py` | 184 headless assertions on gauge tiers, blink edges, no-data handling |
| `tools/replay_limits.py` | Replays the store to measure how often each threshold would fire |
| `tools/fix_rpm_history.py` | Backfills historical RPM/speed; idempotent, backs up first |
| `tools/hud_sim.py` | Drives the driver HUD with no car |

`check_limits.py` and `replay_limits.py` should keep passing after the migration — they test
`limits.py`, which the API now serves.

---

## 4. Step 0 — extract the metric catalogue (do this first, in the clone)

`HISTORY_CHARTS` currently lives inside `pit_dashboard.py`. The backend **cannot** import
that module to get at it: doing so pulls in Streamlit and executes page-level code inside a
FastAPI worker.

It also must not be retyped. `export.py::_XLSX_COLS` already reuses the same hexes so the
screen and the exported workbook agree on a metric's colour, and the brief is explicit that
the frontend must not retype them either. Three consumers, one list.

Create `Pit_Dashboard/metrics.py` holding the `HISTORY_CHARTS` list verbatim, with a
docstring explaining why it is its own module. Then in `pit_dashboard.py` replace the block
with:

```python
from metrics import HISTORY_CHARTS      # noqa: E402  (path set up above)
```

Keep the name identical so every existing use is unchanged. This was tried in the original
folder and verified working — 13 metrics, both consumers fine — then reverted, because the
original folder must stay untouched. Redo it here.

Verify:

```bash
cd Pit_Dashboard
python -c "import metrics; print(len(metrics.HISTORY_CHARTS))"        # 13, no streamlit
python -c "import pit_dashboard as p; print(len(p.HISTORY_CHARTS))"   # 13, still works
```

---

## 5. The week

Each day is a fresh Claude Code session. Do not carry one day's context into the next — see
section 8.

### Day 1 — prove the premise

Thin vertical slice, no styling, no other tabs. The only question being answered is *does
zoom survive a live append*.

- `pip install fastapi pydantic` (uvicorn and websockets are already present)
- `Pit_Web/api.py`: `GET /api/history` (one metric, one window) and `WS /ws/live`
- Minimal Vite + React + TS app: one Plotly chart, subscribe to the WS, `extendTraces`
- **Test:** zoom into the chart, wait for appends, confirm the zoom holds and new points
  arrive. Then pan, then change metric.

If this fails, stop and report before building anything else.

### Day 2 — backend proper

Full read API over `db.py`'s existing helpers (`fetch_samples`, `latest_sample`,
`fetch_lap_summary`, `fetch_faults`, `time_bounds`, `count_samples`, `count_samples_since`,
`recent_laps`, `fetch_lap_track`). Write no new SQL.

`GET /api/config` serving: `limits.py` thresholds and `TIER_COLOURS`, the `metrics.py`
palette, `constants.STRATEGIES`, `SECTION_NAMES` / `SECTION_TURN_LABELS` / `SECTION_RISK` /
`SECTION_COLORS`, `TRACK_LENGTH_METERS`, `DATA_STALE_AFTER_S`.

The write endpoints: `save_race_state` / `load_race_state`, driver message, cut-lap,
strategy selection, `clear_history`. `clear_history` is destructive and currently sits behind
a popover-plus-confirm so it cannot be hit mid-race — keep an equivalent guard.

### Day 3 — React shell

Routing, dark/light theme from the injected tier colours, font-size scaling, the top strip
(fault banner + 7 tiles + 3 lap tiles), the sidebar.

### Day 4 — History tab

The reason for the rewrite. Metric picker, window presets, raw-vs-normalized scale,
min/avg/max/now stats, `rangebreaks`, CSV export in both flavours, recent-samples table,
per-lap energy/time charts, fault episodes, reset-history.

Replace the freeze machinery with a simple pause toggle — the brief says explicitly not to
port the freeze as-is.

### Day 5 — Driver Telemetry

Track position card with velocity-profile target speed, sector strip with warn/crit tints
and the pulse animation, next-feature panel, sector splits, GPS map with satellite imagery.

`has_gps` is deliberately separate from lat/lon: the map falls back to the Zolder paddock so
it has somewhere to centre, and `has_gps` says whether the pin is real. `0,0` is a real place
in the Atlantic — never let a placeholder look like a fix.

### Day 6 — the rest

Live Metrics tab, Weather tab, Strategy tab (PNG endpoint), Excel/CSV export passthrough.

### Day 7 — deployment

FastAPI static-serves the production build, so the pit machine needs Python only — no Node,
no dev server. One `.bat` launcher matching `Start Pit Dashboard.bat`. Test offline, and from
a phone on the pit LAN.

Note from experience: multi-device access fails on campus/venue WiFi due to client isolation.
Use a phone hotspot or a dedicated router.

---

## 6. Must not regress — the checklist

Copied forward because these took real debugging. The brief has the full reasoning.

1. **`null` is never `0`.** Type these `number | null`, render `—`, never `value ?? 0`. A
   chart must break the line at `null` (`connectNulls: false`), not plot zero. CSV writes an
   empty cell.
2. **No physics in JavaScript.** Python owns `drivetrain.py`, `track.py`, `speed_profile.py`,
   `constants.py`, `limits.py`. Derive server-side, serve computed values.
3. **SQLite is the only source of truth.** `collector.py` is the sole Firebase client. The
   dashboard never opens a Firebase connection.
4. **Cadences.** 2s fast tier (tiles, fault banner, driver telemetry, sectors), 10s heavy
   (history, weather, strategy). Weather cached 1h, history ~8s, faults 30s. Push the fast
   tier over the WebSocket. For history, send an **incremental append**, never the whole
   series — that is the entire performance argument.
5. **Missing-vs-zero in the tiers.** `classify(None)` returns `"normal"`, deliberately: an
   absent reading is not evidence of a safe temperature *or* an alarming one.
6. **`_safe()` and the BOM.** `export.py` neutralises CSV/Excel formula injection and encodes
   `utf-8-sig` because Excel mangles `°C` and `Ω` without it.

---

## 7. What must not be touched

- **The original folder.** It is the race backup.
- **`drivetrain.py`, `track.py`, `limits.py`, `speed_profile.py`** at the repo root are
  shared with the car and must stay byte-identical to what the Pi runs. They live at the root
  because both subsystems locate them as *parent-of-my-own-folder* on `sys.path`; moving them
  breaks 14 import sites.
- **`main` branch.** Work on `react-migration` and do not merge until after the race.

---

## 8. Context and token budgeting

The build is estimated at 6–12 context windows. To stay inside that:

- **One phase per session.** Carrying day 3's history into day 4 buys nothing and costs the
  room the History tab needs.
- **Start each session by reading this file plus `REACT_MIGRATION_PROMPT.md`** — about 8k
  tokens combined, and it re-establishes everything. That is what these documents are for.
- **Never read `pit_dashboard.py` whole.** It is 2,514 lines / ~30k tokens, roughly 3% of a
  window for nothing. The fragment functions are cleanly separated; name the section.
- **Do not port `export.py` or `strategy_engine.py`.** Call them.
- Expect Day 4 to consume 2–3× a normal day. Front-end chart work is visually iterative and
  that is the expensive kind.

---

## 9. First message for the day-1 session

> Read `docs/REACT_MIGRATION_PLAN.md` then `docs/REACT_MIGRATION_PROMPT.md` in full. We are
> in the `THE-RACE-react` clone on branch `react-migration`; the original folder is the race
> backup and must not be touched. Do step 0 (extract `metrics.py`), then day 1 only: the
> vertical slice that proves Plotly zoom survives a live WebSocket append. Do not build any
> other tab, do not style anything. Report whether the premise holds before going further.
