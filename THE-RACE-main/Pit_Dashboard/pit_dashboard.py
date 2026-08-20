# pit_dashboard.py — Afeka Pit Wall (SQLite-backed)
#
# This dashboard reads ONLY from the local SQLite store (telemetry.db), which is
# filled by collector.py — the single process that talks to Firebase. The
# dashboard never opens its own Firebase connection. Start the collector first.
#
# Run from the REPO ROOT ("THE RACE"), same as SolarRace_OS — no need to cd in:
#
#     python Pit_Dashboard/collector.py          # ingests Firebase -> telemetry.db
#     streamlit run Pit_Dashboard/pit_dashboard.py
#
# (Both also work from inside Pit_Dashboard as `python collector.py` /
# `streamlit run pit_dashboard.py`. All data files — telemetry.db, the service
# key, the CSS and velocity profile — are located relative to this package, so
# the launch directory does not matter.)
#
# History, charts and exports all come from SQLite, so they survive page
# refreshes and restarts (unlike the old in-memory session_state history).

import os
import time
from datetime import datetime, timezone

import streamlit as st
import pandas as pd
import matplotlib.pyplot as plt
# The live GPS map is a hand-built deck rather than st.map, so that the view
# centre is ours to control (Follow / Focus). pydeck ships with Streamlit — it
# is what st.map itself renders through — so this adds no new install.
import pydeck as pdk

# The History chart needs Plotly: `uirevision` is what lets the browser keep the
# SAME chart instance across a rerun, so a zoom survives the live refresh. If a
# pit laptop is on stale requirements we fall back to the old native-chart grid
# rather than crashing the tab mid-race.
try:
    import plotly.graph_objects as go
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

import db
import export
from weather_service import fetch_zolder_weather
from strategy_engine import (
    calculate_all_strategies,
    create_combined_graph,
    load_velocity_profile,
    get_live_track_status,
    SECTIONS_INFO,          # sector boundaries — the same ones already displayed
)
from constants import (
    MOTOR_TEMP_WARN, MOTOR_TEMP_CRIT, CTRL_TEMP_WARN, CTRL_TEMP_CRIT,
    CELL_TEMP_WARN, CELL_TEMP_CRIT,
    STRATEGIES, STRATEGY_BY_LABEL, DEFAULT_STRATEGY_KEY,
    # speed_kmh() is deliberately NOT imported: road speed comes from the
    # controller's CAN field only (see build_state), and not importing the
    # RPM-derived formula here is what stops it quietly coming back as a
    # fallback.
    GEAR_RATIO, WHEEL_CIRCUMFERENCE_METERS, TRACK_LENGTH_METERS,
    TARGET_LAP_TIME_MIN, DATA_STALE_AFTER_S,
    SECTION_NAMES, SECTION_TURN_LABELS, SECTION_RISK, SECTION_COLORS,
    BMS_PROTECTION_BITS, MMS_ERROR_BITS, decode_error_bits,
)
from ui import render_metric, render_sector_display

_HERE = os.path.dirname(os.path.abspath(__file__))
VELOCITY_PROFILE_PATH = os.path.join(_HERE, "210s.xlsx")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSS_PATH = os.path.join(BASE_DIR, "assets", "pit_dashboard.css")

st.set_page_config(page_title="Afeka Pro Pit Wall",
                   page_icon=":material/sports_score:", layout="wide")

with open(CSS_PATH) as f:
    st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)

# Inline SVG glyphs for the raw-HTML blocks (fault banner + sector card). Streamlit
# widget labels use its native :material/…: icons, but raw HTML can't, so we embed
# sharp vector icons that inherit the surrounding text color via `currentColor`.
# Self-contained (no CDN) → they render on the offline pit LAN.
_SVG_ALERT = (
    '<svg viewBox="0 0 24 24" width="18" height="18" style="vertical-align:-3px;'
    'margin-right:7px" aria-hidden="true"><path fill="currentColor" d="M12 2 1 21h22'
    'L12 2Zm0 4.6 7.6 13.2H4.4L12 6.6ZM11 10v5h2v-5h-2Zm0 6v2h2v-2h-2Z"/></svg>'
)
_SVG_CHECK = (
    '<svg viewBox="0 0 24 24" width="16" height="16" style="vertical-align:-3px;'
    'margin-right:6px" aria-hidden="true"><path fill="currentColor" d="M9 16.2 4.8 12'
    'l-1.4 1.4L9 19 21 7l-1.4-1.4L9 16.2Z"/></svg>'
)
_SVG_CHEVRON = (
    '<svg viewBox="0 0 24 24" width="12" height="12" style="vertical-align:-1px;'
    'margin-right:5px" aria-hidden="true"><path fill="currentColor" d="M8 5v14l11-7z"/></svg>'
)
_SVG_BOLT = (
    '<svg viewBox="0 0 24 24" width="11" height="11" style="vertical-align:-1px;'
    'margin-right:3px" aria-hidden="true"><path fill="currentColor" d="M7 2v11h3v9l7-12h-4l4-8z"/></svg>'
)

# ---------------------------------------------------------------------------- #
# Dark / Light theme (sidebar toggle)
# The config.toml base is "dark", so DARK just sets our custom-tile CSS vars to
# their native values. LIGHT overrides those vars AND re-tints the Streamlit
# chrome (app/sidebar background + text) on top of the dark base. Native chart
# and dataframe widgets still follow the config base theme — that's a Streamlit
# limitation runtime CSS can't reach — so they stay dark in Light mode.
# ---------------------------------------------------------------------------- #
_THEME_DARK_CSS = """
<style>
:root {
    --pit-card-bg: #1a1a1a;
    --pit-card-border: #333;
    --pit-card-shadow: rgba(0, 0, 0, 0.5);
    --pit-title: #888;
    --pit-metric-accent: #00ffcc;
    --pit-ok-bg: #0d1117;
    --pit-ok-border: #1e3a2a;
    --pit-ok-text: #2ecc71;
}
</style>
"""

_THEME_LIGHT_CSS = """
<style>
:root {
    --pit-card-bg: #ffffff;
    --pit-card-border: #d7dce3;
    --pit-card-shadow: rgba(0, 0, 0, 0.08);
    --pit-title: #5a6672;
    --pit-metric-accent: #0a3d62;
    --pit-ok-bg: #eafaf1;
    --pit-ok-border: #b7e4c7;
    --pit-ok-text: #1a7f4b;
}
.stApp, [data-testid="stHeader"] { background-color: #f5f7fa !important; }
[data-testid="stSidebar"] { background-color: #eaeef3 !important; }
.stApp, .stApp p, .stApp label, .stApp li,
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5 { color: #1c2733 !important; }
[data-testid="stMetricValue"] { color: #0a3d62 !important; }
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] * { color: #5a6672 !important; }
[data-testid="stExpander"] summary { color: #1c2733 !important; }
[data-testid="stExpander"] details { background-color: #ffffff !important; border-color: #d7dce3 !important; }
/* Buttons follow the dark base theme by default — re-skin them for light mode so
   the label/icon isn't dark-on-dark (the START button + the toggle itself). */
.stButton button, .stDownloadButton button, .stFormSubmitButton button,
[data-testid="stPopover"] button {
    background-color: #ffffff !important;
    color: #1c2733 !important;
    border: 1px solid #cdd4dc !important;
}
.stButton button:hover, .stDownloadButton button:hover,
.stFormSubmitButton button:hover, [data-testid="stPopover"] button:hover {
    border-color: #0a3d62 !important;
    color: #0a3d62 !important;
}
[data-testid="stBaseButton-primary"] {
    background-color: #0a3d62 !important;
    color: #ffffff !important;
    border-color: #0a3d62 !important;
}
</style>
"""


def apply_theme(mode: str) -> None:
    """Inject the CSS for the selected appearance ('Dark' or 'Light')."""
    st.markdown(_THEME_LIGHT_CSS if mode == "Light" else _THEME_DARK_CSS,
                unsafe_allow_html=True)


def apply_font_scale(scale: float) -> None:
    """Scale the MAIN content only (sidebar stays fixed) so each user can size
    the numbers to taste. The dashboard's CSS uses fixed px, so a root
    font-size won't cascade — we use `zoom` on the main container instead."""
    st.markdown(
        f"<style>[data-testid='stMain'] {{ zoom: {scale}; }}</style>",
        unsafe_allow_html=True,
    )

# ============================================================================
# DATA ACCESS — local SQLite only
# ============================================================================
def _val(row, key, default=0.0):
    if row is None:
        return default
    try:
        v = row[key]
    except (IndexError, KeyError):
        return default  # column not present (e.g. pre-migration row object)
    return default if v is None else v


def read_live_state():
    """Latest sample from SQLite + freshness. Returns (state_dict, age_seconds).
    age is None when the store is empty."""
    conn = db.get_conn()
    try:
        row = db.latest_sample(conn)
    finally:
        conn.close()

    state = {
        # None, not 0, for EVERY metric the car might not have reported. The
        # motor PT1000 pair below already worked this way; the rest used to
        # default to 0 and so claimed a flat pack, a cold battery and a stopped
        # motor whenever the bus was quiet. Displays render None as MISSING_TEXT.
        "soc": None, "voltage": None, "current": None,
        "rpm": None, "temp": None, "power_w": None, "batt_temp": None,
        # Controller-side measurements. `pack_voltage` is the trustworthy pack
        # voltage (see METRIC_COLUMNS in db.py); `voltage` above is the BMS's
        # known-suspect one, kept for comparison rather than for decisions.
        "pack_voltage": None, "motor_current": None, "regen_energy": None,
        # car_speed_kmh removed: it was mms_vehicle_speed_kmh under a second
        # name, was never rendered, and is now simply speed_kmh above.
        "target_speed_kmh": None, "soc_ctrl": None,
        "trip_m": None,
        # Motor PT1000: None (not 0) when the car sent no reading, so the pit
        # shows "—" instead of a 0 °C that reads like a freezing motor.
        "motor_temp": None, "motor_ohms": None,
        # Active power map. None = the car never reported one.
        "motor_map": None, "motor_map_raw": None,
        # Per-lap analytics, all computed on the car. None = not reported yet
        # (no lap completed, or a build older than this feature).
        "last_lap_energy": None, "total_race_energy": None,
        "last_lap_time_s": None, "lap_distance_m": None, "lap_source": None,
        "lap_started_ts": None,
        "auto_lap": None, "odometer_km": None,
        # lat/lon fall back to the Zolder paddock purely so the map has somewhere
        # to centre before the car reports. has_gps says whether the position is
        # REAL: without it a placeholder pin is indistinguishable from a live
        # one, and "the map shows a dot" would look like working GPS.
        "lat": 50.9895, "lon": 5.2568, "has_gps": False, "speed_kmh": None,
        "bms_has_error": 0, "bms_error_code": 0, "bms_protections": "",
        "mms_has_error": 0, "mms_error_code": 0, "mms_alerts": "",
    }
    if row is None:
        return state, None

    state["soc"] = _val(row, "bms_soc_percent", None)
    state["voltage"] = _val(row, "bms_voltage_V", None)
    state["current"] = _val(row, "bms_current_A", None)
    state["pack_voltage"] = _val(row, "mms_measured_voltage_V", None)
    state["motor_current"] = _val(row, "mms_current_A", None)
    state["regen_energy"] = _val(row, "regen_energy", None)
    state["target_speed_kmh"] = _val(row, "target_speed_kmh", None)
    state["soc_ctrl"] = _val(row, "mms_estimated_soc_percent", None)
    state["trip_m"] = _val(row, "mms_trip_m", None)
    state["batt_temp"] = _val(row, "battery_temp_C", None)
    state["rpm"] = _val(row, "mms_rpm", None)
    state["temp"] = _val(row, "mms_temperature_C", None)
    state["power_w"] = _val(row, "mms_power_W", None)
    state["motor_temp"] = _val(row, "mms_motor_temp_C", None)
    state["motor_ohms"] = _val(row, "mms_motor_ohms", None)
    state["motor_map"] = _val(row, "mms_motor_map", None)
    state["motor_map_raw"] = _val(row, "mms_motor_map_raw", None)
    state["last_lap_energy"] = _val(row, "last_lap_energy", None)
    state["total_race_energy"] = _val(row, "total_race_energy", None)
    state["last_lap_time_s"] = _val(row, "last_lap_time_s", None)
    state["lap_distance_m"] = _val(row, "lap_distance_m", None)
    state["lap_source"] = _val(row, "lap_source", None)
    auto_lap = _val(row, "calculated_lap", None)
    state["auto_lap"] = None if auto_lap is None else int(auto_lap)
    odometer_m = _val(row, "odometer_m", None)
    state["odometer_km"] = None if odometer_m is None else odometer_m / 1000.0
    # NULL lat/lon = the car sent no fix (no GPS, or still searching). Keep the
    # Zolder fallback for the map centre, but remember it isn't a real position.
    state["has_gps"] = _val(row, "lat", None) is not None and \
                       _val(row, "lon", None) is not None
    state["lat"] = _val(row, "lat", 50.9895)
    state["lon"] = _val(row, "lon", 5.2568)
    # Road speed comes from ONE place: the controller's own speed field on
    # 0x610, decoded by mms_parser.decode_vehicle_speed_kmh() on the car and
    # stored as mms_vehicle_speed_kmh. The driver HUD reads the same field from
    # the same frame, so the two screens cannot drift apart.
    #
    # Deliberately NO fallback to speed_kmh(state["rpm"]). A fallback would let
    # the tile show a number derived from a different field without saying so,
    # which is exactly the class of bug that made the HUD read 3.3 % high for
    # months. Missing speed renders as an em dash — see fmt().
    #
    # ⚠️ Rows written BEFORE the decode fix hold the raw uncorrected value
    # (~50x too high). They are wrong here, not merely stale. Run the backfill
    # before trusting historical speed — see tools/backfill_columns.py.
    state["speed_kmh"] = _val(row, "mms_vehicle_speed_kmh", None)
    state["bms_has_error"] = _val(row, "bms_has_error", 0)
    state["bms_error_code"] = _val(row, "bms_error_code", 0)
    state["bms_protections"] = _val(row, "bms_protections", "")
    state["mms_has_error"] = _val(row, "mms_has_error", 0)
    state["mms_error_code"] = _val(row, "mms_error_code", 0)
    state["mms_alerts"] = _val(row, "mms_alerts", "")

    device_ts = row["device_ts"]
    age = (time.time() - device_ts) if device_ts else None
    return state, age


