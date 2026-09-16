# Pit_Web — React + FastAPI pit dashboard

**This is the pit dashboard.** It replaced the earlier Streamlit app
(`Pit_Dashboard/pit_dashboard.py`, now deleted), following the plan in
`docs/REACT_MIGRATION_PLAN.md`. It has the same tabs — Driver Telemetry, Live
Metrics, Cell Voltages, History, Weather and Strategy — plus the top strip, the
sidebar and the Excel/CSV exports.

It still reads the same store through the same shared modules in
`Pit_Dashboard/` (`db.py`, `strategy_engine.py`, `export.py`, ...), and
`collector.py` is still the only thing that talks to Firebase. The metric
catalogues are `Pit_Dashboard/metrics.py` (History charts) and
`Pit_Dashboard/live_metrics.py` (Live Metrics tiles).

Streamlit survives in the repo only for the speed-profile builder
(`Build Speed Profiles.bat`, port 8502), which is a separate app.

## Run it

Double-click `Start Pit Dashboard.bat` in the repo root; it forwards to
`Pit_Web\run_web.bat`. That finds a Python, installs
`requirements_web.txt` when the environment does not already match (stamped in
`Pit_Web\.deps_stamp`), starts `collector.py` in its own window, starts uvicorn
on `0.0.0.0:8000`, waits for the port and opens a browser. It prints the LAN URL
so a phone can be pointed at it (`http://<laptop-ip>:8000`).

`Demo Dashboard.bat` runs the same dashboard against a synthetic store
(`tools/demo_seed.py` + `tools/demo_feed.py`) on port 8010. It never touches
`telemetry.db` and does not start the collector.

By hand:

```bash
pip install -r Pit_Web/requirements_web.txt
python -m uvicorn Pit_Web.api:app --host 0.0.0.0 --port 8000
```

**Production needs Python only.** FastAPI static-serves `frontend/dist`, which
is committed to git, so the pit machine has no Node, no `npm install` and no dev
server.

## Changing the frontend

The pit laptop runs whatever `frontend/dist` is in git, not `frontend/src`. So
any change under `src` has to be rebuilt and committed together with it:

```bash
cd Pit_Web/frontend && npm ci && npm run build
```

Commit `dist` in the **same commit** as the `src` change. A `src` change without
its `dist` does nothing on the pit laptop.

For frontend work, `npm run dev` gives hot reload on 5173 and proxies `/api` and
`/ws` to uvicorn. Use **`localhost:5173`**, not `127.0.0.1:5173` — Vite binds
IPv6 loopback and the v4 address refuses the connection.

## Replay mode (development)

The dev snapshot is static, so `/ws/history` has nothing newer than the cursor
and the append path never runs. `SOLARRACE_REPLAY=1` holds the newest rows back
from the initial window and lets the socket walk forward through them on a wall
clock. **Real samples with their real gaps and real nulls — only the clock is
replayed.** Off by default, so pointing this at a live store does the obvious
thing.

| variable | default | meaning |
|---|---|---|
| `SOLARRACE_DB_PATH` | `Pit_Dashboard/telemetry.db` | which store to read |
| `SOLARRACE_REPLAY` | `0` | `1` replays held-back rows as if live |
| `SOLARRACE_REPLAY_HOLDBACK` | `600` | rows withheld from the initial window |
| `SOLARRACE_REPLAY_BATCH` | `5` | rows released per tick |
| `SOLARRACE_FAST_TICK` | `2.0` | seconds between live-tier pushes |
| `SOLARRACE_HISTORY_TICK` | `10.0` | seconds between history appends |

## How it is put together

**Cadences.** Two tiers, the split the earlier Streamlit app arrived at after
the page kept stalling. `WS /ws/live` pushes the whole fast tier every 2 s (tiles, fault
banner, sector card, map position). `WS /ws/history` sends an **incremental
append** every 10 s — never the whole series. That is the entire performance
argument for the rewrite.

**Zoom survives appends.** `newPlot` runs once per (metric set, window); every
sample after that goes in via `extendTraces`. The chart div is a ref, never
React state, so React cannot re-render it, and the History tab stays mounted
when you switch tabs. The earlier Streamlit app's freeze machinery became a
plain Pause toggle, which is all it was ever standing in for.