# Every history metric we can chart: (df column, label, unit, color). Column
# names match read_history_df() below; colors are distinct and legible on both
# the dark and light themes.
HISTORY_CHARTS = [
    ("Speed",     "Speed",           "km/h", "#00FFCC"),
    ("Power",     "Motor Power",     "W",    "#00B3FF"),
    ("RPM",       "Motor RPM",       "rpm",  "#9b59b6"),
    ("SoC",       "Battery SoC",     "%",    "#f1c40f"),
    # Sourced from the CONTROLLER's measurement, not the BMS's. Same name and
    # same place in the list as before, but it now reads ~50 V instead of ~113 V
    # for the same pack, because the controller's figure is the one that matches
    # the cell count. The BMS decode in bms_parser.py is a known open bug; its
    # value is still stored and still in the Excel export for diagnosis, it just
    # isn't the number the pit reads during a race any more.
    ("Voltage",   "Battery Voltage", "V",    "#2ecc71"),
    ("Current",   "Battery Current", "A",    "#e67e22"),
    ("BattTemp",  "Battery Temp",    "°C",   "#ff9900"),
    # "Motor Temp" now plots the motor's OWN PT1000 sensor. It used to plot the
    # controller temperature, which is still charted — under its real name.
    ("MotorTemp", "Motor Temp",      "°C",   "#ff5e5e"),
    ("CtrlTemp",  "Controller Temp", "°C",   "#e74c3c"),
    ("MotorOhms", "Motor Sensor",    "Ω",    "#c39bd3"),
    ("Distance",  "Distance",        "km",   "#1abc9c"),
    ("Lap",       "Lap",             "#",    "#7f8c9b"),
    # Accumulated motor energy over time — the consumption curve. This is the
    # running total the CAR integrates, not something re-derived here, so it is
    # correct across telemetry dropouts. It is NET of regen, so the trace can
    # legitimately dip on a long descent. Distinct from the per-lap energy bars
    # further down the History tab, which show one figure per completed lap.
    ("Energy",    "Total Race Energy", "Wh", "#58D68D"),
]
# The other fields the car sends (regen energy, target speed, the controller's own
# speed and SoC estimate, the trip counter) are stored in telemetry.db and land in
# the Excel export, but are deliberately kept OUT of this list: they are analysis
# data, and thirteen chips above the chart is already the most a pit wall wants to
# read at a glance.
HISTORY_LABELS = [label for _, label, _, _ in HISTORY_CHARTS]

# Time windows offered above the charts (label -> minutes; None = all history).
# Every one of these is offered as a one-click preset above the chart, so this
# dict stays the single source of truth for the window options. The long ones
# matter: this is a 24-hour race and "how are we doing over the whole night" is
# a normal question to ask the chart.
HISTORY_WINDOWS = {"1 min": 1, "5 min": 5, "15 min": 15, "1 hour": 60,
                   "3 hours": 180, "12 hours": 720, "24 hours": 1440, "All": None}

# What the History tab starts with. NOT all 13 — they now share one chart, and
# thirteen overlaid traces is a scribble nobody can read. Exactly two units
# (km/h + %) so the opening view fills both axes honestly and needs no warning;
# adding a third unit is what prompts the switch to Normalize.
HISTORY_DEFAULT_METRICS = ["Speed", "Battery SoC"]

# Cap points drawn per chart — keeps wide windows ("All" = a whole race) snappy.
MAX_PLOT_POINTS = 1500

# Lower cap for the single overlay chart: several traces share it, and total SVG
# point count is what makes hover go sluggish, not points-per-trace.
OVERLAY_MAX_POINTS = 900

# Refresh cadences for the History panels. The chart is the only thing that needs
# to be anywhere near live; the tables were the expensive part of the old
# everything-every-10s tick.
CHART_TICK_S = 10
STATUS_TICK_S = 5
TABLE_TICK_S = 30

# Shown wherever a reading was never received. The codebase already uses an em
# dash for this (unknown lap time, unknown target speed, unknown MAP), so a
# missing number looks the same everywhere instead of masquerading as a real 0.
MISSING_TEXT = "—"

# How long a history read is reused. Just under the 10s history refresh, so each
# tick serves from cache instead of re-reading up to 100k rows on the main
# thread — that read is what made long sessions stall and look disconnected.
HISTORY_CACHE_S = 8


def _downsample(df, n=MAX_PLOT_POINTS):
    """Thin a DataFrame to ~n rows by even stride, so charts stay responsive on
    long windows. Keeps the newest row so the live end of the trace is exact."""
    if len(df) <= n:
        return df
    step = (len(df) // n) + 1
    thinned = df.iloc[::step]
    if df.index[-1] not in thinned.index:
        thinned = pd.concat([thinned, df.iloc[[-1]]])
    return thinned


@st.cache_data(ttl=HISTORY_CACHE_S, show_spinner=False)
def read_history_df(limit=100000, start_ts=None):
    """Samples as a DataFrame for charting (oldest -> newest), one column per
    chartable metric. `start_ts` limits to samples at/after that unix time.

    Cached for 8s — just under the 10s history refresh. Without it, every tick
    re-read and re-boxed up to 100k rows on the main thread; on the "All" window
    late in a race that is long enough to stall the websocket and make the page
    look frozen. `start_ts` is part of the cache key, so changing the time
    window still refetches immediately.
    """
    conn = db.get_conn()
    try:
        rows = db.fetch_samples(conn, start_ts=start_ts, limit=limit)
    finally:
        conn.close()
    cols = ["Time"] + [c for c, *_ in HISTORY_CHARTS]
    if not rows:
        return pd.DataFrame(columns=cols)

    # A reading the car never sent stays None -> pandas NaN, NOT 0. The old
    # `or 0` coalescing turned every telemetry dropout into a confident lie: the
    # pack "reaching 0 V", the battery "at 0 °C", and — worst — those zeros being
    # averaged into stint statistics. NaN instead means charts draw an honest gap
    # and min/avg/max skip the gap rather than being dragged down by it.
    recs = []
    for r in rows:
        rpm = r["mms_rpm"]
        odo = r["odometer_m"]
        recs.append({
            "Time": datetime.fromtimestamp(r["device_ts"]) if r["device_ts"] else None,
            # Same single source as the live tile and the driver HUD: the
            # controller's own speed field, never re-derived from RPM.
            "Speed": r["mms_vehicle_speed_kmh"],
            "Power": r["mms_power_W"],
            "RPM": rpm,
            "SoC": r["bms_soc_percent"],
            # The controller's measurement — see the HISTORY_CHARTS note. NO
            # fallback to bms_voltage_V: the two disagree by ~2.25x, so filling
            # gaps from the other source would draw a trace that steps between
            # 50 V and 113 V and looks like a real electrical event. A gap is
            # honest. Rows predating this column read empty until
            # tools/backfill_columns.py recovers them from raw_json.
            "Voltage": r["mms_measured_voltage_V"],
            "Current": r["bms_current_A"],
            "BattTemp": r["battery_temp_C"],
            "MotorTemp": r["mms_motor_temp_C"],
            "CtrlTemp": r["mms_temperature_C"],
            "MotorOhms": r["mms_motor_ohms"],
            "Distance": odo / 1000.0 if odo is not None else None,
            "Lap": r["calculated_lap"],
            "Energy": r["total_race_energy"],
        })
    return pd.DataFrame(recs)


def _bms_fault_detail(protections, code):
    """Human-readable BMS fault text: the car's label string if present, else
    the pit-side decode of the raw code, else the bare hex for unknown codes."""
    return (protections or decode_error_bits(code, BMS_PROTECTION_BITS)
            or f"code 0x{int(code or 0):X}")


def _mms_fault_detail(alerts, code):
    """Human-readable MMS fault text — same precedence as the BMS helper."""
    return (alerts or decode_error_bits(code, MMS_ERROR_BITS)
            or f"error 0x{int(code or 0):X}")


@st.cache_data(ttl=30, show_spinner=False)
def read_fault_episodes(limit_rows=3000, gap_s=3.0):
    """Collapse the per-sample fault rows into discrete episodes.

    Cached for 30s: this reads 3000 rows and collapses them in Python, which was
    the most expensive thing on the History refresh path. Fault history barely
    changes between ticks, so re-deriving it every time bought nothing.

    Consecutive fault rows with the same signature (and no time gap bigger than
    `gap_s`) become one episode with a start time and duration — so a fault that
    persists for 200 samples shows as a single readable row, not 200."""
    conn = db.get_conn()
    try:
        rows = db.fetch_faults(conn, limit=limit_rows)
    finally:
        conn.close()

    episodes = []
    for r in rows:
        ts = r["device_ts"]
        if ts is None:
            continue
        parts = []
        if r["bms_has_error"]:
            parts.append("BMS: " + _bms_fault_detail(r["bms_protections"],
                                                     r["bms_error_code"]))
        if r["mms_has_error"]:
            parts.append("MMS: " + _mms_fault_detail(r["mms_alerts"],
                                                     r["mms_error_code"]))
        sig = "  |  ".join(parts)
        if not sig:
            continue
        if episodes and episodes[-1]["sig"] == sig and ts - episodes[-1]["end"] <= gap_s:
            episodes[-1]["end"] = ts
        else:
            episodes.append({"start": ts, "end": ts, "sig": sig})
    return episodes


# ============================================================================
# UI COMPONENTS
# ============================================================================
def format_lap_time(seconds):
    """Seconds -> M:SS.mmm, the way a lap time is read on a timing screen.

    Returns "—" for None so a lap that has not completed yet is visibly absent
    rather than showing as 0:00.000, which would look like a real (impossible)
    lap time.
    """
    if seconds is None:
        return "—"
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "—"
    if seconds < 0:
        return "—"
    minutes, rem = divmod(seconds, 60.0)
    return f"{int(minutes)}:{rem:06.3f}"


def fmt(value, spec=".0f", missing=None):
    """Format a possibly-unknown reading.

    `None` means the car never reported it, and that is shown as an em dash
    rather than a number. Every numeric readout on the dashboard goes through
    here so a missing value can never reach the screen disguised as a real one —
    a "0" battery temperature or "0 V" pack is a lie the pit wall acts on."""
    if value is None:
        return missing if missing is not None else MISSING_TEXT
    try:
        return format(value, spec)
    except (TypeError, ValueError):
        return str(value)


def _age_text(seconds):
    """Data age in units a human reads at a glance.

    "Stale · 77585s ago" is a number you have to do arithmetic on before you know
    whether to worry; "21h 33m ago" is not."""
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _over_cond(value, warn, crit):
    """Tile severity for a reading where HIGH is bad (temperatures).

    An unknown reading is styled "normal", not safe-looking-green and not
    alarming red: we have no evidence either way, and the value itself already
    shows as a dash so nobody mistakes it for a measurement."""
    if value is None:
        return "normal"
    if value > crit:
        return "critical"
    if value > warn:
        return "warning"
    return "normal"


def render_metric(col, title, val, unit, condition="normal"):
    color_class = ""
    if condition == "warning": color_class = "warning"
    if condition == "critical": color_class = "critical"
    col.markdown(f"""
    <div class="metric-container">
        <div class="metric-title">{title}</div>
        <div class="metric-value {color_class}">{val} <span style="font-size:16px;">{unit}</span></div>
    </div>
    """, unsafe_allow_html=True)


def render_sector_display(track_status, current_dist_m, sector_id):
    """Sector card with the velocity-profile TARGET SPEED for this point."""
    risk = SECTION_RISK.get(sector_id, "normal")
    color = SECTION_COLORS[risk]
    current_class = "current" if risk == "normal" else f"current-{risk}"
    sector_name = SECTION_NAMES.get(sector_id, f"Section {sector_id}")
    target_speed = track_status.get("target_speed", 0)
    next_name = track_status.get("next_feature", "N/A")
    next_dist = track_status.get("distance_to_next", 0)
    next_desc = track_status.get("next_feature_desc", "")
    next_speed = track_status.get("next_feature_speed", 0)

    segs_html = ""
    for i in range(1, 10):
        if i < sector_id:
            cls = "past"
        elif i == sector_id:
            cls = current_class
        else:
            cls = "future"
        turn = SECTION_TURN_LABELS.get(i, "") if i == sector_id else ""
        segs_html += (
            f'<div class="sector-seg {cls}"><div>S{i}</div>'
            f'<div style="font-size:13px;margin-top:2px;opacity:0.9">{turn}</div></div>'
        )

    return f"""
    <div class="sector-card">
        <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;">
            <div>
                <div class="sector-sub">CURRENT SECTOR</div>
                <div class="sector-headline" style="color:{color}">SECTOR {sector_id}</div>
                <div style="color:#8899aa;font-size:13px;margin-top:5px;">{sector_name}</div>
            </div>
            <div style="text-align:right;">
                <div class="sector-sub">TARGET SPEED</div>
                <div style="font-size:30px;font-weight:800;color:{color}">{target_speed:.0f}<span style="font-size:13px;color:#556;font-weight:400;"> km/h</span></div>
                <div style="color:#445566;font-size:11px;margin-top:3px;">{current_dist_m:.0f} m / 4000 m</div>
            </div>
        </div>
        <div class="sector-strip">{segs_html}</div>
        <div class="next-feature-panel">
            <div class="sector-sub">NEXT FEATURE</div>
            <div style="display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-top:5px;">
                <span class="next-feature-name">{_SVG_CHEVRON}{next_name}</span>
                <span class="dist-badge">in&nbsp;<b>{next_dist:.0f}m</b></span>
                <span class="speed-badge">{_SVG_BOLT}{next_speed} km/h</span>
            </div>
            <div class="next-feature-desc">"{next_desc}"</div>
        </div>
    </div>
    """


# ============================================================================
# FRAGMENTS
# Two cadences: live numbers refresh fast (2s); the heavy history / weather /
# strategy panels refresh slowly (10s) so the page stays snappy.
#
# Each fragment renders DIRECTLY into its tab/sidebar spot (not into an external
# st.empty().container()). A fragment rerun then updates its own elements in
# place, instead of blanking the area and redrawing it — which is what caused the
# dark flash every couple of seconds.
# ============================================================================
def _elapsed_and_left():
    time_left_min = 1440.0
    elapsed_minutes = 0
    if st.session_state.is_racing and st.session_state.race_start_time:
        elapsed_minutes = (time.time() - st.session_state.race_start_time) / 60.0
        time_left_min = max(0.0, 1440.0 - elapsed_minutes)
    return elapsed_minutes, time_left_min


def _live_context():
    """Shared live snapshot for the fast (2s) fragments — one SQLite read + track
    calc, reused by the sidebar status, top strip, and Driver tab. Overrides come
    from session_state so the fragments need no args; the velocity profile is
    @st.cache_data, so recomputing per fragment is cheap."""
    manual_lap_override = int(st.session_state.get("manual_lap", -1))
    comp_laps = int(st.session_state.get("rival_laps", 0))
    elapsed_minutes, time_left_min = _elapsed_and_left()

    state, age = read_live_state()
    fresh = age is not None and age <= DATA_STALE_AFTER_S

    active_lap = manual_lap_override if manual_lap_override >= 0 else state["auto_lap"]
    expected_laps_now = elapsed_minutes / TARGET_LAP_TIME_MIN if TARGET_LAP_TIME_MIN > 0 else 0

    odometer_km = state["odometer_km"]
    # Prefer the car's own "metres since the last lap trigger". Once laps are
    # cut at the GPS finish line, odometer % 4000 no longer lines up with the
    # real lap boundary and the sector display drifts a little further out of
    # step every lap. Fall back to the modulo for older rows that lack it.
    if state["lap_distance_m"] is not None:
        current_lap_dist_m = float(state["lap_distance_m"]) % TRACK_LENGTH_METERS
    elif odometer_km is not None:
        current_lap_dist_m = (odometer_km * 1000.0) % TRACK_LENGTH_METERS
    else:
        # Nothing to place the car with. Track POSITION still needs a number to
        # render a strip, and 0 m is where it always sat before the car reported.
        # The distance READOUTS stay None so they show a dash — position being
        # unknown-but-drawn is fine, a fabricated odometer figure is not.
        current_lap_dist_m = 0.0
    track_status = get_live_track_status(current_lap_dist_m,
                                         load_velocity_profile(VELOCITY_PROFILE_PATH))
    try:
        sec_raw = track_status.get("section", "Section 1")
        current_sector_id = int(sec_raw.split(" ")[-1]) if "Section" in sec_raw else 1
    except (ValueError, AttributeError):
        current_sector_id = 1

    return {
        "state": state, "age": age, "fresh": fresh,
        "hours_left": int(time_left_min // 60),
        "mins_left": int(time_left_min % 60),
        "secs_left": int((time_left_min * 60) % 60),
        "active_lap": active_lap,
        # None when the car has not reported a lap count and nobody has set one
        # manually. Comparing an unknown lap tally against a target produces a
        # deficit that looks like the car is losing the race.
        "lap_delta": None if active_lap is None else active_lap - expected_laps_now,
        "gap_to_competitor": None if active_lap is None else active_lap - comp_laps,
        "odometer_km": odometer_km,
        "current_lap_dist_m": current_lap_dist_m,
        "track_status": track_status,
        "current_sector_id": current_sector_id,
        "sb_sector_name": SECTION_NAMES.get(current_sector_id, f"Section {current_sector_id}"),
    }


@st.fragment(run_every=2)
def _sidebar_status_fragment():
    """Live race status block in the sidebar — updates in place, no flash."""
    c = _live_context()
    age = c["age"]
    if c["fresh"]:
        st.success(f"LIVE · {age:.0f}s ago", icon=":material/sensors:")
    elif age is None:
        st.error("No data — is collector.py running?", icon=":material/cloud_off:")
    else:
        st.warning(f"Stale · {_age_text(age)} ago", icon=":material/warning:")

    st.metric(":material/timer: Time Remaining",
              f"{c['hours_left']:02d}:{c['mins_left']:02d}:{c['secs_left']:02d}")
    r1a, r1b = st.columns(2)
    r1a.metric(":material/loop: Active Lap", fmt(c["active_lap"], "d"))
    r1b.metric(":material/timelapse: Lap Delta", fmt(c["lap_delta"], "+.1f"))
    r2a, r2b = st.columns(2)
    r2a.metric(":material/compare_arrows: Gap (laps)",
               fmt(c["gap_to_competitor"], "+d"))
    r2b.metric(":material/route: Distance",
               MISSING_TEXT if c["odometer_km"] is None
               else f"{c['odometer_km']:.1f} km")
    st.metric(":material/pin_drop: Sector", f"S{c['current_sector_id']} · {c['sb_sector_name']}")


@st.fragment(run_every=2)
def _top_strip_fragment():
    """Fault banner + live metric tiles, above the tabs — updates in place."""
    c = _live_context()
    state, fresh = c["state"], c["fresh"]

    faults = []
    if state["bms_has_error"]:
        faults.append("BMS: " + _bms_fault_detail(state["bms_protections"], state["bms_error_code"]))
    if state["mms_has_error"]:
        faults.append("MMS: " + _mms_fault_detail(state["mms_alerts"], state["mms_error_code"]))

    if faults:
        cls = "fault-banner" if fresh else "fault-stale"
        suffix = "" if fresh else " · STALE"
        st.markdown(f'<div class="{cls}">{_SVG_ALERT}ACTIVE FAULTS{suffix} — '
                    + "&nbsp;&nbsp;|&nbsp;&nbsp;".join(faults) + "</div>",
                    unsafe_allow_html=True)
    elif fresh:
        st.markdown(f'<div class="fault-ok">{_SVG_CHECK}No active faults</div>',
                    unsafe_allow_html=True)

    # ── Active power map ──────────────────────────────────────────────────── #
    # A badge of its own rather than an eighth tile: strategy needs to see at a
    # glance which energy configuration the car is on, and it is a state, not a
    # measurement. Amber for the reverse map since that should never be live on
    # track; grey when the car has not reported one.
    motor_map = state["motor_map"]
    if motor_map:
        raw = state["motor_map_raw"]
        raw_txt = f" (raw {int(raw)})" if raw is not None else ""
        colour = "#ff9900" if "Reverse" in motor_map else "#00FFCC"
        st.markdown(
            f'<div style="border-left:4px solid {colour};padding:4px 10px;'
            f'margin-bottom:6px;background:rgba(255,255,255,0.04);">'
            f'<span style="color:#8b98a5;font-size:12px;letter-spacing:1px;">'
            f'POWER MAP</span>&nbsp;&nbsp;'
            f'<span style="color:{colour};font-size:18px;font-weight:bold;">'
            f'{motor_map}</span>'
            f'<span style="color:#8b98a5;font-size:12px;">{raw_txt}</span></div>',
            unsafe_allow_html=True)
    else:
        st.markdown(
            '<div style="border-left:4px solid #4a5568;padding:4px 10px;'
            'margin-bottom:6px;background:rgba(255,255,255,0.04);">'
            '<span style="color:#8b98a5;font-size:12px;letter-spacing:1px;">'
            'POWER MAP</span>&nbsp;&nbsp;'
            '<span style="color:#8b98a5;font-size:16px;">not reported</span></div>',
            unsafe_allow_html=True)

    soc, temp, batt_temp = state["soc"], state["temp"], state["batt_temp"]
    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    render_metric(c1, "Speed", fmt(state["speed_kmh"], ".1f"), "KM/H")

    # Motor PT1000 — the converted °C is the headline, with the raw Ω beside it
    # so the pit can see the measurement the temperature came from (and tell a
    # genuinely hot motor from a failing sensor).
    motor_temp, motor_ohms = state["motor_temp"], state["motor_ohms"]
    ohms_txt = f" · {motor_ohms:.1f} Ω" if motor_ohms is not None else ""
    if motor_temp is None:
        # No conversion: show the raw Ω if we have one, so a stuck/out-of-range
        # sensor is visible rather than silently absent.
        render_metric(c2, "Motor Temp", "—",
                      ohms_txt.lstrip(" ·") or "no sensor data")
    else:
        motor_cond = "normal"
        if motor_temp > MOTOR_TEMP_WARN: motor_cond = "warning"
        if motor_temp > MOTOR_TEMP_CRIT: motor_cond = "critical"
        render_metric(c2, "Motor Temp", f"{motor_temp:.1f}",
                      f"°C{ohms_txt}", motor_cond)

    # Byte 4 of the same frame — the controller's own temperature. Previously
    # mislabelled "Motor Temp" here, back when the motor had no sensor of its own.
    render_metric(c3, "Controller Temp", fmt(temp), "°C",
                  _over_cond(temp, CTRL_TEMP_WARN, CTRL_TEMP_CRIT))
    # SoC is inverted — LOW is the dangerous end. Unknown is styled neutral
    # either way: an absent reading is not evidence of a healthy pack, and it is
    # not evidence of a flat one either.
    soc_cond = "critical" if soc is not None and soc < 20 else "normal"
    render_metric(c4, "Battery SoC", fmt(soc), "%", soc_cond)
    render_metric(c5, "Battery Temp", fmt(batt_temp), "°C",
                  _over_cond(batt_temp, CELL_TEMP_WARN, CELL_TEMP_CRIT))
    render_metric(c6, "Power Out", fmt(state["power_w"]), "W")
    render_metric(c7, "Lap Dist", fmt(c["current_lap_dist_m"]), "m")

    # ── Second row: per-lap analytics ─────────────────────────────────────── #
    # A second row rather than widening the first: seven tiles are already
    # narrow, and these three are read together as a set.
    l1, l2, l3 = st.columns(3)

    # Last lap time freezes at the trigger by construction — the car only
    # recomputes it when a lap is cut, so it is a settled figure for the whole
    # of the next lap. The stopwatch beside it is the one that keeps running.
    lap_time = state["last_lap_time_s"]
    src = state["lap_source"]
    # "odometer" means the GPS trigger missed and the distance backstop fired.
    src_note = "" if src in (None, "gps", "manual") else f" · {src}"
    render_metric(l1, "Last Lap Time", format_lap_time(lap_time),
                  f"m:ss{src_note}")

    lap_wh = state["last_lap_energy"]
    render_metric(l2, "Last Lap Energy",
                  "—" if lap_wh is None else f"{lap_wh:.1f}", "Wh")

    total_wh = state["total_race_energy"]
    # One decimal, matching Last Lap Energy and the Excel export — with a whole
    # number you cannot see the total move during a slow lap, or dip at all
    # under regen. Net of regen and motor-side only (excludes controller losses
    # and auxiliaries), so it reads lower than what actually left the pack.
    render_metric(l3, "Total Race Energy",
                  "—" if total_wh is None else f"{total_wh:.1f}", "Wh net")


# ── Live GPS map ──────────────────────────────────────────────────────────── #
# Zoom 14 is about the whole Zolder circuit in frame — the level the map opened
# at when it was an st.map call, kept so the first render looks the same.
MAP_ZOOM = 14
# The dot's RADIUS IN METRES. st.map defaults it to 100 — a 200 m blob that at
# zoom 14 (~6 m/px at Zolder's latitude) covers a visible fraction of the lap
# and hides the very corner you are looking at. 12 m is car-and-a-bit scale, so
# the dot reads as a position rather than an area, and radiusMinPixels floors it
# at a crisp ~6 px dot however far you zoom out.
CAR_DOT_RADIUS_M = 12
CAR_DOT_COLOR = [0, 255, 204]
# ~11 cm of latitude — invisible on the map, but enough to make a view state
# differ from the one already on screen. See the Focus handling below.
FOCUS_NUDGE_DEG = 1e-6


def _car_map_deck(lat, lon, center_lat, center_lon):
    """One dot at the car, view centred wherever the caller says."""
    return pdk.Deck(
        # None, not pydeck's own "dark" default: it leaves the basemap to the
        # frontend, which picks the Carto style matching the dashboard's
        # light/dark theme. That is exactly what st.map did.
        map_style=None,
        initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lon,
                                         zoom=MAP_ZOOM, pitch=0, bearing=0),
        layers=[pdk.Layer(
            "ScatterplotLayer",
            # A fixed id. pydeck otherwise mints a fresh uuid on every call, and
            # deck.gl would tear the layer down and rebuild it twice a second
            # instead of moving the dot it already has.
            id="car-position",
            data=[{"lat": lat, "lon": lon}],
            get_position="[lon, lat]",
            get_fill_color=CAR_DOT_COLOR,
            get_radius=CAR_DOT_RADIUS_M,
            radius_min_pixels=3,
            # Quoted so pydeck passes the string through as-is: a bare string
            # kwarg is wrapped as an "@@=" data accessor, which deck.gl would
            # then try to evaluate against each row.
            radius_units="'meters'",
        )],
    )


def render_gps_map(state):
    """Live GPS map with Google-Maps-style Follow, Focus and open-in-Maps.

    Both modes fall out of one fact about the frontend: it diffs the deck's
    initialViewState against the last one it received and pushes only the keys
    that actually moved. Feed it the car's position every tick and the map
    follows the car; hold the centre still and the pan/zoom the engineer set in
    the browser is never touched, while the dot keeps moving underneath.
    """
    lat, lon = state["lat"], state["lon"]
    fix = state["has_gps"]

    b1, b2, b3 = st.columns([1.1, 1, 1.3], vertical_alignment="center")
    # Deliberately never disabled, not even while following: with the car parked
    # in the pit its position stops changing, so following stops re-centring,
    # and this is then the only way back from a stray pan.
    focus = b1.button(":material/my_location: Focus car", width="stretch",
                      key="map_focus", help="Centre the map on the car.")
    follow = b2.toggle("Follow", value=True, key="map_follow",
                       help="Keep the map centred on the car. Switch off to pan "
                            "and zoom freely — the dot still moves live.")
    # Google's documented Maps URL: opens the web map on the pit laptop and
    # deep-links into the Maps app on a phone. Disabled without a fix, because
    # the fallback centre is the Zolder paddock — sending a marshal to a pin
    # that is not the car is worse than giving them no link at all.
    b3.link_button(
        ":material/open_in_new: Google Maps",
        f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}",
        width="stretch", disabled=not fix,
        help=("Open the car's position in Google Maps." if fix
              else "No GPS fix from the car — nothing real to open."))

    center = st.session_state.get("map_center")
    if follow or focus or center is None:
        center = (lat, lon)
    st.session_state["map_center"] = center

    nudge = st.session_state.get("map_focus_nudge", 0)
    if focus:
        # Pressing Focus twice with the car parked would otherwise send a view
        # state identical to the one on screen — and "unchanged centre" is
        # precisely how we tell the frontend to leave the pan alone, so the
        # second press would do nothing. Alternate a hand-width offset and
        # every press is a real change.
        nudge = 0 if nudge else 1
        st.session_state["map_focus_nudge"] = nudge

    st.pydeck_chart(_car_map_deck(lat, lon,
                                  center[0] + nudge * FOCUS_NUDGE_DEG, center[1]))

    # Say plainly whether that dot is the car or just the default map centre.
    if fix:
        st.caption(f":green[● GPS fix] — {lat:.5f}, {lon:.5f}")
    else:
        st.caption(":orange[○ No GPS position from the car] — "
                   "pin is the default Zolder centre, not the car. "
                   "Check gpsd on the Pi (`gpspipe -w`).")