**History traces are SVG `scatter`, not `scattergl`.** WebGL traces silently
ignore `rangebreaks`; the axis then fails to autorange and the chart draws empty
on a default epoch range. Rangebreaks are what make a wide window readable — on
"All" they take the axis from 59 days to the ~13 hours actually recorded — so
they win. `/api/history` thins to ~4,000 points by even stride to keep SVG fast,
and reports the true count separately so nothing claims to show every sample.
`scattergl` remains the escape hatch if that stops being enough.

## The boundaries this keeps

1. **`null` is never `0`.** Typed `number | null` end to end, rendered as an em
   dash, `connectgaps: false` so a chart breaks the line at a dropout, empty
   cell in CSV. There is no `?? 0` in this app and there must never be one.
2. **No physics in JavaScript.** Speed, target speed, track position, strategy
   and every tier come from Python. `limits.classify()` is called server-side
   and the browser is handed a tier string — it is never given a threshold to
   compare against, so the pit and the driver HUD cannot disagree.
3. **No retyped palette.** `/api/config` serves `metrics.py`'s colours,
   `limits.py`'s thresholds and tier colours, and the section metadata. A hex
   typed in the frontend is a bug.
4. **No new SQL.** Every read goes through `db.py`'s existing helpers.
5. **Reads are `mode=ro`.** A backend pointed at the live store during a race
   cannot corrupt it. The write endpoints open their own connection explicitly.
   (This is why they do not use `db.get_conn()` — its `PRAGMA journal_mode=WAL`
   is a write and fails on a read-only handle.)
6. **Exports stay in Python.** `export.py` owns `_safe()` formula-injection
   defence and the `utf-8-sig` BOM Excel needs for `°C` and `Ω`.
7. **`clear_history` needs a typed phrase**, not a boolean — `{"confirm":
   "DELETE HISTORY"}`. It is irreversible and a stray POST from a phone in
   someone's pocket must not be able to wipe the store.

## Design system

`frontend/src/index.css` is the whole visual language: layered dark surfaces,
one accent, three ink levels, and status colours reserved for status. Inter and
JetBrains Mono are **bundled** via `@fontsource` — nothing is fetched from a font
CDN, because the track is offline. Light mode is a selected palette, not an
automatic flip.

Rules the components follow:

- **Text never wears a series colour.** A coloured mark beside the text — the
  swatch on a metric chip, the left rule on a stat card — carries identity. The
  number itself is always primary ink.
- **A tier is never colour alone.** A warning/critical value takes the tier
  colour *and* a labelled badge, so it reads on a washed-out screen or for a
  colourblind engineer.
- **Proportional figures for big numbers, tabular only in columns.** Tile values
  use Inter's default figures; tables and axis ticks use JetBrains Mono with
  `tabular-nums` so digits align.
- **One Plotly theme** (`plotly-theme.ts`): hairline gridlines, 2px lines, no
  zeroline, a dark unified tooltip, and a single series gets no legend box.
- **Sparklines on the top strip** show the last 10 minutes per tile. Nulls break
  the trace — a dropout reads as a gap, never as a dive to zero.
- **End-labels on the History chart** as a secondary identity channel. The
  validator flags Motor Temp `#ff5e5e` vs Controller Temp `#e74c3c` at ΔE 6.6 —
  hard to tell apart even with full colour vision, and both are °C so they share
  an axis. The hexes come from `metrics.py` and are shared with the workbook, so they stay;
  the label at each line's last point means identity never rests on that pair.

## Offline

Verified with every non-origin host blocked: the whole app works, and the only
thing it reaches for is `server.arcgisonline.com` for satellite tiles. Plotly,
MapLibre and the fonts are bundled. With tiles blocked the map still renders its
opaque `#0c1624` layer with a live dot, so an offline pit gets a dark panel
rather than a void.

Weather is the one panel that genuinely needs the internet, by nature. It
degrades to a stated "unavailable" rather than an empty chart.

## Known gotcha: multi-device access

Campus and venue WiFi often enable client isolation, which silently blocks other
devices from reaching the laptop. Use a phone hotspot or a dedicated router.
**Do not spend time debugging this as an app bug.**

## Collector supervision — decided