@st.fragment(run_every=2)
def _driver_fragment():
    """Driver Telemetry tab — track position + live GPS map. Updates in place."""
    c = _live_context()
    st.markdown("### :material/sports_score: Track Position")
    st.markdown(render_sector_display(c["track_status"], c["current_lap_dist_m"],
                                      c["current_sector_id"]), unsafe_allow_html=True)

    st.markdown("### :material/timer: Sector Times")
    lap, splits, deltas = read_sector_times()
    render_sector_times(lap, splits, deltas)
    st.markdown("### :material/map: Live GPS Map")
    render_gps_map(c["state"])


# Sector boundaries, straight from the definitions the pit already displays.
SECTOR_BOUNDS = [(sid, info["range"][0], info["range"][1])
                 for sid, info in sorted(SECTIONS_INFO.items())]

# Reference splits from the 210 s baseline's own Time(s) column — what each
# sector "should" take on the base strategy.
REFERENCE_SPLITS = {1: 23.35, 2: 23.64, 3: 40.44, 4: 10.08, 5: 27.93,
                    6: 10.34, 7: 21.13, 8: 28.88, 9: 23.81}


def _crossing_time(samples, boundary_m):
    """When the car passed `boundary_m`, interpolated between samples.

    The car reports about once a second, and sectors 4 and 6 are only ~100 m
    long — about five samples wide at racing speed. Snapping a split to the
    nearest sample would put several tenths into a number displayed to 0.01 s,
    which is the difference between a sector delta that means something and one
    that is mostly sampling noise. So interpolate between the two samples that
    straddle the boundary.

    Returns None when no pair straddles it (the car never crossed, or the
    samples are missing).
    """
    for i in range(1, len(samples)):
        d0, d1 = samples[i - 1][1], samples[i][1]
        if d0 <= boundary_m <= d1 and d1 > d0:
            t0, t1 = samples[i - 1][0], samples[i][0]
            frac = (boundary_m - d0) / (d1 - d0)
            return t0 + frac * (t1 - t0)
    return None


def sector_splits(samples):
    """{sector_id: seconds} for one lap's (device_ts, lap_distance_m) samples."""
    if len(samples) < 2:
        return {}
    splits = {}
    for sid, start_m, end_m in SECTOR_BOUNDS:
        t_in = _crossing_time(samples, start_m) if start_m > 0 else samples[0][0]
        t_out = _crossing_time(samples, end_m)
        if t_in is not None and t_out is not None and t_out > t_in:
            splits[sid] = t_out - t_in
    return splits


@st.cache_data(ttl=4, show_spinner=False)
def read_sector_times():
    """Current lap's sector splits and the deltas to the previous lap.

    Returns (current_lap, {sid: seconds}, {sid: delta_vs_previous_seconds}).
    Everything is derived from telemetry already stored — `device_ts`,
    `lap_distance_m` and `calculated_lap` — so this needs no new car state and
    no new columns.
    """
    conn = db.get_conn()
    try:
        laps = db.recent_laps(conn, 2)
        if not laps:
            return None, {}, {}
        current = laps[0]
        cur = sector_splits([(r["device_ts"], r["lap_distance_m"])
                             for r in db.fetch_lap_track(conn, current)])
        prev = {}
        if len(laps) > 1:
            prev = sector_splits([(r["device_ts"], r["lap_distance_m"])
                                  for r in db.fetch_lap_track(conn, laps[1])])
    finally:
        conn.close()
    deltas = {sid: cur[sid] - prev[sid] for sid in cur if sid in prev}
    return current, cur, deltas


def render_sector_times(current_lap, splits, deltas):
    """F1-style split table: green with a minus when faster, red with a plus."""
    if not splits:
        st.info("Sector times appear once the car completes a sector. They are "
                "derived from lap distance, so they need the car reporting "
                "`lap_distance_m`.", icon=":material/info:")
        return

    st.caption(f"Lap {current_lap} · delta vs previous lap · "
               f"reference is the 210 s baseline")
    cells = st.columns(len(SECTOR_BOUNDS))
    for col, (sid, _s, _e) in zip(cells, SECTOR_BOUNDS):
        secs = splits.get(sid)
        with col:
            name = SECTION_NAMES.get(sid, f"S{sid}")
            if secs is None:
                st.markdown(
                    f"<div class='metric-container' style='padding:8px;'>"
                    f"<div class='metric-title'>S{sid}</div>"
                    f"<div style='font-size:20px;color:#8b98a5;'>—</div>"
                    f"<div style='font-size:10px;color:#8b98a5;'>{name}</div>"
                    f"</div>", unsafe_allow_html=True)
                continue

            d = deltas.get(sid)
            if d is None:
                delta_html = ("<span style='font-size:12px;color:#8b98a5;'>"
                              "first lap</span>")
            else:
                # Green/minus faster, red/plus slower — the F1 convention, and
                # the sign is explicit so it reads correctly at a glance.
                colour = "#2ecc71" if d < 0 else "#ff4444"
                delta_html = (f"<span style='font-size:13px;font-weight:bold;"
                              f"color:{colour};'>{d:+.2f}</span>")
            ref = REFERENCE_SPLITS.get(sid)
            ref_html = (f"<div style='font-size:10px;color:#556;'>ref "
                        f"{ref:.1f}s</div>" if ref else "")
            st.markdown(
                f"<div class='metric-container' style='padding:8px;'>"
                f"<div class='metric-title'>S{sid}</div>"
                f"<div style='font-size:20px;font-weight:bold;color:#00FFCC;'>"
                f"{secs:.2f}</div>{delta_html}{ref_html}</div>",
                unsafe_allow_html=True)


@st.cache_data(ttl=10, show_spinner=False)
def read_lap_df():
    """One row per completed lap, INDEXED BY LAP NUMBER.

    The X axis here is lap number, not time — the first frame in this app that
    is not a time series, which is why it is built separately from
    read_history_df rather than joining the shared time-indexed plot frame.

    No integration happens here. The car computed each lap's energy and time at
    the moment it cut the lap, and holds those figures for the whole of the next
    lap, so the query just picks up a value that is constant within each lap
    group. That is also why a dropped telemetry link cannot punch holes in these
    charts: one received sample per lap is enough.

    Cached for 10s to match the history fragment's refresh, so paging around the
    dashboard doesn't re-run the GROUP BY.
    """
    conn = db.get_conn()
    try:
        rows = db.fetch_lap_summary(conn)
    finally:
        conn.close()

    cols = ["Lap", "EnergyWh", "LapTimeS", "LapTimeMin", "DistanceM"]
    if not rows:
        return pd.DataFrame(columns=cols).set_index("Lap")

    recs = [{
        "Lap": r["lap"],
        "EnergyWh": r["energy_wh"],
        "LapTimeS": r["lap_time_s"],
        # Minutes as well: a 4 km lap is minutes long, and a y-axis in seconds
        # is harder to read against the 3.5-minute target pace.
        "LapTimeMin": (r["lap_time_s"] / 60.0) if r["lap_time_s"] else None,
        "DistanceM": r["distance_m"],
    } for r in rows]
    return pd.DataFrame(recs).set_index("Lap")


def _render_lap_charts():
    """Per-lap analysis: energy per lap and lap time, both X = lap number."""
    st.markdown("#### :material/timeline: Per-lap analysis")
    lap_df = read_lap_df()

    if lap_df.empty:
        st.info("No completed laps yet. Laps appear here once the car crosses "
                "the finish line (or the pit cuts one manually).",
                icon=":material/info:")
        return

    energy = lap_df["EnergyWh"].dropna()
    times = lap_df["LapTimeMin"].dropna()

    col_e, col_t = st.columns(2)
    with col_e:
        st.caption("Energy per Lap (Wh)")
        if energy.empty:
            st.info("No lap energy reported yet.", icon=":material/info:")
        else:
            # Bars: each lap is a discrete quantity, not a continuous signal.
            st.bar_chart(energy, color="#00B3FF", height=220)
    with col_t:
        st.caption("Lap Time History (minutes)")
        if times.empty:
            st.info("No lap times reported yet.", icon=":material/info:")
        else:
            st.line_chart(times, color="#00FFCC", height=220)

    # A compact summary — the numbers a strategist reads off these charts.
    if not energy.empty or not times.empty:
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Laps recorded", f"{len(lap_df)}")
        if not times.empty:
            s2.metric("Best lap", format_lap_time(times.min() * 60.0))
            s3.metric("Average lap", format_lap_time(times.mean() * 60.0))
        if not energy.empty:
            s4.metric("Average Wh/lap", f"{energy.mean():.1f}")


# ============================================================================
# HISTORY TAB
# ============================================================================
# The old version drew 13 st.line_charts inside one @st.fragment(run_every=10),
# so every tick rebuilt every chart, re-read 3000 fault rows and redrew two
# tables. You could not examine anything: the view was destroyed under you every
# ten seconds.
#
# The fix is a FREEZE, not a smarter chart. Streamlit hashes the whole figure
# JSON into the chart's element id and the browser uses that id as its React
# key, so *any* data change tears the plot down and rebuilds it — `key=` and
# `uirevision` cannot prevent it. The flip side is that identical JSON means an
# identical id and therefore no rebuild at all. So:
#
#   LIVE   — refreshes every 10s. Zoom is REMOVED rather than offered and
#            broken: dragging selects a time range, which freezes. Nobody gets
#            to zoom and then have it yanked away.
#   FROZEN — the chart fragment has no run_every at all, so nothing is redrawn
#            and zoom / pan / hover are perfectly stable for as long as you like.
#
# What the server knows: a scroll or modebar zoom happens entirely in the
# browser and is never reported back to Python. So the range used for stats and
# exports is the *server-known* one — window preset, narrowed by a drag — and it
# is printed on screen verbatim. Nothing here claims to export "what's visible",
# because after a free zoom that would be a lie.

HIST_CHART_KEY = "hist_chart"

# The wrapper only injects its own default removals when we supply none, so this
# list has to carry `sendDataToCloud` and `lasso2d` itself.
_HIST_MODEBAR_BASE = ["sendDataToCloud", "lasso2d", "select2d", "toggleSpikelines",
                      "hoverClosestCartesian", "hoverCompareCartesian"]


def _hist_chart_config(frozen):
    """Live: no zoom controls at all, so the only drag is "pick a range".
    Frozen: the full kit, because now nothing will redraw and take it away."""
    if frozen:
        return {"displaylogo": False, "scrollZoom": True,
                "modeBarButtonsToRemove": _HIST_MODEBAR_BASE}
    return {
        "displaylogo": False,
        "scrollZoom": False,
        "modeBarButtonsToRemove": _HIST_MODEBAR_BASE + [
            "zoom2d", "pan2d", "zoomIn2d", "zoomOut2d", "autoScale2d",
        ],
    }


def _hist_frozen_range():
    """The frozen (t0, t1) unix range, or None when live."""
    return st.session_state.get("hist_freeze")


def _hist_span_text(t0, t1):
    """Human range text. Includes dates the moment the span crosses midnight.

    Bare %H:%M:%S was actively misleading on this data: a range covering two
    months read as "19:24:38 → 15:11:58", which looks like it ends before it
    starts. A 24-hour race means most interesting ranges cross a date boundary."""
    same_day = t0.date() == t1.date()
    if same_day:
        return f"{t0:%H:%M:%S} → {t1:%H:%M:%S}"
    if (t1 - t0) < pd.Timedelta(days=1):
        return f"{t0:%H:%M:%S} → {t1:%H:%M:%S} (+1 day)"
    return f"{t0:%d %b %H:%M} → {t1:%d %b %H:%M}"


def _hist_bounds():
    """Server-known range as (start_ts, end_ts); end_ts None means "up to now"."""
    frozen = _hist_frozen_range()
    if frozen:
        return float(frozen[0]), float(frozen[1])
    window_min = HISTORY_WINDOWS.get(st.session_state.get("hist_window") or "15 min")
    if not window_min:
        return None, None
    # Quantise to whole HISTORY_CACHE_S steps: start_ts is part of
    # read_history_df's cache key, so a raw time.time() would mint a brand-new
    # key every tick and the cache would never hit once.
    now_q = int(time.time() // HISTORY_CACHE_S) * HISTORY_CACHE_S
    return now_q - window_min * 60, None


def _hist_frame():
    """(DataFrame, start_ts, end_ts) for the current range, full resolution."""
    start_ts, end_ts = _hist_bounds()
    df = read_history_df(start_ts=start_ts)
    if end_ts is not None and not df.empty:
        df = df[df["Time"] <= datetime.fromtimestamp(end_ts)]
    return df, start_ts, end_ts


def _hist_selected_charts():
    labels = st.session_state.get("hist_metrics")
    if not labels:
        return []
    return [c for c in HISTORY_CHARTS if c[1] in labels]


@st.cache_data(ttl=4, show_spinner=False)
def _hist_new_since(ts):
    """How many samples landed after `ts` — the "new data buffered" counter."""
    conn = db.get_conn()
    try:
        return db.count_samples_since(conn, ts)
    finally:
        conn.close()


def _break_time_gaps(plot_df, factor=8.0):
    """Insert a blank row wherever the car stopped reporting for a while.

    `connectgaps=False` only breaks a line on a missing VALUE. Two samples three
    weeks apart are still consecutive rows, so Plotly joins them with a straight
    line — which is how a collection gap ended up drawn as a confident diagonal
    sweep across the whole chart, looking like real telemetry. An all-NaN row at
    each discontinuity makes a gap render as what it is: nothing.

    `factor` is relative to the frame's own median interval, so this works the
    same on a 1-minute window as on a whole race.
    """
    if len(plot_df) < 3:
        return plot_df
    deltas = plot_df.index.to_series().diff()
    typical = deltas.median()
    if pd.isna(typical) or typical <= pd.Timedelta(0):
        return plot_df
    gap_at = deltas[deltas > typical * factor].index
    if len(gap_at) == 0:
        return plot_df
    # One blank sample-width before each resumption, so the break sits inside the
    # gap rather than on top of the first real reading after it.
    filler = pd.DataFrame(index=gap_at - typical, columns=plot_df.columns,
                          dtype="float64")
    return pd.concat([plot_df, filler]).sort_index()


def _hist_axis_plan(charts, normalize):
    """Which y-axis each unit lands on.

    One chart can be asked to hold km/h, W, % and °C at once, so metrics are
    grouped by unit: first group left, second right. A third has nowhere honest
    to go — we refuse to plot it against someone else's axis and say so.
    Returns (unit -> axis, units_plotted, units_refused)."""
    units = []
    for _key, _label, unit, _color in charts:
        if unit not in units:
            units.append(unit)
    if normalize:
        return {u: "y" for u in units}, units, []
    plan = {u: ("y" if i == 0 else "y2") for i, u in enumerate(units[:2])}
    return plan, units[:2], units[2:]


def _hist_figure(plot_df, charts, normalize, dark, view_rev):
    """The overlay figure. `view_rev` must change only when the view should be
    reset (range, metrics, scale) — while it holds steady Plotly keeps whatever
    the user did by hand."""
    plan, plotted, refused = _hist_axis_plan(charts, normalize)
    grid = "rgba(255,255,255,0.09)" if dark else "rgba(0,0,0,0.10)"
    fg = "#c9d3dd" if dark else "#1c2733"

    fig = go.Figure()
    for key, label, unit, color in charts:
        if unit in refused:
            continue
        series = plot_df[key]
        trace = dict(x=plot_df.index, name=f"{label} ({unit})", mode="lines",
                     line=dict(color=color, width=2.2),
                     # NaN is a real gap in telemetry, so let it show as one
                     # instead of bridging it with a line that never happened.
                     connectgaps=False)
        if normalize:
            lo, hi = series.min(), series.max()
            span = hi - lo
            # A flat series has no range to scale into; park it mid-chart rather
            # than dividing by zero.
            trace["y"] = ((series - lo) / span * 100.0) if span else series * 0 + 50.0
            # The SHAPE is normalised but the number you read is not: real values
            # ride along in customdata and that is what the tooltip prints.
            trace["customdata"] = series
            trace["hovertemplate"] = f"{label} <b>%{{customdata:.2f}}</b> {unit}<extra></extra>"
        else:
            trace["y"] = series
            trace["yaxis"] = plan[unit]
            trace["hovertemplate"] = f"{label} <b>%{{y:.2f}}</b> {unit}<extra></extra>"
        fig.add_trace(go.Scatter(**trace))

    frozen = _hist_frozen_range() is not None
    fig.update_layout(
        uirevision=view_rev,
        height=470,
        # Zero side margins + automargin on the axes: Plotly then reserves
        # exactly the width the tick labels need. A fixed l=10 clipped the
        # leading digit off every y label ("50" rendered as "0").
        margin=dict(l=0, r=0, t=30, b=0, pad=4, autoexpand=True),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=fg, size=13),
        # The modebar inherits the (transparent) paper colour otherwise, which
        # left dark-on-dark icons that were effectively invisible.
        modebar=dict(bgcolor="rgba(0,0,0,0)", color=fg,
                     activecolor="#00ffcc" if dark else "#0a3d62"),
        hovermode="x unified",
        hoverlabel=dict(bgcolor="#0d1117" if dark else "#ffffff",
                        bordercolor=grid, font=dict(color=fg, size=12)),
        # Live: drag = pick a range (and freeze). Frozen: drag = pan, since the
        # range is already chosen. Horizontal only — the vertical extent of a
        # time selection is meaningless.
        dragmode="pan" if frozen else "select",
        selectdirection="h",
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0,
                    bgcolor="rgba(0,0,0,0)"),
        xaxis=dict(gridcolor=grid, zeroline=False, showspikes=True,
                   spikemode="across", spikesnap="cursor", spikethickness=1,
                   spikedash="dot", spikecolor=fg, automargin=True),
        # Unit as a horizontal caption above the axis, not a rotated title: a
        # sideways "km/h" squeezed against the tick labels was unreadable and ate
        # the width the labels needed.
        yaxis=dict(gridcolor=grid, zeroline=False, automargin=True,
                   ticksuffix=" ",
                   title=dict(text=("% of range" if normalize
                                    else (plotted[0] if plotted else "")),
                              font=dict(size=11), standoff=6)),
    )
    if len(plotted) > 1 and not normalize:
        fig.update_layout(yaxis2=dict(overlaying="y", side="right", showgrid=False,
                                      zeroline=False, automargin=True,
                                      title=dict(text=plotted[1],
                                                 font=dict(size=11), standoff=6)))
    return fig, refused