The `.bat` starts `collector.py` in its own window rather than having FastAPI
supervise it as a subprocess. Supervision couples the two lifetimes: restarting
the API to pick up a change would kill the collector mid-race, and an API crash
would orphan it. Separate windows let either be restarted alone, and match what
the earlier Streamlit launcher did. A dead collector cannot masquerade as a live
dashboard, because the sidebar shows the age of the newest sample and turns
amber past `DATA_STALE_AFTER_S`.

## Race-day behaviours worth knowing

- **Times on screen are the crew's local wall clock**, by the same rule the
  Excel export uses (`pit_config.export_local`: Asia/Jerusalem before the
  14 Sept switch, Europe/Brussels after). Served as naive local strings because
  Plotly draws whatever wall-clock string it is given. The History badge says
  which zone it is showing.
- **The race clock starts on the server's clock.** A phone whose clock is
  minutes out cannot skew it for everyone. The app bar ticks every second
  against the server↔client offset measured on each socket message.
- **Connection loss is loud.** If the socket goes quiet for 10 s the app bar
  goes red, a banner names the moment the numbers froze, and the browser tab
  title says FROZEN. The server's `fresh` flag cannot do this — it stops
  arriving too.
- **History zoom survives** adding or removing a metric, Normalize, and a theme
  change; only picking a different window resets it. Past 30,000 points the
  window is silently re-thinned with the zoom carried across, so an 8-hour
  stint on a 15-minute window cannot bog the SVG chart down. A catch-up burst
  from the collector is thinned server-side before it reaches the chart.
- **Driver-change countdown.** Regulations cap a stint at 2 hours, so the
  countdown sits in the app bar beside the race clock and the sidebar carries
  the reset. Amber at 15 min left, red at 5 min, and once it passes zero it
  counts UP with a full-width banner and the tab title saying OVERDUE. The
  limit and both thresholds are `DRIVER_STINT_*` in `Pit_Web/store.py`.

  State lives in SQLite beside the race clock, so every laptop, tablet and
  phone shows the same number and a refresh changes nothing. The server stamps
  the change time, so a device with a wrong clock cannot skew it. Starting the
  race auto-starts stint 1; restarting a race mid-session never resets a
  running countdown. The change button takes one click with no confirmation —
  it is pressed during a pit stop — and **Undo last change** appears for two
  minutes afterwards, restoring the previous stint exactly. The toast names
  the length of the stint just ended, so a mis-click reads as
  "previous stint ran 00:00:03" and is obvious immediately.

  **The countdown only runs while the race does.** It holds before the green
  flag and through any stoppage, and says so ("Holding · race stopped", dimmed,
  with a pause glyph). Otherwise it would drain through setup and show a false
  OVERDUE before the race had started — the cry-wolf failure `limits.py` exists
  to prevent. Time is banked on stop and resumed on start, so a red flag never
  eats into a driver's two hours, and the tab-title alert stays quiet while
  held. The due/overdue banner still shows when held, because a stoppage is a
  good moment to swap.

- **Reset race clock**, in the sidebar's Danger zone. "Stop race" deliberately
  keeps the start time so Resume works, which leaves no way back to "never
  started" — this is that way back, for a race started by accident. It clears
  the driver stint too, because starting a race auto-starts stint one and a
  phantom stint counting against a race that no longer exists is worse than
  none. The button says what it will clear before you press it ("this race has
  run 1 h 31 m and is RUNNING"), the toast says what it cleared, and **Undo
  race reset** restores both exactly for two minutes — a start time cannot be
  reconstructed by hand, so the mistake has to be takeable-back.

- **Every tab is in its own error boundary.** A render error in one tab leaves
  the strip, the sidebar and the other tabs working.
- **Reset history** lives in the sidebar's Danger zone behind a typed phrase.
- **The GPS map** draws the last completed lap's line in blue and a live
  breadcrumb trail in green.

## Still open

- **Two pre-existing palette clashes in `export.py::_XLSX_COLS`**, unrelated to
  this work and left alone because fixing either changes exported workbooks:
  `mms_measured_voltage_V` is `16A085` in the workbook but the screen plots that
  same controller voltage in `2ECC71` (which the workbook gives to the BMS
  voltage); and `58D68D` is shared by "Total Energy" and "Power Map (raw)".
- The value-filter control the earlier Streamlit History tab had was not
  carried over. The chart is directly zoomable now, which covers most of what
  it was for.