def _hist_range_from_selection(selection, plot_df):
    """Unix (t0, t1) from a Plotly box selection, or None.

    Prefers `point_indices` — every trace shares one index, so the indices give
    an exact window with no date parsing. Falls back to the box rectangle's own
    x values (plotly date strings) for a drag that spans a gap and so caught no
    points. This is a client-supplied payload, so it is parsed defensively."""
    if not selection:
        return None
    idx = [i for i in (selection.get("point_indices") or [])
           if isinstance(i, int) and 0 <= i < len(plot_df)]
    if idx:
        t0 = plot_df.index[min(idx)].timestamp()
        t1 = plot_df.index[max(idx)].timestamp()
    else:
        boxes = selection.get("box") or []
        xs = list(boxes[0].get("x") or []) if boxes else []
        stamps = []
        for v in xs:
            try:
                stamps.append(pd.Timestamp(v).timestamp())
            except (ValueError, TypeError):
                continue
        if len(stamps) < 2:
            return None
        t0, t1 = min(stamps), max(stamps)
    # A stray click reads as a zero-width box, which is not a range.
    return (t0, t1) if t1 - t0 >= 1.0 else None


FILTER_OFF = "— none —"


def _clear_hist_filter():
    """Reset the value filter. Must run as a widget callback — see the caller."""
    st.session_state["hist_filter_metric"] = FILTER_OFF
    st.session_state["hist_filter_min"] = None
    st.session_state["hist_filter_max"] = None


def _apply_value_filter(df, plotted_labels=()):
    """Blank the chosen metric outside its min/max band.

    Returns (frame, note). Masks that metric's COLUMN rather than dropping rows,
    so the other traces keep their full history — you are filtering one signal,
    not deleting moments in time.

    The masked cells become NaN, which is deliberate: NaN is already the "no
    reading" value everywhere in this app, so the chart gaps there and min/avg/max
    and both CSVs recompute without them automatically. The filter reaches the
    numbers, not just the picture — which is the whole point of filtering out a
    bad reading rather than just scrolling it off screen. `note` is what tells the
    user it happened, so a filtered stat can never be mistaken for an unfiltered
    one.
    """
    label = st.session_state.get("hist_filter_metric")
    if not label or label == FILTER_OFF:
        return df, None
    entry = next((c for c in HISTORY_CHARTS if c[1] == label), None)
    if entry is None or entry[0] not in df:
        return df, None
    lo = st.session_state.get("hist_filter_min")
    hi = st.session_state.get("hist_filter_max")
    if lo is None and hi is None:
        return df, None

    col, _lbl, unit, _colour = entry
    # A band set on a metric that is not on the chart changes nothing, so saying
    # "filter active" would be as misleading as saying nothing. Name the reason.
    if plotted_labels and label not in plotted_labels:
        return df, f"{label} filter is set but {label} is not plotted — no effect"
    series = df[col]
    keep = series.notna()
    if lo is not None:
        keep &= series >= lo
    if hi is not None:
        keep &= series <= hi
    hidden = int((series.notna() & ~keep).sum())
    if not hidden:
        return df, None

    df = df.copy()
    df[col] = series.where(keep)
    bounds = (f"{lo:g}–{hi:g}" if lo is not None and hi is not None
              else (f"≥ {lo:g}" if hi is None else f"≤ {hi:g}"))
    return df, f"{label} filtered to {bounds} {unit} · {hidden:,} reading(s) hidden"


def _hist_count_between(df, span):
    """How many samples of `df` fall inside a (t0, t1) unix span."""
    t0, t1 = (datetime.fromtimestamp(span[0]), datetime.fromtimestamp(span[1]))
    return int(((df["Time"] >= t0) & (df["Time"] <= t1)).sum())


def _hist_fmt(value):
    if value is None or pd.isna(value):
        return MISSING_TEXT
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:.1f}"


def _hist_stat_card(label, unit, color, series):
    """min / avg / max / now for one metric over the server-known range.

    NaN-aware throughout: pandas skips missing readings, so a dropout no longer
    drags the average toward zero the way the old `or 0` coalescing did. The
    count of missing samples is shown rather than hidden."""
    clean = series.dropna()
    if clean.empty:
        return (f'<div class="metric-container hist-stat" style="border-left-color:{color}">'
                f'<div class="metric-title">{label}</div>'
                f'<div class="hist-now" style="color:{color}">{MISSING_TEXT}</div>'
                f'<div class="hist-foot">no readings in range</div></div>')
    missing = int(series.isna().sum())
    foot = f"{len(clean):,} samples"
    if missing:
        foot += f" · {missing:,} missing"
    return (
        f'<div class="metric-container hist-stat" style="border-left-color:{color}">'
        f'<div class="metric-title">{label} <span class="hist-unit">{unit}</span></div>'
        f'<div class="hist-now" style="color:{color}">{_hist_fmt(clean.iloc[-1])}</div>'
        f'<div class="hist-mmm"><span>min <b>{_hist_fmt(clean.min())}</b></span>'
        f'<span>avg <b>{_hist_fmt(clean.mean())}</b></span>'
        f'<span>max <b>{_hist_fmt(clean.max())}</b></span></div>'
        f'<div class="hist-foot">{foot}</div></div>'
    )


def _render_hist_stats(df, charts):
    per_row = 4
    for i in range(0, len(charts), per_row):
        cols = st.columns(per_row)
        for col, (key, label, unit, color) in zip(cols, charts[i:i + per_row]):
            col.markdown(_hist_stat_card(label, unit, color, df[key]),
                         unsafe_allow_html=True)


def _render_hist_export(df, charts):
    """CSV for exactly the range the caption above names.

    Built from the same DataFrame the chart and the stats used, so the picture,
    the numbers and the file cannot disagree. `data=` takes a callable, which
    Streamlit runs on click — so nothing is generated on the refresh tick."""
    t0, t1 = df["Time"].iloc[0], df["Time"].iloc[-1]
    stem = (f"history_{'-'.join(c[1].lower().replace(' ', '') for c in charts[:3])}"
            f"{f'-plus{len(charts) - 3}' if len(charts) > 3 else ''}"
            f"_{t0:%Y%m%d_%H%M%S}-{t1:%H%M%S}")

    st.markdown("##### :material/download: Export this range")
    c1, c2, c3 = st.columns([2, 1, 1])
    c1.text_input("Session name", key="hist_session",
                  placeholder="Session name for the report header (optional)",
                  label_visibility="collapsed")
    session = st.session_state.get("hist_session", "")
    rows = len(df)
    for col, style, label, tip in (
        (c2, "data", "CSV (data)", "Clean table — opens straight into Excel or Sheets."),
        (c3, "report", "CSV (report)", "Same table behind a documented header block."),
    ):
        col.download_button(
            f":material/table_view: {label} · {rows:,}",
            # A closure, not bytes: Streamlit only calls it if the user actually
            # clicks. Must stay pure pandas/csv — st.* calls inside are ignored.
            data=lambda s=style: export.history_csv_bytes(df, charts, style=s,
                                                          session=session),
            file_name=f"{stem}{'_report' if style == 'report' else ''}.csv",
            mime="text/csv", key=f"hist_csv_{style}", help=tip,
            on_click="ignore", width="stretch")


@st.fragment(run_every=STATUS_TICK_S)
def _history_status_fragment():
    """The LIVE / FROZEN badge.

    Its own tiny fragment on purpose: it keeps ticking to count incoming samples
    while the chart fragment is completely stopped, so you get a live "new data
    waiting" signal without anything redrawing under your cursor."""
    frozen = _hist_frozen_range()
    left, right = st.columns([5, 1])

    if not frozen:
        left.markdown(
            f'<div class="hist-badge live">● LIVE · refreshing every {CHART_TICK_S}s'
            f' · drag across the chart to freeze and examine a range</div>',
            unsafe_allow_html=True)
        if right.button(":material/pause: Freeze", key="hist_freeze_btn",
                        width="stretch", help="Stop refreshing so you can zoom in."):
            start_ts, _ = _hist_bounds()
            now = time.time()
            # Same guard as the drag: freezing a window that holds nothing would
            # strand you on an empty chart. Nothing is in the future, so "samples
            # at or after the window start" is exactly the window's own count.
            if _hist_new_since(start_ts or 0.0) == 0:
                st.toast("Nothing in this window to freeze yet.",
                         icon=":material/info:")
                return
            st.session_state["hist_freeze"] = (start_ts or 0.0, now)
            st.session_state["hist_frozen_at"] = now
            # Full app rerun, NOT fragment scope: the run_every timer is only
            # registered/cleared on a whole-app run, so a fragment rerun would
            # leave the chart ticking while it is meant to be frozen.
            st.rerun()
        return

    # Count from the moment you FROZE, not from the end of the selected range.
    # Counting from the range end meant selecting a window in the middle of a
    # race reported every later sample as "new samples waiting" — 19,541 of them,
    # none of which were new.
    since = st.session_state.get("hist_frozen_at") or frozen[1]
    waiting = _hist_new_since(since)
    extra = (f" · {waiting:,} new sample{'s' if waiting != 1 else ''} since you froze"
             if waiting else "")
    # The range itself is NOT repeated here — the caption under the chart owns it.
    left.markdown(
        f'<div class="hist-badge frozen">⏸ FROZEN · not refreshing · '
        f'zoom and hover freely, nothing will move{extra}</div>',
        unsafe_allow_html=True)
    if right.button(":material/play_arrow: Resume", key="hist_resume_btn",
                    width="stretch", help="Go back to live refreshing."):
        st.session_state["hist_freeze"] = None
        st.rerun()


def _render_history_chart():
    """Chart + stats + export. Deliberately undecorated — main() wraps this with
    a run_every so freezing can stop the tick outright."""
    charts = _hist_selected_charts()
    if not charts:
        st.info("Pick one or more metrics above.", icon=":material/info:")
        return
    df, start_ts, end_ts = _hist_frame()
    if df.empty:
        # Frozen-and-empty needs different advice from live-and-empty. Telling
        # someone to "start collector.py" when they have simply frozen a span
        # with nothing in it sends them to debug a process that is running fine,
        # and the chart they need to drag on is no longer there to drag.
        if _hist_frozen_range():
            st.info("Nothing was recorded in the range you froze.",
                    icon=":material/info:")
            if st.button(":material/play_arrow: Resume live", key="hist_resume_empty",
                         type="primary"):
                st.session_state["hist_freeze"] = None
                st.rerun()
        else:
            st.info("No telemetry in this range. Widen the window or start collector.py.",
                    icon=":material/info:")
        return

    # Applied before anything else reads the frame, so the chart, the stats and
    # both CSVs are all filtered identically — they cannot disagree.
    df, filter_note = _apply_value_filter(df, [c[1] for c in charts])

    thinned = _downsample(df.set_index("Time"), OVERLAY_MAX_POINTS)
    # Count the real plotted samples BEFORE gap-breaking adds blank rows —
    # otherwise the caption credits the fillers as drawn data.
    drawn = len(thinned)
    plot_df = _break_time_gaps(thinned)
    frozen = _hist_frozen_range() is not None
    normalize = st.session_state.get("hist_scale") == "Normalize"

    # The one honesty line, and the ONLY place the range is stated — the frozen
    # badge deliberately does not repeat it. Two range readouts on one screen
    # disagreed the moment one of them rounded differently, which made both
    # untrustworthy. min/max rather than the first and last rows, so an
    # out-of-order timestamp in the store cannot invert it.
    note = (f"Range **{_hist_span_text(df['Time'].min(), df['Time'].max())}** · "
            f"{len(df):,} samples")
    if drawn != len(df):
        note += f" · chart drawing {drawn:,} of them (exports use all)"
    if frozen:
        note += " · zooming does not change this range — drag a new box to re-scope"
    # A filter that is not announced is a silent edit to the numbers. The controls
    # live in a popover, so this line is the only thing that says it is on.
    if filter_note:
        note += f"\n\n:orange[**Filter active** — {filter_note}]"
    st.caption(note)

    if HAS_PLOTLY:
        # Changing this is what lets a new range take effect; while it holds
        # steady Plotly keeps the user's own zoom instead of overriding it.
        view_rev = "|".join(str(x) for x in (
            st.session_state.get("hist_window"), _hist_frozen_range(), normalize,
            ",".join(c[1] for c in charts),
            # Filtering changes what the axes should span, so the view has to be
            # allowed to rescale rather than keeping the old zoom.
            st.session_state.get("hist_filter_metric"),
            st.session_state.get("hist_filter_min"),
            st.session_state.get("hist_filter_max")))
        fig, refused = _hist_figure(plot_df, charts, normalize,
                                    st.session_state.get("theme_mode") != "Light",
                                    view_rev)
        event = st.plotly_chart(fig, key=HIST_CHART_KEY, theme=None,
                                on_select="rerun", selection_mode="box",
                                config=_hist_chart_config(frozen))
        if refused:
            st.caption(
                f":orange[{', '.join(refused)} not plotted] — one chart carries two "
                f"units honestly, not more. Switch **Scale** to Normalize to compare "
                f"every metric by shape.")
        picked = _hist_range_from_selection(getattr(event, "selection", None), plot_df)
        if picked and picked != _hist_frozen_range():
            # Never freeze onto a span with nothing in it. On a wide window the
            # data can occupy a small slice of the axis, so a drag across the
            # empty part used to freeze you into "No telemetry in this range" —
            # an empty chart with nothing left to drag on to get out of it.
            if _hist_count_between(df, picked):
                st.session_state["hist_freeze"] = picked
                st.session_state["hist_frozen_at"] = time.time()
                st.rerun()   # app scope — see _history_status_fragment
            else:
                st.toast("Nothing recorded in that span — still live. "
                         "Drag across a part of the trace that has data.",
                         icon=":material/info:")
    else:
        st.warning("Plotly is missing, so this falls back to basic charts with no "
                   "zoom or freeze. Run `pip install -r requirements_pit.txt`.",
                   icon=":material/warning:")
        for i in range(0, len(charts), 3):
            for col, (key, label, unit, color) in zip(st.columns(3), charts[i:i + 3]):
                col.caption(f"{label} ({unit})")
                col.line_chart(plot_df[key], color=color, height=200)

    _render_hist_stats(df, charts)
    _render_hist_export(df, charts)


@st.fragment(run_every=TABLE_TICK_S)
def _history_tables_fragment():
    """Recent samples, per-lap charts and fault history.

    Split off the chart's cadence and slowed to 30s: these were most of the cost
    of the old 10s tick and none of them need to be live while you read a chart.
    (An st.expander still executes its body — it tidies the page, it does not
    defer the work. The saving here comes from the slower tick plus caching.)"""
    with st.expander(":material/history: Recent Samples"):
        df, _s, _e = _hist_frame()
        if df.empty:
            st.caption("Nothing in this range.")
        else:
            st.dataframe(df.tail(200).round(2), width="stretch", hide_index=True)

    # Per-lap charts are keyed to lap number, not the selected time window, so
    # completed laps stay visible even when the window holds no samples.
    with st.expander(":material/timer: Per-Lap Energy & Times", expanded=True):
        _render_lap_charts()

    episodes = read_fault_episodes()
    with st.expander(f":material/error: Errors History ({len(episodes)})"):
        if episodes:
            err_df = pd.DataFrame([{
                "Start": datetime.fromtimestamp(e["start"]),
                "Duration": f"{e['end'] - e['start']:.0f}s",
                "Fault": e["sig"],
            } for e in reversed(episodes)])   # newest first
            st.dataframe(err_df, width="stretch", hide_index=True)
        else:
            st.success("No faults recorded in history.",
                       icon=":material/check_circle:")


@st.fragment(run_every=10)
def _weather_fragment():
    """Weather tab — solar irradiance forecast (cached 1h). In place."""
    st.markdown("### :material/sunny: Solar Irradiance Forecast (Next 24H)")
    weather_df = fetch_zolder_weather()
    if weather_df is not None:
        wc1, wc2 = st.columns([2, 1])
        with wc1:
            st.line_chart(weather_df.set_index("Time")["Solar Radiation (W/m²)"],
                          color="#f1c40f", height=250)
        with wc2:
            st.dataframe(weather_df[["Time", "Cloud Cover (%)"]], hide_index=True, height=250)
    else:
        st.warning("Weather API unavailable.")


@st.fragment(run_every=10)
def _strategy_fragment():
    """Strategy tab — consumption matrix + combined graph. In place."""
    _, time_left_min = _elapsed_and_left()
    state, _ = read_live_state()
    soc = state["soc"]
    manual = int(st.session_state.get("manual_lap", -1))
    active_lap = manual if manual >= 0 else state["auto_lap"]

    render_strategy_selector()

    st.markdown("### :material/insights: Strategy Matrix")
    # From constants.STRATEGIES so the matrix, the remote selector and the
    # generated profiles can never disagree about what a strategy is.
    consumption_table = [{'label': s['label'], 'lap_time_min': s['lap_time_min'],
                          'energy_wh': s['energy_wh']} for s in STRATEGIES]
    # `not soc` covers both a missing reading and a reported 0: neither is a
    # usable capacity, so the matrix assumes a full pack rather than telling the
    # strategist the car is empty. (This already treated 0 that way; None just
    # joins it now that an unreported SoC stays None instead of becoming 0.)
    car_battery_wh = 8550 if not soc else (soc / 100.0) * 8550
    if soc is None or active_lap is None:
        missing = " or ".join(n for n, v in (("battery SoC", soc),
                                             ("lap count", active_lap))
                              if v is None)
        st.caption(f":orange[Assuming a full pack / lap 0 — no {missing} from the "
                   f"car yet.] These figures are a placeholder until it reports.")
    all_strategies = calculate_all_strategies(time_left_min, car_battery_wh,
                                              active_lap or 0, consumption_table)
    display_df = pd.DataFrame(all_strategies)
    graph_data_list = display_df.pop('_graph_data').tolist()
    st.dataframe(display_df, width="stretch", hide_index=True)
    fig = create_combined_graph(graph_data_list)
    st.pyplot(fig)
    plt.close(fig)   # release the matplotlib figure so they don't pile up


# ============================================================================
# DRIVER MESSAGE — send a short instruction to the car HUD (pit's only FB write)
# ============================================================================
def render_strategy_selector():
    """Choose a speed profile and send it to the car.

    Only the strategy NAME goes over the link — the car already holds all five
    generated profiles, so this is a few bytes rather than a 400-row table, and
    the profile the car flies is the one committed to git rather than whatever
    was pushed at the time.

    Changing this changes the target speed on the driver's HUD and the corner
    warnings they get, so the car's acknowledgement is shown back: "sent" and
    "the car is running it" are not the same thing.
    """
    import driver_message  # lazy import: keep FB-write deps out of app startup

    st.markdown("### :material/route: Active Strategy")
    col_sel, col_send = st.columns([3, 1])
    with col_sel:
        labels = [s["label"] for s in STRATEGIES]
        default_idx = next((i for i, s in enumerate(STRATEGIES)
                            if s["key"] == DEFAULT_STRATEGY_KEY), 0)
        choice = st.selectbox("Speed profile", labels, index=default_idx,
                              key="strategy_choice",
                              label_visibility="collapsed")
    with col_send:
        send = st.button(":material/send: Send to Car", width="stretch",
                         key="strategy_send")

    chosen = STRATEGY_BY_LABEL[choice]
    st.caption(f"{chosen['label']} — target lap "
               f"{chosen['lap_time_min'] * 60:.0f}s, "
               f"{chosen['energy_wh']:.0f} Wh/lap · profile "
               f"`{chosen['key']}`")

    if send:
        try:
            driver_message.send_strategy(chosen["key"])
            st.session_state.strategy_sent = (chosen["key"],
                                              time.strftime("%H:%M:%S"))
            st.toast(f"Sent {chosen['label']} to the car",
                     icon=":material/check_circle:")
        except Exception as e:
            st.toast("Strategy send failed", icon=":material/error:")
            st.error(f"Strategy send failed: {e}")

    sent = st.session_state.get("strategy_sent")
    if sent:
        ack = driver_message.read_strategy_ack()
        if isinstance(ack, dict) and ack.get("applied") \
                and ack.get("strategy") == sent[0]:
            st.success(f"Car confirmed **{ack.get('strategy')}** · sent {sent[1]}",
                       icon=":material/check_circle:")
        elif isinstance(ack, dict) and ack.get("note"):
            # The car rejected it — usually a profile it does not have, which
            # means the generated CSVs have not been deployed to the Pi.
            st.warning(f"Car reported: {ack['note']}", icon=":material/warning:")
        else:
            st.info(f"Sent {sent[0]} at {sent[1]} — awaiting the car's "
                    f"confirmation.", icon=":material/schedule:")


def render_cut_lap_panel():
    """Ask the CAR to close its current lap now.

    This is NOT the "Manual Lap (-1 for Auto)" box above it, and the two are
    deliberately independent:

      Manual Lap   a local display correction. Changes the lap NUMBER shown on
                   this dashboard. Never leaves the pit, never touches the car.
      Cut Lap      a command sent to the car. Makes the Pi close the current lap
                   exactly as a GPS finish-line crossing would — snapshotting
                   lap energy and lap time — so the lap appears in the per-lap
                   charts. Does not alter the Manual Lap box.

    Use this when the GPS trigger missed a crossing, or on a track where the
    finish line coordinates are not set up.
    """
    import driver_message  # lazy import: keep FB-write deps out of app startup

    st.sidebar.caption("Cut Lap asks the **car** to close its lap "
                       "(snapshots lap energy + time). It does not change the "
                       "Manual Lap box above.")
    if st.sidebar.button(":material/flag_circle: CUT LAP NOW", width="stretch",
                         key="cut_lap_btn"):
        try:
            driver_message.send_lap_cut()
            st.session_state.cut_lap_last = time.strftime("%H:%M:%S")
            st.toast("Cut Lap sent to the car", icon=":material/check_circle:")
        except Exception as e:
            st.toast("Cut Lap failed", icon=":material/error:")
            st.sidebar.error(f"Cut Lap failed: {e}")

    sent = st.session_state.get("cut_lap_last")
    if sent:
        # Show the car's acknowledgement when it comes back, so the engineer
        # knows the command actually landed rather than just left the pit.
        ack = driver_message.read_lap_ack()
        if isinstance(ack, dict) and ack.get("applied"):
            st.sidebar.caption(
                f":material/check_circle: Car confirmed — now on lap "
                f"**{ack.get('lap')}** · sent {sent}")
        else:
            st.sidebar.caption(f":material/schedule: Cut Lap sent {sent} — "
                               "awaiting the car's confirmation.")


def render_driver_message_panel():
    """Send a short instruction to the car's HUD. Two free-form modes: an open
    text message, or a label + number (e.g. 'Wanted Speed: 70'). This is the pit
    wall's only Firebase *write* — everything else is read-only from SQLite."""
    import driver_message  # lazy import: keep FB-write deps out of app startup

    st.sidebar.markdown("---")
    st.sidebar.header(":material/campaign: Driver Message")

    # Two-way mode toggle (segmented control; radio fallback on older Streamlit).
    try:
        mode = st.sidebar.segmented_control(
            "Type", ["Label + number", "Text message"],
            default="Label + number", key="dm_mode")
    except Exception:
        mode = st.sidebar.radio("Type", ["Label + number", "Text message"],
                                horizontal=True, key="dm_mode")

    if mode == "Text message":
        text = st.sidebar.text_input("Message", key="dm_text",
                                     placeholder="e.g. PIT IN NOW")
        send_category, send_value = "", text.strip()
        ready = bool(send_value)
    else:
        label = st.sidebar.text_input("Label", key="dm_label",
                                      placeholder="e.g. Wanted Speed")
        number = st.sidebar.number_input("Number", value=0, step=1, key="dm_num")
        send_category, send_value = label.strip().upper(), number
        ready = bool(send_category)

    col_send, col_clear = st.sidebar.columns(2)
    if col_send.button(":material/send: Send", width="stretch",
                       disabled=not ready, key="dm_send"):
        try:
            driver_message.send_driver_command(send_category, send_value)
            shown = f"{send_category}: {send_value}" if send_category else str(send_value)
            st.session_state.dm_last = (shown, time.strftime("%H:%M:%S"))
            st.toast("Sent to driver", icon=":material/check_circle:")
        except Exception as e:
            st.toast("Send failed", icon=":material/error:")
            st.sidebar.error(f"Send failed: {e}")

    if col_clear.button(":material/close: Clear", width="stretch", key="dm_clear"):
        try:
            driver_message.clear_driver_command()
            st.session_state.dm_last = None
            st.toast("Cleared driver message", icon=":material/check_circle:")
        except Exception as e:
            st.toast("Clear failed", icon=":material/error:")
            st.sidebar.error(f"Clear failed: {e}")

    last = st.session_state.get("dm_last")
    if last:
        st.sidebar.caption(f":material/campaign: Now showing: **{last[0]}** · sent {last[1]}")
    else:
        st.sidebar.caption("Nothing on the driver HUD right now.")


# ============================================================================
# EXPORT PANEL — filter by date/time + subsystem, then download an Excel workbook
# ============================================================================
def render_export_panel():
    st.sidebar.markdown("### :material/download: Export Telemetry (Excel)")

    conn = db.get_conn()
    try:
        db.init_db(conn)  # make sure the table exists even before first ingest
        lo, hi = db.time_bounds(conn)
        total = db.count_samples(conn)
    finally:
        conn.close()

    if not total or lo is None:
        st.sidebar.caption(":material/warning: No telemetry stored yet — start collector.py.")
        return

    lo_dt = datetime.fromtimestamp(lo)
    hi_dt = datetime.fromtimestamp(hi)
    st.sidebar.caption(f"{total:,} samples · {lo_dt:%Y-%m-%d %H:%M} → {hi_dt:%H:%M}")

    # Subsystem filter (BMS / MMS / Temperature / Motion-GPS)
    groups = st.sidebar.multiselect(
        "Systems to include",
        options=list(export.METRIC_GROUPS.keys()),
        default=list(export.METRIC_GROUPS.keys()),
        help="Pick which subsystems go into the workbook (columns, charts, and "
             "the Faults sheet).",
    )

    # Date/time range filter
    cda, cta = st.sidebar.columns(2)
    start_date = cda.date_input("Start date", value=lo_dt.date(), key="exp_sd")
    start_time = cta.time_input("Start time", value=lo_dt.time(), key="exp_st")
    cdb, ctb = st.sidebar.columns(2)
    end_date = cdb.date_input("End date", value=hi_dt.date(), key="exp_ed")
    end_time = ctb.time_input("End time", value=hi_dt.time(), key="exp_et")

    start_ts = datetime.combine(start_date, start_time).timestamp()
    end_ts = datetime.combine(end_date, end_time).timestamp()

    if st.sidebar.button(":material/tune: Prepare Excel", width="stretch"):
        if start_ts > end_ts:
            st.sidebar.error("Start is after end.")
        elif not groups:
            st.sidebar.error("Pick at least one system.")
        else:
            metrics = export.metrics_for_groups(groups)
            with st.spinner("Building workbook…"):
                xlsx_bytes, n = export.to_xlsx_bytes(
                    start_ts=start_ts, end_ts=end_ts, metrics=metrics)
            st.session_state.export_bytes = xlsx_bytes
            st.session_state.export_rows = n
            st.session_state.export_name = (
                f"telemetry_{datetime.fromtimestamp(start_ts):%Y%m%d-%H%M}"
                f"_{datetime.fromtimestamp(end_ts):%H%M}.xlsx"
            )

    if st.session_state.get("export_bytes"):
        st.sidebar.download_button(
            label=f":material/download: Download Excel ({st.session_state.export_rows:,} rows)",
            data=st.session_state.export_bytes,
            file_name=st.session_state.export_name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )


# ============================================================================
# MAIN
# ============================================================================
def _persist_race():
    """Save the current race clock to SQLite so a refresh doesn't lose it."""
    conn = db.get_conn()
    try:
        db.save_race_state(conn, st.session_state.is_racing,
                           st.session_state.race_start_time)
    finally:
        conn.close()


def main():
    # Appearance: one small Dark/Light toggle, pinned top-left of the page (above
    # the title). The icon shows the mode you'll switch TO — a sun while dark, a
    # moon while light — so it reads light-on-dark and dark-on-light. Defined here
    # (not in a fragment) so its click drives a normal rerun; apply_theme injects
    # global CSS that applies wherever the button sits in the DOM.
    if "theme_mode" not in st.session_state:
        st.session_state.theme_mode = "Dark"
    if "font_scale" not in st.session_state:
        st.session_state.font_scale = 1.0
    # History tab state. Seeded here rather than via each widget's `default=`,
    # because Streamlit warns when a key is both pre-set and given a default.
    for hist_key, hist_default in (("hist_window", "15 min"),
                                   ("hist_metrics", HISTORY_DEFAULT_METRICS),
                                   ("hist_scale", "Raw units"),
                                   ("hist_freeze", None),
                                   ("hist_frozen_at", None),
                                   ("hist_filter_metric", FILTER_OFF),
                                   ("hist_filter_min", None),
                                   ("hist_filter_max", None),
                                   ("hist_session", "")):
        st.session_state.setdefault(hist_key, hist_default)
    is_dark = st.session_state.theme_mode == "Dark"
    if st.button(
        ":material/light_mode:" if is_dark else ":material/dark_mode:",
        key="theme_toggle_btn",
        help="Switch to light mode" if is_dark else "Switch to dark mode",
    ):
        st.session_state.theme_mode = "Light" if is_dark else "Dark"
        st.rerun()
    apply_theme(st.session_state.theme_mode)
    apply_font_scale(st.session_state.font_scale)

    st.title(":material/speed: Afeka Pit Wall - Pro Racing Dashboard")

    # Ensure the schema exists / is migrated (adds fault columns to older DBs)
    # before the live fragment starts reading, and load the persisted race
    # clock so a browser refresh keeps a running race (no manual restore).
    conn = db.get_conn()
    try:
        db.init_db(conn)
        if "race_loaded" not in st.session_state:
            rs = db.load_race_state(conn)
            st.session_state.race_start_time = rs["race_start_time"]
            st.session_state.is_racing = rs["is_racing"]
            st.session_state.race_loaded = True
    finally:
        conn.close()

    # --- Text size (personal readability) --------------------------------- #
    # Two buttons scale the MAIN content only (see apply_font_scale). Living in
    # the sidebar means they don't grow/shift under the cursor while clicking.
    st.sidebar.caption(
        f":material/format_size: Text size — {int(st.session_state.font_scale * 100)}%"
    )
    _c_dec, _c_inc = st.sidebar.columns(2)
    if _c_dec.button(":material/text_decrease:", key="font_dec",
                     help="Smaller text", width="stretch"):
        st.session_state.font_scale = max(0.7, round(st.session_state.font_scale - 0.1, 2))
        st.rerun()
    if _c_inc.button(":material/text_increase:", key="font_inc",
                     help="Larger text", width="stretch"):
        st.session_state.font_scale = min(1.8, round(st.session_state.font_scale + 0.1, 2))
        st.rerun()

    # --- Race control (Start / Restore) ----------------------------------- #
    st.sidebar.header(":material/flag: Race Control")
    if not st.session_state.is_racing:
        if st.sidebar.button(":material/play_arrow: START FRESH 24H RACE", width="stretch"):
            st.session_state.race_start_time = time.time()
            st.session_state.is_racing = True
            _persist_race()
            st.rerun()
        with st.sidebar.expander(":material/manage_history: Restore / set elapsed time"):
            col_h, col_m = st.columns(2)
            restore_h = col_h.number_input("Hours", 0, 24, 23)
            restore_m = col_m.number_input("Mins", 0, 59, 59)
            if st.button(":material/restart_alt: RESUME", width="stretch"):
                elapsed_mins = 1440.0 - ((restore_h * 60) + restore_m)
                st.session_state.race_start_time = time.time() - (elapsed_mins * 60.0)
                st.session_state.is_racing = True
                _persist_race()
                st.rerun()
    else:
        st.sidebar.success("Race in progress — auto-saved (survives refresh).",
                           icon=":material/check_circle:")

    # --- Live race status — placed high so it's the first thing you see ---- #
    # Rendered by a fragment directly in the sidebar (updates in place, no flash).
    with st.sidebar:
        _sidebar_status_fragment()

    # --- Driver message (pit -> car HUD) ---------------------------------- #
    render_driver_message_panel()

    # --- Overrides & export (used less often, kept lower) ------------------ #
    # Keyed so the fast fragments can read them from session_state.
    st.sidebar.markdown("---")
    st.sidebar.markdown("### :material/tune: Overrides & Rivals")
    st.sidebar.number_input("Manual Lap (-1 for Auto)", min_value=-1, value=-1, step=1,
                            key="manual_lap")
    st.sidebar.number_input("Rival Team Laps", min_value=0, value=0, step=1, key="rival_laps")

    render_cut_lap_panel()

    st.sidebar.markdown("---")
    render_export_panel()

    # --- Danger Zone — kept at the very bottom of the sidebar -------------- #
    if st.session_state.is_racing:
        st.sidebar.markdown("---")
        with st.sidebar.expander(":material/warning: Danger Zone"):
            if st.button(":material/pause: PAUSE RACE", width="stretch"):
                st.session_state.is_racing = False
                _persist_race()
                st.rerun()

    # Always-visible top strip (fault banner + metric tiles), above the tabs.
    # Each fragment below renders directly into its spot and refreshes in place.
    _top_strip_fragment()  # fast (2s)

    tab_driver, tab_history, tab_weather, tab_strategy = st.tabs(
        [":material/speed: Driver Telemetry", ":material/show_chart: History",
         ":material/cloud: Weather", ":material/insights: Strategy"]
    )
    with tab_driver:
        _driver_fragment()  # fast (2s)
    with tab_history:
        # Controls live OUTSIDE the fragments (they are what drives them), so
        # changing one is a full app rerun — which is also what re-registers the
        # chart's refresh timer. Changing the window also drops any freeze,
        # otherwise picking "1 min" while frozen would appear to do nothing.
        hctl1, hctl2, hctl3 = st.columns([5, 2, 1])
        with hctl1:
            st.segmented_control("Time window", list(HISTORY_WINDOWS), key="hist_window",
                                 on_change=lambda: st.session_state.update(hist_freeze=None),
                                 label_visibility="collapsed")
        with hctl2:
            st.segmented_control("Scale", ["Raw units", "Normalize"], key="hist_scale",
                                 label_visibility="collapsed",
                                 help="Normalize scales every metric to 0-100% so "
                                      "shapes can be compared; the tooltip still "
                                      "shows real values.")
        with hctl3:
            # Behind a popover so it costs no space until wanted — but whenever it
            # IS on, the caption under the chart says so, because a filter that
            # quietly edits min/avg/max would be indistinguishable from bad data.
            with st.popover(":material/filter_alt: Value filter", width="stretch"):
                st.caption("Hide readings outside a band. Applies to the chart, "
                           "the stats and both CSV exports.")
                st.selectbox("Metric", [FILTER_OFF] + HISTORY_LABELS,
                             key="hist_filter_metric")
                fc1, fc2 = st.columns(2)
                fc1.number_input("Min", value=None, key="hist_filter_min",
                                 placeholder="none")
                fc2.number_input("Max", value=None, key="hist_filter_max",
                                 placeholder="none")
                # on_click, NOT an `if st.button(...)` body: the widgets above
                # already own these keys this run, and Streamlit refuses writes
                # to an instantiated widget's state. A callback runs at the start
                # of the next rerun, before the widgets exist, so it is allowed.
                st.button("Clear filter", key="hist_filter_clear",
                          width="stretch", on_click=_clear_hist_filter)
        st.pills("Metrics", HISTORY_LABELS, selection_mode="multi",
                 key="hist_metrics", label_visibility="collapsed")

        _history_status_fragment()   # 5s — badge only, ticks even while frozen
        # run_every is applied HERE rather than as a decorator so a freeze can
        # stop the tick outright: a frozen chart is never redrawn, so nothing can
        # shift under the cursor while it is being read. Fragment identity is
        # module + function name + position (not run_every), so swapping the
        # interval like this keeps the very same fragment.
        st.fragment(run_every=None if _hist_frozen_range() else CHART_TICK_S)(
            _render_history_chart)()
        st.markdown("---")
        _history_tables_fragment()   # 30s
        # Reset control — guarded behind a popover + confirm so it can't be hit by
        # accident mid-race. The collector keeps its stream position, so only past
        # data is cleared.
        with st.popover(":material/delete: Reset History"):
            st.warning("Permanently deletes all stored telemetry and fault "
                       "history. This cannot be undone.")
            if st.button("Yes, delete everything", type="primary",
                         key="confirm_reset_history"):
                conn = db.get_conn()
                try:
                    removed = db.clear_history(conn)
                finally:
                    conn.close()
                st.toast(f"Cleared {removed:,} stored sample(s).", icon=":material/delete:")
                st.rerun()
        st.markdown("---")
    with tab_weather:
        _weather_fragment()  # slow (10s)
    with tab_strategy:
        _strategy_fragment()  # slow (10s)


if __name__ == "__main__":
    main()
