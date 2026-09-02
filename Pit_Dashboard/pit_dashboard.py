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
    # One classify() and a Threshold per metric, replacing six loose scalars.
    # The tile code no longer knows what a threshold IS -- it asks limits.
    NORMAL, WARNING, CRITICAL, TIER_COLOURS, classify,
    MOTOR_TEMP, CTRL_TEMP, CELL_TEMP, SOC, POWER,
    PACK_VOLTAGE, BATT_CURRENT, MOTOR_CURRENT, SPEED, CELL_VOLTAGE,
    plausible_cell_temp,
    CELL_COUNT, cell_temp_label,
    THERMISTOR_GROUP_RANGES, THERMISTOR_GROUP_NAMES,
    THERMISTOR_GROUPED_COUNT, THERMISTOR_ID_MAX,
    # Uncoloured on purpose (no warn, no crit) — it is imported anyway so the
    # tile declares a limit like every other numeric tile, and so the gauge
    # scale stays shared with the driver HUD.
    SOLAR_CURRENT,
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
from ui import render_metric, render_sector_display, _age_text, MISSING_TEXT
# Throttle zone boundaries and pedal calibration, shared with the car so the
# pit's zone label matches the bar on the driver's HUD.
#
# MUST stay below the `from constants import ...` above: constants.py is what
# puts the repo root on sys.path (see its header), and efficiency.py lives
# there. Importing it up with `db` and `export` would be an ImportError,
# exactly the invisible ordering dependency constants' own header warns about.
import efficiency

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
# Light-mode tier colours. limits.TIER_COLOURS is the canonical pair and is
# tuned for the driver's near-black cockpit screen; #ff6500 on the pit's white
# background is about 2.9:1 contrast, which is too low to read a number in.
#
# So the TIER and its THRESHOLD are shared -- both screens agree a reading is a
# warning at the same value -- and only the light-mode rendering differs. That
# is a deliberate accessibility concession, not drift: pretending one hex works
# on both backgrounds would trade a real legibility problem for a nominal
# consistency win.
_PIT_WARNING_LIGHT = "#b35400"
_PIT_CRITICAL_LIGHT = "#c62828"


def _tier_css_vars(warning_hex: str, critical_hex: str) -> str:
    """The two tier colours as CSS custom properties.

    Concatenated into the blocks below rather than interpolated with an f-string:
    these blocks are full of literal CSS braces, and doubling every one of them
    to satisfy an f-string is a large edit with a lot of room to be wrong.
    """
    return (f"    --pit-warning: {warning_hex};\n"
            f"    --pit-critical: {critical_hex};\n")


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
""" + _tier_css_vars(TIER_COLOURS[WARNING], TIER_COLOURS[CRITICAL]) + """}
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
""" + _tier_css_vars(_PIT_WARNING_LIGHT, _PIT_CRITICAL_LIGHT) + """}
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


def _val_cf(row, known, key, default=None):
    """Like _val, but falls back to `known` (db.latest_known()'s dict) when the
    newest row's column is null. Returns (value, device_ts) — device_ts is
    None for a live reading straight off `row`, and populated only when the
    value was carried forward, so callers can show its age."""
    v = _val(row, key, None)
    if v is not None:
        return v, None
    fallback = known.get(key)
    return fallback if fallback is not None else (default, None)


@st.cache_resource(show_spinner=False)
def _ensure_schema():
    """Create / migrate the schema — ONCE per server process.

    init_db is not the cheap idempotent call it looks like. It ends with a
    one-time correction of historical rows whose motor power was stored as a
    raw uint16 (see db.init_db), and there is no index on mms_power_W, so that
    UPDATE is a full SCAN of the whole table inside a write transaction.
    Measured on a 96 MB store: 84.5 ms, matching zero rows, every time.

    It was running TWICE per full app rerun — here and again in the export
    panel — which meant every theme toggle and every font-size click paid ~170
    ms of pointless table scanning before drawing anything. As a migration that
    cost is fine; as something a button press triggers it is not.

    cache_resource, not cache_data: there is no value to memoise, we are
    memoising the side effect of having run it.

    The one thing this gives up: if telemetry.db is deleted or swapped out from
    under a running server, the schema is not recreated and Streamlit has to be
    restarted. The in-app path is safe — clear_history is a DELETE, not a file
    drop — so this only bites someone replacing the file by hand mid-session.
    """
    conn = db.get_conn()
    try:
        db.init_db(conn)
    finally:
        conn.close()


# How long one live read is reused. Deliberately SHORTER than the 2s fragment
# tick, so no tile ever shows a sample meaningfully older than an uncached read
# would have given — but long enough that the four 2s fragments, which Streamlit
# runs back-to-back on one script thread within a few hundred ms of each other,
# share ONE connection and ONE query instead of opening four.
#
# Not 2.0: a TTL equal to the tick period aliases against it, and a fragment
# would land on the same cached sample about half the time.
LIVE_CACHE_S = 1.0


@st.cache_data(ttl=LIVE_CACHE_S, show_spinner=False)
def _live_snapshot():
    """The newest stored sample, as (state_dict, device_ts).

    Returns device_ts and NOT age, which is the whole point of the split. age is
    a fact about *now*; device_ts is a fact about the row. Caching an age would
    freeze the "LIVE - 3s ago" badge at whatever it read when the cache filled,
    and — much worse — freeze the `fresh` flag derived from it, which is what
    decides whether the fault banner is marked STALE. A dashboard that cannot
    notice the car went quiet is the one failure this whole file is written to
    avoid.

    Before this existed, each of the four 2s fragments called read_live_state
    independently, so every tick opened three to five fresh SQLite connections
    (each with its own WAL/synchronous/busy_timeout PRAGMAs) and read the same
    row three to five times — and because they read at slightly different
    instants, the top strip and the Live Metrics tab could legitimately display
    different samples. One cached read fixes both.
    """
    conn = db.get_conn()
    try:
        row = db.latest_sample(conn)
        known = db.latest_known(conn)
    finally:
        conn.close()

    # Carry-forward-eligible fields fall back to `known` (last real value, any
    # age) when the newest row is null for them; field_ts records the age of
    # any value that was actually carried forward, keyed by the STATE dict key
    # (not the db column), so read_live_state can hand it straight to a tile.
    # Fault flags, instant-diagnostics (solar_sensor_status, lap_source) and
    # position are deliberately excluded below and keep using plain _val — see
    # the plan's carry-forward scope table for why each is excluded.
    field_ts = {}

    def cf(state_key, column, default=None):
        val, ts = _val_cf(row, known, column, default)
        if ts is not None:
            field_ts[state_key] = ts
        return val

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
        # Throttle pedal. None (not 0) when the car sent no reading — an
        # unreported pedal and a released pedal are different facts, and the
        # whole point of this feature is telling a driver about the pedal.
        "throttle_pct": None, "throttle_mv": None, "throttle_zone": None,
        # Solar charge current, amps. None (not 0) when the car sent nothing —
        # and here the distinction really bites: 0 A is the correct reading for
        # the whole night stint, so a missing value coalesced to 0 would be
        # invisible among thousands of legitimate zeros.
        "solar_current": None, "solar_status": None,
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
        # last_lap_regen_energy: added alongside last_lap_energy — None on a
        # build/history that predates it, same as every other "not reported
        # yet" field here.
        "last_lap_regen_energy": None,
        # Since the last detected charging stop (see lap_tracker.mark_stint_start
        # on the car). None until the car has reported one — a build without
        # charge_detector.py never sends these, and that must read as "unknown"
        # rather than a confident 0 Wh stint.
        "stint_energy": None, "stint_regen_energy": None,
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
    # Note: everything below runs even when row is None (empty telemetry table,
    # e.g. right after a history clear) — _val(None, ...) already returns its
    # default, and _val_cf falls through to `known` the same way, so this is
    # correct with no special-casing. `known` is empty in that same situation
    # too, since clear_history deletes last_known alongside telemetry.
    state["soc"] = cf("soc", "bms_soc_percent")
    state["voltage"] = cf("voltage", "bms_voltage_V")
    state["current"] = cf("current", "bms_current_A")
    # How many cell taps the BMS itself reports configured (0x104). The
    # authoritative gate for the individual cell tiles below — see
    # db.BMS_CELL_COLUMN_COUNT for why the column count (30) is a wiring
    # limit, not a live count, and this is the number that actually is.
    state["bms_string_count"] = cf("bms_string_count", "bms_string_count")
    for _i in range(1, db.BMS_CELL_COLUMN_COUNT + 1):
        _cell_key = f"bms_cell_{_i:02d}_V"
        state[_cell_key] = cf(_cell_key, _cell_key)
    # DS003 — carried forward the same as cell voltage: a real reading, once
    # any thermistor has ever been configured, is exactly the "continuous
    # measurement" case this whole mechanism exists for.
    for _i in range(1, db.THERMISTOR_CELL_COLUMN_COUNT + 1):
        _cell_temp_key = f"bms_cell_temp_{_i:02d}_C"
        state[_cell_temp_key] = cf(_cell_temp_key, _cell_temp_key)
    state["pack_voltage"] = cf("pack_voltage", "mms_measured_voltage_V")
    state["motor_current"] = cf("motor_current", "mms_current_A")
    state["regen_energy"] = cf("regen_energy", "regen_energy")
    state["target_speed_kmh"] = cf("target_speed_kmh", "target_speed_kmh")
    state["soc_ctrl"] = cf("soc_ctrl", "mms_estimated_soc_percent")
    state["trip_m"] = cf("trip_m", "mms_trip_m")
    state["batt_temp"] = cf("batt_temp", "battery_temp_C")
    state["rpm"] = cf("rpm", "mms_rpm")
    state["temp"] = cf("temp", "mms_temperature_C")
    state["power_w"] = cf("power_w", "mms_power_W")
    state["motor_temp"] = cf("motor_temp", "mms_motor_temp_C")
    state["motor_ohms"] = cf("motor_ohms", "mms_motor_ohms")
    state["motor_map"] = cf("motor_map", "mms_motor_map")
    state["motor_map_raw"] = cf("motor_map_raw", "mms_motor_map_raw")
    state["throttle_pct"] = cf("throttle_pct", "mms_throttle_percent")
    state["throttle_mv"] = cf("throttle_mv", "mms_throttle_mv")
    state["solar_current"] = cf("solar_current", "solar_current_A")
    # solar_sensor_status is deliberately NOT carried forward — it explains
    # *why* solar_current is missing right now (night, cloud, unplugged
    # sensor); a stale reason attached to a live reading would mislead.
    state["solar_status"] = _val(row, "solar_sensor_status", None)
    # Prefer the zone the CAR classified (what the driver's bar actually showed).
    # Fall back to classifying the percentage here only for rows written before
    # the column existed, so historical samples still colour rather than reading
    # blank — efficiency.zone() is the same function the car ran. Carrying the
    # raw column forward first means a stale zone still prefers the car's own
    # past classification over re-deriving from a (now also carried-forward)
    # throttle_pct.
    state["throttle_zone"] = (cf("throttle_zone", "mms_throttle_zone")
                              or efficiency.zone(state["throttle_pct"]))
    state["last_lap_energy"] = cf("last_lap_energy", "last_lap_energy")
    state["total_race_energy"] = cf("total_race_energy", "total_race_energy")
    state["last_lap_regen_energy"] = cf("last_lap_regen_energy", "last_lap_regen_energy")
    state["stint_energy"] = cf("stint_energy", "stint_energy")
    state["stint_regen_energy"] = cf("stint_regen_energy", "stint_regen_energy")
    state["last_lap_time_s"] = cf("last_lap_time_s", "last_lap_time_s")
    state["lap_distance_m"] = cf("lap_distance_m", "lap_distance_m")
    # lap_source is deliberately NOT carried forward — it's only meaningful
    # paired with the lap that JUST happened, not as a standing fact.
    state["lap_source"] = _val(row, "lap_source", None)
    auto_lap = cf("auto_lap", "calculated_lap")
    state["auto_lap"] = None if auto_lap is None else int(auto_lap)
    odometer_m = cf("odometer_km", "odometer_m")
    state["odometer_km"] = None if odometer_m is None else odometer_m / 1000.0
    # Position is deliberately NOT carried forward — confidently drawing the
    # car at a stale fix is the highest-confidence WRONG signal this dashboard
    # could produce. NULL lat/lon = the car sent no fix (no GPS, or still
    # searching); keep the Zolder fallback for the map centre only, and
    # remember it isn't a real position.
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
    # (~50x too high). They are wrong here, not merely stale. The live database
    # has been repaired and the one-off script that did it is gone; a pre-fix
    # .bak opened in this dashboard still shows those bad values.
    state["speed_kmh"] = cf("speed_kmh", "mms_vehicle_speed_kmh")
    # Fault flags are deliberately NOT carried forward — a stale "no fault"
    # could mask a fault that started after the last report, and a stale
    # "fault active" would never clear. The page-level "Not live" banner
    # already covers a car that's gone quiet.
    state["bms_has_error"] = _val(row, "bms_has_error", 0)
    state["bms_error_code"] = _val(row, "bms_error_code", 0)
    state["bms_protections"] = _val(row, "bms_protections", "")
    state["mms_has_error"] = _val(row, "mms_has_error", 0)
    state["mms_error_code"] = _val(row, "mms_error_code", 0)
    state["mms_alerts"] = _val(row, "mms_alerts", "")

    state["_field_ts"] = field_ts
    return state, (row["device_ts"] if row is not None else None)


# -- Pit command acknowledgements ----------------------------------------- #
# How long after a pit command we keep asking the car whether it applied it.
#
# An ack answers exactly one question -- "did the car pick it up?" -- and the car
# answers within a second or two or it is not listening. Asking again for the
# remaining twenty-three hours of the race buys nothing and costs a blocking
# HTTPS GET on the very thread that redraws the live tiles: Streamlit runs a
# session's fragment reruns sequentially on one script-run thread, so this was
# never just a slow Strategy tab, it stalled the speed and the fault banner too.
#
# Cut from 120s to 30s. The reasoning above already says the car answers within
# a second or two, so the remaining ninety seconds were pure blocking I/O on the
# render thread for an answer that had either arrived or was never coming. The
# "Check again" button covers the rare case where it is worth asking later.
ACK_POLL_WINDOW_S = 30

# Dedupe within the window. The strategy panel ticks every 10s and the sidebar
# redraws on every app rerun, so without this a single send still means dozens
# of round trips.
ACK_CACHE_S = 5


@st.cache_data(ttl=ACK_CACHE_S, show_spinner=False, max_entries=8)
def _cached_strategy_ack(sent_at):
    """The car's strategy ack. `sent_at` is in the cache key ONLY so that a NEW
    send instantly invalidates the previous send's cached answer -- the same
    trick as _hist_bounds' quantised now_q, used the other way round."""
    import driver_message
    return driver_message.read_strategy_ack()


@st.cache_data(ttl=ACK_CACHE_S, show_spinner=False, max_entries=8)
def _cached_lap_ack(sent_at):
    """The car's cut-lap ack. See _cached_strategy_ack for why sent_at is a key."""
    import driver_message
    return driver_message.read_lap_ack()


def _ack_polling(sent_at, settled):
    """Should we still be asking? Only inside the window, and only until the car
    has actually said something."""
    return settled is None and (time.time() - float(sent_at or 0)) <= ACK_POLL_WINDOW_S


def read_live_state():
    """Latest sample from SQLite + freshness. Returns (state_dict, age_seconds).
    age is None when the store is empty.

    A thin wrapper so every existing call site is unchanged: the SQLite read
    behind it is cached for LIVE_CACHE_S, but the age is recomputed on every
    single call. See _live_snapshot for why that division matters.

    Also stamps state["_field_ages"]: {state_key: age_seconds} for every field
    that was carried forward from last_known (see _live_snapshot's `field_ts`).
    Computed fresh here rather than cached alongside field_ts for the same
    reason `age` itself is — caching an age freezes it for the whole TTL, and
    a carried-forward critical reading is exactly the case where a frozen "3s
    old" caption would be actively misleading 40 minutes later.
    """
    state, device_ts = _live_snapshot()
    now = time.time()
    age = (now - device_ts) if device_ts else None
    state["_field_ages"] = {k: (now - ts) for k, ts in state.get("_field_ts", {}).items()}
    return state, age


# Every history metric we can chart: (df column, label, unit, color). Column
# names match read_history_df() below; colors are distinct and legible on both
# the dark and light themes.
HISTORY_CHARTS = [
    ("Speed",     "Speed",           "km/h", "#00FFCC"),
    # THE THROTTLE TRACE — the pit wall's driver-coaching signal, and the reason
    # it sits immediately under Speed: the two are read together. Speed says
    # what the car did, throttle says what the driver asked for, and the gap
    # between them is where the energy goes. A sawtooth trace at constant speed
    # is a driver pumping the pedal (expensive); a flat trace is the steady
    # highway input a 24-hour race is won with.
    #
    # Shares the "%" axis with Battery SoC, so adding it to the default view
    # costs no third axis — see HISTORY_DEFAULT_METRICS and _hist_axis_plan.
    ("Throttle",  "Throttle",        "%",    "#ff4dd2"),
    # Solar charge current — what the array put into the pack, over time. The
    # strategy trace for a solar race: overlay it on Total Race Energy and the
    # night/day balance of a 24-hour run reads straight off the chart. Amber-gold
    # because it is the sun, and because no other series here uses it.
    ("SolarCurrent", "Solar Current", "A",  "#ffd166"),
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
# Throttle joins them because it is the one the pit wall asked for by name and
# it needs no extra axis (it is a "%" like SoC). Three traces, two units: the
# opening view still fills both axes honestly and still needs no Normalize.
HISTORY_DEFAULT_METRICS = ["Speed", "Throttle", "Battery SoC"]

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
# Now defined in ui.py (imported at the top of this file) so render_metric can
# recognise its own dashes; re-stated here only as documentation of intent.

# Two DIFFERENT numbers that used to be one constant — and conflating them meant
# the cache they were supposed to serve never hit once.
#
# The BUCKET is the step _hist_bounds quantises start_ts to, and start_ts is part
# of read_history_df's cache key. The TTL is how long an entry lives. A TTL
# SHORTER than the tick consuming it is always expired by the time that tick
# arrives; a bucket on a different clock from the tick changes the key between
# ticks as well. Both were true of a single 8s constant against a 10s chart, so
# every tick paid the full read in full — while the comment here claimed it was
# being saved. Measured on the real store, that read was 26s on a 24h window.
#
# Bucket == the tick, so the key changes exactly once per tick and everything
# within one tick shares an entry. TTL > the tick, so the entry outlives the tick
# that created it and the 30s tables fragment lands on a live one.
HISTORY_BUCKET_S = CHART_TICK_S
HISTORY_CACHE_S = CHART_TICK_S + 5

# How many rows the chart asks SQLite for before pandas thins to
# OVERLAY_MAX_POINTS. Deliberately ~9x the 900 actually drawn, not 900 itself:
# at this target every window up to an hour comes back unstrided and identical
# to before, only 3h+ is touched, and the stats cards still read a frame dense
# enough that their min/avg/max are indistinguishable from exact.
HISTORY_SQL_TARGET = 8000

# Exactly the columns the loop in read_history_df reads. The table has 118; the
# other 102 include raw_json, which is ~1.6 kB per row and 61% of the database
# file, and which this path fetched off disk, boxed into Python and threw away
# once per row, every tick. Sixteen columns instead of all of them measured 17x
# faster on 100k rows (26.9s -> 1.5s).
_HIST_COLUMNS = [
    "device_ts", "mms_vehicle_speed_kmh", "mms_throttle_percent",
    "solar_current_A", "mms_power_W", "mms_rpm", "bms_soc_percent",
    "mms_measured_voltage_V", "bms_current_A", "battery_temp_C",
    "mms_motor_temp_C", "mms_temperature_C", "mms_motor_ohms",
    "odometer_m", "calculated_lap", "total_race_energy",
]


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


@st.cache_data(ttl=HISTORY_CACHE_S, show_spinner=False, max_entries=4)
def read_history_df(limit=100000, start_ts=None, stride_target=HISTORY_SQL_TARGET):
    """Samples as a DataFrame for charting (oldest -> newest), one column per
    chartable metric. `start_ts` limits to samples at/after that unix time.

    Returns (df, total, step): the frame, how many samples the range really
    holds, and the stride SQLite applied to get there (1 = every row in range).
    The caller needs those two to caption the chart honestly — a thinned chart
    that says nothing about being thinned is a chart that misreports the data.

    Reads only _HIST_COLUMNS and lets SQLite do the thinning (see
    db.fetch_series). Both matter: this runs every CHART_TICK_S seconds on the
    single thread that also redraws every live tile, so whatever it costs, the
    speed and SoC tiles are frozen for exactly that long.

    `start_ts` and `stride_target` are part of the cache key, so changing the
    window still refetches immediately.
    """
    conn = db.get_conn()
    try:
        rows, total, step = db.fetch_series(
            conn, _HIST_COLUMNS, start_ts=start_ts, limit=limit,
            stride_target=stride_target)
    finally:
        conn.close()
    cols = ["Time"] + [c for c, *_ in HISTORY_CHARTS]
    if not rows:
        return pd.DataFrame(columns=cols), 0, 1

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
            # NaN, never 0, wherever the pedal was not reported — the trace
            # draws an honest gap instead of a phantom lift-off.
            "Throttle": r["mms_throttle_percent"],
            # NaN, never 0, where the sensor reported nothing. A dropout must
            # not be averaged in as a genuine zero when the crew asks what the
            # array averaged over a stint.
            "SolarCurrent": r["solar_current_A"],
            "Power": r["mms_power_W"],
            "RPM": rpm,
            "SoC": r["bms_soc_percent"],
            # The controller's measurement — see the HISTORY_CHARTS note. NO
            # fallback to bms_voltage_V: the two disagree by ~2.25x, so filling
            # gaps from the other source would draw a trace that steps between
            # 50 V and 113 V and looks like a real electrical event. A gap is
            # honest. Rows predating this column read empty; the raw_json each
            # row carries still holds the value, which is how the one-off
            # backfill recovered them before it was retired.
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
    return pd.DataFrame(recs), total, step


def _bms_fault_detail(protections, code):
    """Human-readable BMS fault text: the car's label string if present, else
    the pit-side decode of the raw code, else the bare hex for unknown codes."""
    return (protections or decode_error_bits(code, BMS_PROTECTION_BITS)
            or f"code 0x{int(code or 0):X}")


def _mms_fault_detail(alerts, code):
    """Human-readable MMS fault text — same precedence as the BMS helper."""
    return (alerts or decode_error_bits(code, MMS_ERROR_BITS)
            or f"error 0x{int(code or 0):X}")


# The only columns the episode collapse below reads. Same reasoning as
# _HIST_COLUMNS: this was pulling all 118, raw_json included, for 3000 rows.
_FAULT_COLUMNS = ["device_ts", "bms_has_error", "bms_protections",
                  "bms_error_code", "mms_has_error", "mms_alerts",
                  "mms_error_code"]


# ttl 45 against a 30s fragment. It was 30 against 30, which never hits: the
# entry expires at the same moment the tick that wants it arrives.
@st.cache_data(ttl=45, show_spinner=False)
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
        rows = db.fetch_faults(conn, limit=limit_rows,
                               columns=_FAULT_COLUMNS)
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
    @st.cache_data, so recomputing per fragment is cheap.

    THIS FUNCTION IS DELIBERATELY NOT CACHED, and must not become so. Two
    reasons, either one sufficient:

      * It reads session_state (manual_lap, rival_laps, is_racing) which would
        not be part of any cache key. st.cache_data is process-global, not
        per-session, so two pit laptops would silently show each other's
        overrides — and even in one browser a Manual Lap change would not take
        effect until the TTL expired.
      * It computes the race countdown from time.time(). Caching it stops the
        clock for the TTL, which reads as a stuttering timer on the wall.

    The expensive part — the SQLite read — is cached one level down in
    _live_snapshot, which has neither of those problems."""
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
    ages = state.get("_field_ages", {})
    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    render_metric(c1, "Speed", fmt(state["speed_kmh"], ".1f"), "KM/H",
                  stale_s=ages.get("speed_kmh"))

    # Motor PT1000 — the converted °C is the headline, with the raw Ω beside it
    # so the pit can see the measurement the temperature came from (and tell a
    # genuinely hot motor from a failing sensor).
    motor_temp, motor_ohms = state["motor_temp"], state["motor_ohms"]
    ohms_txt = f" · {motor_ohms:.1f} Ω" if motor_ohms is not None else ""
    if motor_temp is None:
        # No conversion: show the raw Ω if we have one, so a stuck/out-of-range
        # sensor is visible rather than silently absent.
        render_metric(c2, "Motor Temp", "—",
                      ohms_txt.lstrip(" ·") or "no sensor data",
                      stale_s=ages.get("motor_ohms"))
    else:
        motor_cond = classify(motor_temp, MOTOR_TEMP)
        render_metric(c2, "Motor Temp", f"{motor_temp:.1f}",
                      f"°C{ohms_txt}", motor_cond,
                      stale_s=ages.get("motor_temp"))

    # Byte 4 of the same frame — the controller's own temperature. Previously
    # mislabelled "Motor Temp" here, back when the motor had no sensor of its own.
    render_metric(c3, "Controller Temp", fmt(temp), "°C",
                  classify(temp, CTRL_TEMP), stale_s=ages.get("temp"))
    # SoC is inverted — LOW is the dangerous end, which limits.SOC expresses as
    # low_side=True. Unknown stays neutral either way: an absent reading is not
    # evidence of a healthy pack, and not evidence of a flat one either.
    #
    # This used to be a bare `soc < 20 -> critical` with nothing in between, so
    # the pack went from green straight to red. It now has the amber tier the
    # driver HUD also gained.
    render_metric(c4, "Battery SoC", fmt(soc), "%", classify(soc, SOC),
                  stale_s=ages.get("soc"))
    render_metric(c5, "Battery Temp", fmt(batt_temp), "°C",
                  classify(batt_temp, CELL_TEMP), stale_s=ages.get("batt_temp"))
    # Power was the one tile with no tier at all. Regen is negative and so never
    # trips a high-side threshold, which is what we want: recovering energy is
    # not a fault.
    render_metric(c6, "Power Out", fmt(state["power_w"]), "W",
                  classify(state["power_w"], POWER), stale_s=ages.get("power_w"))
    # Lap Dist is c["current_lap_dist_m"], a synthetic figure (position-derived
    # modulo, deliberately fabricated to 0.0 when unknown rather than left
    # null) — not a direct carry-forward field, so no stale_s here.
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
                  f"m:ss{src_note}", stale_s=ages.get("last_lap_time_s"))

    lap_wh = state["last_lap_energy"]
    render_metric(l2, "Last Lap Energy",
                  "—" if lap_wh is None else f"{lap_wh:.1f}", "Wh",
                  stale_s=ages.get("last_lap_energy"))

    total_wh = state["total_race_energy"]
    # One decimal, matching Last Lap Energy and the Excel export — with a whole
    # number you cannot see the total move during a slow lap, or dip at all
    # under regen. Net of regen and motor-side only (excludes controller losses
    # and auxiliaries), so it reads lower than what actually left the pack.
    render_metric(l3, "Total Race Energy",
                  "—" if total_wh is None else f"{total_wh:.1f}", "Wh net",
                  stale_s=ages.get("total_race_energy"))


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
# A dark outline round the dot. On the Carto basemap the cyan fill was legible
# on its own; over satellite imagery it is not, because the ground underneath
# swings from bright concrete to near-black tree cover within one lap.
CAR_DOT_OUTLINE = [8, 12, 18]

# Satellite basemap. Esri's World Imagery, not a Mapbox satellite style: Mapbox
# styles need an access token, and a token the pit crew has to provision is a
# thing that will be missing at 3 a.m. on race night. This needs no key.
#
# The tile URL, its zoom ceiling and its attribution all live in
# static/esri_satellite.json now, NOT here. They were duplicated in both places
# for a while, which is one edit away from a style that points somewhere the
# comment says it does not.
# The satellite view is a BASEMAP STYLE, not a deck.gl layer, and the style has
# to be a URL rather than an inline document. Two dead ends worth recording so
# nobody spends the afternoon on them again:
#
#  1. A deck.gl TileLayer under the car dot renders NOTHING, silently. TileLayer
#     only fetches tiles; turning them into pixels is the job of a
#     `renderSubLayers` callback that wraps each tile in a BitmapLayer, and
#     deck.gl's default callback builds a GeoJsonLayer, which draws nothing at
#     all for a JPEG. That callback is a JavaScript function and pydeck's JSON
#     cannot express one. The layer appears in the generated JSON, deck.gl
#     accepts it, and the map just stays on the old basemap.
#  2. Passing the MapLibre style inline as a dict throws
#     "e.mapStyle?.indexOf is not a function" in the browser: Streamlit's
#     deck.gl component calls .indexOf() on mapStyle, so it must be a string.
#
# So the style lives in static/esri_satellite.json and Streamlit serves it
# (server.enableStaticServing in .streamlit/config.toml). Relative, not
# root-absolute, so it still resolves if the app is ever mounted under a
# baseUrlPath. No API key anywhere: MapLibre has never needed one, and nothing
# in the style points at a mapbox:// URL.
SATELLITE_MAP_STYLE = "app/static/esri_satellite.json"

# ~11 cm of latitude — invisible on the map, but enough to make a view state
# differ from the one already on screen. See the Focus handling below.
FOCUS_NUDGE_DEG = 1e-6


def _car_map_deck(lat, lon, center_lat, center_lon):
    """One dot at the car, view centred wherever the caller says."""
    return pdk.Deck(
        # Satellite imagery instead of the theme-following Carto basemap. The
        # map no longer changes with Light/Dark mode, which is the intended
        # trade: aerial imagery is the same picture in either theme, and a
        # marshal being sent to a corner wants to recognise the corner.
        map_style=SATELLITE_MAP_STYLE,
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
            # See CAR_DOT_OUTLINE: needed for contrast over imagery.
            stroked=True,
            get_line_color=CAR_DOT_OUTLINE,
            line_width_min_pixels=2,
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


# ── Live Metrics tab ──────────────────────────────────────────────────────── #
# Every live reading the car sends, in one place, at the size you can read from
# a folding chair two metres from the screen.
#
# This is the UNION of three sets that had drifted apart: the tile strip above
# the tabs, the gauges on the driver's HUD, and the metrics the History tab can
# chart. Each of those showed a different subset, so "what is the car doing
# right now" meant looking in three places and still missing things -- regen
# energy, trip, target speed and the two currents were on no pit screen at all.
#
# Declared as data rather than a wall of render calls so the set is auditable:
# you can read the whole inventory in one screenful, and _assert_no_duplicates()
# below makes a copy-paste slip an import error instead of a tile shown twice.
#
# Fields:
#   label   what the tile says. Must be unique across ALL groups.
#   unit    shown small beside the number.
#   get     (state, ctx) -> value. A callable so derived readings (delta to
#           target) and ctx-sourced ones (lap, lap distance) fit the same shape.
#   spec    format for fmt(); ignored when text=True.
#   limit   a limits.Threshold to colour against, or None to stay neutral.
#   mag     colour on abs(value). The currents are signed - discharge is
#           negative - so the magnitude is what the threshold means, while the
#           SIGN is information the pit wants to keep seeing.
#   note    a small caveat under the number, for a reading that must never be
#           shown bare.
#   text    value is already a string; skip formatting and colouring.
LIVE_METRICS_PER_ROW = 4


def _relative_regen(regen_wh, total_wh):
    """regen / (regen + total), as a PERCENTAGE (0-100, not a 0-1 fraction) —
    what fraction of gross forward energy at the motor was recovered as regen.

    None whenever either input is missing or the denominator isn't positive,
    rather than 0 or a divide-by-zero: at the start of a stint/lap/race both
    are legitimately 0.0, and "0 % recovered" is a claim about a real number,
    not the honest "nothing to divide yet" this returns instead.
    """
    if regen_wh is None or total_wh is None:
        return None
    denom = regen_wh + total_wh
    if denom <= 0:
        return None
    return regen_wh / denom * 100.0


LIVE_METRIC_GROUPS = [
    ("Motion", [
        dict(label="Speed", unit="km/h", spec=".1f", limit=SPEED,
             field="speed_kmh", get=lambda s, c: s["speed_kmh"]),
        dict(label="Target Speed", unit="km/h", spec=".1f",
             field="target_speed_kmh", get=lambda s, c: s["target_speed_kmh"],
             note="from the active velocity profile"),
        # Deliberately uncoloured. The HUD colours the equivalent readout against
        # a symmetric +/-5 km/h tolerance, which is a band rather than a
        # threshold and so has no limits.Threshold to share. Inventing a
        # one-sided limit here would let the pit and the car disagree about
        # whether the driver is on pace.
        dict(label="Delta to Target", unit="km/h", spec="+.1f",
             get=lambda s, c: (None if s["speed_kmh"] is None
                               or s["target_speed_kmh"] is None
                               else s["speed_kmh"] - s["target_speed_kmh"]),
             note="actual minus target"),
        dict(label="Motor RPM", unit="rpm", spec=".0f",
             field="rpm", get=lambda s, c: s["rpm"]),
    ]),
    ("Motor", [
        dict(label="Motor Power", unit="W", spec=".0f", limit=POWER,
             field="power_w", get=lambda s, c: s["power_w"],
             note="negative = regen"),
        dict(label="Motor Temp", unit="\u00b0C", spec=".1f", limit=MOTOR_TEMP,
             field="motor_temp", get=lambda s, c: s["motor_temp"]),
        # Its own tile here, where the strip above only appends it to the motor
        # temperature. The raw resistance is what separates a genuinely hot
        # motor from a failing PT1000.
        dict(label="Motor Sensor", unit="\u03a9", spec=".1f",
             field="motor_ohms", get=lambda s, c: s["motor_ohms"],
             note="raw PT1000; the temp is derived from this"),
        dict(label="Motor Current", unit="A", spec=".1f", limit=MOTOR_CURRENT,
             mag=True, field="motor_current", get=lambda s, c: s["motor_current"],
             note="amber only; high current is normal"),
        dict(label="Power Map", unit="", text=True,
             field="motor_map", get=lambda s, c: s["motor_map"]),
    ]),
    # ── Driver input ─────────────────────────────────────────────────────── #
    # Its own group rather than an entry under "Motor", because this is the one
    # thing on the page the DRIVER controls directly. Everything else here is
    # the car's response; this is the input, and it is what the pit radios about.
    ("Driver Input", [
        # Deliberately UNCOLOURED (no `limit`). The efficiency zone is not a
        # limits tier: red on this page means a fault is developing, and 90 %
        # throttle on the pit straight is not a fault. The zone travels in the
        # note instead, where it reads as the coaching cue it is. See the
        # docstring in efficiency.py for why the two vocabularies stay apart.
        dict(label="Throttle", unit="%", spec=".0f",
             field="throttle_pct", get=lambda s, c: s["throttle_pct"],
             note="pedal position - see zone below"),
        dict(label="Efficiency Zone", unit="", text=True,
             field="throttle_zone",
             get=lambda s, c: (None if s["throttle_zone"] is None else
                               efficiency.ZONE_LABELS.get(s["throttle_zone"],
                                                          s["throttle_zone"])),
             note="what the driver's HUD bar is showing"),
        # The raw millivolts, which is how the placeholder pedal calibration
        # gets replaced with a measured one: read this with the pedal released,
        # then floored, and put the two numbers in efficiency.py. Kept on the
        # page permanently (not behind a debug flag) for the same reason
        # "Motor Sensor" is - it is what separates a real reading from a dead
        # sensor, and a throttle stuck at "-" with a plausible mV here means
        # the calibration is wrong, not the wiring.
        dict(label="Throttle Raw", unit="mV", spec=".0f",
             field="throttle_mv", get=lambda s, c: s["throttle_mv"],
             note="calibrate efficiency.py from this"),
    ]),
    ("Controller", [
        dict(label="Controller Temp", unit="\u00b0C", spec=".1f", limit=CTRL_TEMP,
             field="temp", get=lambda s, c: s["temp"]),
    ]),
    ("Battery", [
        dict(label="Battery SoC", unit="%", spec=".0f", limit=SOC,
             field="soc", get=lambda s, c: s["soc"], note="BMS coulomb count"),
        dict(label="Pack Voltage", unit="V", spec=".2f", limit=PACK_VOLTAGE,
             field="pack_voltage", get=lambda s, c: s["pack_voltage"],
             note="controller measurement - the one to trust"),
        # Kept beside the controller's figure so the two can be compared at a
        # glance. This decode WAS wrong - it read 2.25x high - and was fixed on
        # 2026-08-20 at 12:12, after which the two agree to within 0.4 %. The
        # tile stays because that is how the fix was confirmed, and how a
        # regression would be spotted.
        #
        # Note the asymmetry this leaves: the LIVE number is trustworthy now,
        # but 82 % of the stored history predates the fix, so the History tab's
        # BMS-voltage trace is still 2.25x high over most of its range.
        dict(label="Pack Voltage (BMS)", unit="V", spec=".2f",
             field="voltage", get=lambda s, c: s["voltage"],
             note="agrees since 2026-08-20 12:12; older history reads 2.25x high"),
        dict(label="Battery Current", unit="A", spec=".1f", limit=BATT_CURRENT,
             mag=True, field="current", get=lambda s, c: s["current"],
             note="negative = discharge"),
        dict(label="Battery Temp", unit="\u00b0C", spec=".1f", limit=CELL_TEMP,
             field="batt_temp", get=lambda s, c: s["batt_temp"],
             note="hottest cell in the pack"),
        # ---- Solar input ------------------------------------------------- #
        # In the Battery group because that is where this current GOES; it is
        # the only inbound number on the page. Uncoloured (limits.SOLAR_CURRENT
        # sets no thresholds) — see the reasoning there: there is no bad value.
        #
        # The sign is shown, not abs()'d: negative means the Yocto-Amp's
        # terminals are reversed, and that has to be visible.
        dict(label="Solar Current", unit="A", spec="+.2f", limit=SOLAR_CURRENT,
             field="solar_current", get=lambda s, c: s["solar_current"],
             note="MPPT into the pack; Yocto-Amp, 10 A max"),
        # The sensor's own health, as text. This is what separates "the array is
        # making nothing" from "the USB cable fell out" — the same blank
        # reading, and a completely different thing to do about it.
        dict(label="Solar Sensor", unit="", text=True,
             get=lambda s, c: s["solar_status"],
             note="online / offline / no_hub / searching"),
        # Also shown despite being useless, for the same reason: it reads 0.00 in
        # all 44,088 recorded samples, so an empty tile here is the evidence that
        # the controller never populates the field.
        dict(label="SoC (controller est.)", unit="%", spec=".0f",
             field="soc_ctrl", get=lambda s, c: s["soc_ctrl"],
             note="unimplemented on this controller - always 0"),
    ]),
    ("Energy", [
        # Whole race, never re-datumed.
        dict(label="Total Race Energy", unit="Wh", spec=".0f",
             field="total_race_energy", get=lambda s, c: s["total_race_energy"],
             note="integrated on the car, net of regen"),
        dict(label="Total Regen Energy", unit="Wh", spec=".0f",
             field="regen_energy", get=lambda s, c: s["regen_energy"],
             note="recovered under braking, whole race"),
        dict(label="Total Relative Regen", unit="%", spec=".1f",
             get=lambda s, c: _relative_regen(s["regen_energy"],
                                              s["total_race_energy"]),
             note="regen / (regen + total), whole race"),
        # Since the last detected charging stop — see
        # SolarRace_OS/modules/charge_detector.py and
        # lap_tracker.mark_stint_start(). Reads the same as the Total tiles
        # above until the car has been through its first charging stop.
        dict(label="Current Stint Energy", unit="Wh", spec=".1f",
             field="stint_energy", get=lambda s, c: s["stint_energy"],
             note="since the last detected charging stop"),
        dict(label="Current Stint Regen Energy", unit="Wh", spec=".1f",
             field="stint_regen_energy", get=lambda s, c: s["stint_regen_energy"],
             note="since the last detected charging stop"),
        dict(label="Current Stint Relative Regen", unit="%", spec=".1f",
             get=lambda s, c: _relative_regen(s["stint_regen_energy"],
                                              s["stint_energy"]),
             note="regen / (regen + total), this stint"),
        # Held for the whole of the FOLLOWING lap — see snapshot()'s docstring
        # in lap_tracker.py for why that is what makes this survive a dropped
        # link instead of needing the two samples either side of a boundary.
        dict(label="Last Lap Energy", unit="Wh", spec=".1f",
             field="last_lap_energy", get=lambda s, c: s["last_lap_energy"]),
        dict(label="Last Lap Regen Energy", unit="Wh", spec=".1f",
             field="last_lap_regen_energy",
             get=lambda s, c: s["last_lap_regen_energy"]),
        dict(label="Last Lap Relative Regen", unit="%", spec=".1f",
             get=lambda s, c: _relative_regen(s["last_lap_regen_energy"],
                                              s["last_lap_energy"]),
             note="regen / (regen + total), last lap"),
    ]),
    ("Lap & Distance", [
        dict(label="Lap", unit="", spec=".0f",
             get=lambda s, c: c["active_lap"]),
        dict(label="Lap Distance", unit="m", spec=".0f",
             get=lambda s, c: c["current_lap_dist_m"]),
        dict(label="Last Lap Time", unit="", text=True,
             field="last_lap_time_s",
             get=lambda s, c: (None if s["last_lap_time_s"] is None
                               else format_lap_time(s["last_lap_time_s"]))),
        dict(label="Odometer", unit="km", spec=".2f",
             field="odometer_km", get=lambda s, c: c["odometer_km"]),
        dict(label="Trip", unit="m", spec=".0f",
             field="trip_m", get=lambda s, c: s["trip_m"],
             note="controller trip counter"),
        dict(label="Lap Source", unit="", text=True,
             get=lambda s, c: s["lap_source"],
             note="what triggered the last lap"),
    ]),
]


def _assert_no_duplicates():
    """A label may appear once across the whole tab.

    The set is a union of three overlapping displays - the strip above the tabs,
    the driver HUD and the History charts - and six metrics were already in all
    three. Catching a repeat at import is the difference between noticing it now
    and someone reading two tiles on race night wondering why they disagree.
    """
    seen = {}
    for group, metrics in LIVE_METRIC_GROUPS:
        for m in metrics:
            label = m["label"]
            if label in seen:
                raise ValueError(
                    f"Live Metrics: {label!r} appears in both {seen[label]!r} "
                    f"and {group!r}")
            seen[label] = group
    return len(seen)


LIVE_METRIC_COUNT = _assert_no_duplicates()

# DS004 of the technical regulations: "Voltage of all battery modules (26
# sensors)". A FIXED compliance screen, so it always renders exactly this many
# tiles — Module 1..26 — with a dash for any not yet reporting, rather than a
# variable-length list that would reshuffle the layout as cells come online.
DS004_MODULE_COUNT = 26


def _cell_value(state, i):
    """One cell's voltage, or None if it isn't a real reading.

    Gated on bms_string_count, not just on the stored value being present:
    a CAN ID can be POLLED (so its frame arrives and decodes to a literal
    0.000 V) without the tap being electrically wired to a real cell — seen on
    this exact hardware in bench captures. Treating that as a genuine 0.000 V
    reading would be indistinguishable from a shorted/dead cell; treating it as
    unreported, like every other missing value on this dashboard, is the
    honest answer. Falls back to the raw value when bms_string_count itself
    hasn't been reported yet (an older build, or no sample at all) — there is
    nothing more authoritative to gate on in that case.
    """
    v = state.get(f"bms_cell_{i:02d}_V")
    if v is None:
        return None
    known = state.get("bms_string_count")
    if known is not None and i > known:
        return None
    return v


def _render_cell_row(cols_n, indices, state, label_fmt):
    ages = state.get("_field_ages", {})
    for row_start in range(0, len(indices), cols_n):
        chunk = indices[row_start:row_start + cols_n]
        cols = st.columns(cols_n)
        # Every column gets an element, trailing unused ones included -- see the
        # ghost-tile note in _live_metrics_fragment for why an empty slot in a
        # 2 s fragment keeps showing the previous render's content.
        for slot, col in enumerate(cols):
            if slot >= len(chunk):
                col.empty()
                continue
            i = chunk[slot]
            v = _cell_value(state, i)
            render_metric(col, label_fmt(i), fmt(v, ".3f"), "V",
                         classify(v, CELL_VOLTAGE),
                         stale_s=ages.get(f"bms_cell_{i:02d}_V"))


def _cell_temp_value(state, i):
    """One cell's temperature, or None if that thermistor has never reported
    or is reporting a broken-sensor value.

    No bms_string_count-style gate like _cell_value's: per the Orion
    Thermistor Expansion Module's own datasheet, a thermistor that hasn't
    been loaded/enabled via the Thermistor Utility is simply never
    transmitted at all — there is no "polled but unwired" wire state here
    the way there is for a BMS voltage tap, so an absent value already means
    exactly "not configured," nothing further to disambiguate.

    What DOES need gating is a thermistor the module has enabled but which is
    failed/disconnected: it reports a nonsense negative (-41 °C on this car's
    cells 14-20) alongside a per-sensor fault bit. The car now drops those at
    the source, but the pit must gate them too — every such reading already
    stored in history, and carried forward through last_known, would
    otherwise keep rendering as a real measurement forever.
    """
    return plausible_cell_temp(state.get(f"bms_cell_temp_{i:02d}_C"))


def _render_cell_temp_row(cols_n, indices, state, label_fmt):
    ages = state.get("_field_ages", {})
    for row_start in range(0, len(indices), cols_n):
        chunk = indices[row_start:row_start + cols_n]
        cols = st.columns(cols_n)
        # Every column gets an element -- see _render_cell_row just above.
        for slot, col in enumerate(cols):
            if slot >= len(chunk):
                col.empty()
                continue
            i = chunk[slot]
            v = _cell_temp_value(state, i)
            render_metric(col, label_fmt(i), fmt(v, ".1f"), "°C",
                         classify(v, CELL_TEMP),
                         stale_s=ages.get(f"bms_cell_temp_{i:02d}_C"))


def ds004_compliance(state):
    """(valid_count, required, missing_indices) for the DS004 screen.

    "Valid" means a reading this dashboard would actually show a scrutineer:
    present, past _cell_value's bms_string_count gate, and non-zero. The
    zero check matters — an unwired tap on this hardware decodes to a literal
    0.000 V, and counting that toward compliance is exactly the fabrication
    this whole screen exists to avoid.
    """
    required = DS004_MODULE_COUNT
    missing = []
    valid = 0
    for i in range(1, required + 1):
        v = _cell_value(state, i)
        if v is None or float(v) <= 0.0:
            missing.append(i)
        else:
            valid += 1
    return valid, required, missing


def _render_ds004_compliance(state):
    """The pass/fail banner for DS004's 26-sensor requirement.

    Deliberately reports the SHORTFALL rather than quietly rendering 26 tiles
    of which half are dashes. The rulebook asks for 26 live module voltages;
    if the car cannot supply them the crew needs to know that at a glance, on
    the screen that claims compliance, not by counting dashes.
    """
    valid, required, missing = ds004_compliance(state)
    if valid >= required:
        st.success(f":material/verified: **DS004 OK — {valid}/{required} "
                  f"module voltages live.**", icon=":material/check_circle:")
        return

    def _runs(nums):
        """Compact 13-26 instead of thirteen separate numbers."""
        out, start, prev = [], None, None
        for n in nums + [None]:
            if start is None:
                start = prev = n
            elif n is not None and n == prev + 1:
                prev = n
            else:
                out.append(str(start) if start == prev else f"{start}-{prev}")
                start = prev = n
        return ", ".join(out)

    st.error(
        f":material/error: **DS004 NOT MET — {valid}/{required} module "
        f"voltages live.** Missing module(s): {_runs(missing)}.\n\n"
        f"The pit is asking the BMS for every cell frame (0x107-0x110, all "
        f"ten are polled); these simply are not being answered. That is a "
        f"BMS configuration / cell-tap wiring matter on the car, not a "
        f"dashboard one — check the pack's configured series count and the "
        f"sense harness.",
        icon=":material/error:")


def render_cell_voltages(state):
    """DS003/DS004 tab body: per-cell temperature and per-cell voltage.

    DS003 reads the Orion Thermistor Expansion Module's per-sensor broadcast
    (0x1838F3xx — see SolarRace_OS/modules/temp_controller_parser.py). That
    module only transmits a thermistor once it has been individually
    loaded/enabled via Orion's own Thermistor Utility software, so every
    bms_cell_temp_NN_C column stays permanently None until that config step
    happens on the car — there is no wire signal that means "not configured
    yet", only the absence of a value ever arriving (see the parser's
    docstring). Below, that shows as a "not configured yet" sign; the section
    switches to the real grid automatically the moment the car sends its
    first genuine reading, with no code change needed here on that day.

    The thermistors are grouped as the pack is actually built — module A then
    module B, 13 cells each — rather than as one flat 1..30 run, so a hot
    reading names a cell someone can physically go and find. The naming and
    the A/B split both come from limits.cell_temp_label, shared with the
    driver HUD so the two screens cannot label the same sensor differently.
    """
    st.markdown("##### :material/thermostat: DS003 — Cell Temperatures")
    configured = any(_cell_temp_value(state, i) is not None
                     for i in range(1, db.THERMISTOR_CELL_COLUMN_COUNT + 1))
    if not configured:
        st.info(":material/info: **Not configured yet.** The Orion "
               "Thermistor Expansion Module hasn't had any sensors "
               "loaded/enabled via its own Thermistor Utility software, so "
               "no per-cell temperature has ever been reported. This will "
               "switch to real readings automatically — with no dashboard "
               "changes needed — the first time the car sends one.")
    else:
        st.caption(f"Colour: warn above {CELL_TEMP.warn:.0f} °C, critical "
                  f"above {CELL_TEMP.crit:.0f} °C.")
        # Iterate the module's REAL id ranges (1-13 and 21-33), not a
        # contiguous run — ids 14-20 are not loaded on this module at all.
        mapped = set()
        for group_name, lo, hi in THERMISTOR_GROUP_RANGES:
            indices = list(range(lo, hi + 1))
            mapped.update(indices)
            st.markdown(f"**Module {group_name}** "
                       f"({cell_temp_label(lo)}–{cell_temp_label(hi)}"
                       f" · sensor ids {lo}-{hi})")
            _render_cell_temp_row(LIVE_METRICS_PER_ROW, indices,
                                  state, cell_temp_label)

        # Anything reporting outside the mapped ranges. Shown only when it
        # actually reports, exactly like DS004's "Additional Cells" below —
        # the house rule is that no data is silently dropped, but an empty
        # section is clutter on a compliance screen.
        extra_t = [i for i in range(1, db.THERMISTOR_CELL_COLUMN_COUNT + 1)
                   if i not in mapped and _cell_temp_value(state, i) is not None]
        if extra_t:
            st.markdown(f"**Unmapped sensors** "
                       f"(ids {', '.join(str(i) for i in extra_t)})")
            st.caption("Reporting from ids outside the pack's two 13-cell "
                      "modules — shown so no reading is lost. If these are "
                      "real cells, extend THERMISTOR_GROUP_RANGES in limits.py.")
            _render_cell_temp_row(LIVE_METRICS_PER_ROW, extra_t,
                                  state, cell_temp_label)

    st.divider()

    known = state.get("bms_string_count")
    if known is not None:
        st.caption(f"BMS reports **{int(known)} cells** wired and configured.")
    else:
        st.caption(":orange[Cell count unknown — the car has not reported "
                  "bms_string_count yet.]")

    st.markdown(f"##### :material/rule: DS004 — Module Voltages "
               f"(1–{DS004_MODULE_COUNT})")
    _render_ds004_compliance(state)
    st.caption("The JBD BMS protocol has no grouping above individual cells "
              "(BMS_CAN_PROTOCOL.docx: \"Cell 1\" .. \"Cell 30\", no separate "
              "module concept), so a monitored cell IS a module here. Colour: "
              f"warn below {CELL_VOLTAGE.warn} V, critical below "
              f"{CELL_VOLTAGE.crit} V.")
    _render_cell_row(LIVE_METRICS_PER_ROW, list(range(1, DS004_MODULE_COUNT + 1)),
                     state, lambda i: f"Module {i}")

    extra = [i for i in range(DS004_MODULE_COUNT + 1, db.BMS_CELL_COLUMN_COUNT + 1)
            if _cell_value(state, i) is not None]
    if extra:
        st.markdown(f"##### Additional Cells ({DS004_MODULE_COUNT + 1}"
                   f"–{db.BMS_CELL_COLUMN_COUNT})")
        st.caption("Wired beyond the rulebook's 26 — shown so no data is "
                  "lost, per the engineering copy of this screen.")
        _render_cell_row(LIVE_METRICS_PER_ROW, extra, state, lambda i: f"Cell {i}")


@st.fragment(run_every=2)
def _cell_voltage_fragment():
    """Cell Voltages tab. Same 2s cadence and shared cached read as the other
    fast tabs — see _live_metrics_fragment's docstring for what is and is not
    cached."""
    ctx = _live_context()
    if not ctx["fresh"]:
        st.warning(f":material/warning: Not live — every reading below is "
                  f"from the last sample received, {_age_text(ctx['age'])} ago.")
    render_cell_voltages(ctx["state"])


def _render_live_metric(col, m, state, ctx):
    """One large tile from a LIVE_METRIC_GROUPS entry."""
    value = m["get"](state, ctx)
    note = m.get("note")
    # Only set on carry-forward-eligible entries (see the `field=` tags in
    # LIVE_METRIC_GROUPS) — a stale/never-reported field naturally has nothing
    # in _field_ages, so stale_s is None and render_metric shows no caption.
    stale_s = state.get("_field_ages", {}).get(m.get("field"))

    if m.get("text"):
        # Strings carry no tier: there is nothing to compare them against.
        render_metric(col, m["label"], value or MISSING_TEXT, m.get("unit", ""),
                      large=True, note=note, stale_s=stale_s)
    else:
        limit = m.get("limit")
        condition = NORMAL
        if limit is not None:
            # Colour on the magnitude where the threshold is about magnitude,
            # but still DISPLAY the signed value below - losing the sign would
            # hide regen entirely.
            judged = abs(value) if (m.get("mag") and value is not None) else value
            condition = classify(judged, limit)
        render_metric(col, m["label"], fmt(value, m.get("spec", ".0f")),
                      m["unit"], condition, large=True, note=note, stale_s=stale_s)


@st.fragment(run_every=2)
def _live_metrics_fragment():
    """Live Metrics tab. 2 s, matching the strip above the tabs.

    Same cadence and the same one cached SQLite read, so this tab costs a little
    markdown rather than another query per tick. The cache is on _live_snapshot,
    NOT on _live_context — this docstring claimed the latter for a long time and
    neither was cached at all, which is how four fragments ended up each opening
    their own connection every two seconds without anyone noticing.
    """
    ctx = _live_context()
    state = ctx["state"]

    if not ctx["fresh"]:
        # Every tile below is from the same stale sample, so say it once here
        # rather than decorating twenty-three tiles. Without this the page looks
        # identical to a live one, which is the worst way to read old numbers.
        st.warning(f":material/warning: Not live \u2014 every reading below is from "
                   f"the last sample received, {_age_text(ctx['age'])} ago.")

    for group, metrics in LIVE_METRIC_GROUPS:
        st.markdown(f"##### {group}")
        for i in range(0, len(metrics), LIVE_METRICS_PER_ROW):
            chunk = metrics[i:i + LIVE_METRICS_PER_ROW]
            # Always a full-width row of columns even when the group does not
            # fill it, so tiles line up down the page instead of stretching to
            # different widths per group.
            cols = st.columns(LIVE_METRICS_PER_ROW)
            # EVERY column gets an element, including the trailing unused ones.
            # This used to be `zip(cols, chunk)`, which simply left them empty --
            # and on a fragment that reruns every 2 s, Streamlit matches elements
            # by position, so an empty slot kept displaying whatever the previous
            # RENDER had drawn in it. The visible symptom was ghost tiles: the
            # Energy group's 9th metric sat beside stale copies of the row above
            # it, and Lap & Distance repeated "Last Lap Time"/"Odometer" under
            # Trip. Writing an explicit empty placeholder keeps the element tree
            # the same shape on every rerun, so nothing is left to inherit.
            for slot, col in enumerate(cols):
                if slot < len(chunk):
                    _render_live_metric(col, chunk[slot], state, ctx)
                else:
                    col.empty()
        # The one write control on this whole read-only page, directly under the
        # group holding the Trip tile. NOT inside the tile loop above: this
        # panel renders a VARIABLE number of widgets (it grows a caption and a
        # "Check again" button once a command has been sent), and a grid cell
        # whose element count changes between reruns is exactly what desynced
        # the element tree in the first place.
        if group == "Lap & Distance":
            render_trip_reset_panel()

    st.caption(
        f"{LIVE_METRIC_COUNT} metrics \u00b7 refreshing every 2 s \u00b7 "
        f"\u2014 means the car has not reported that field. Thresholds and "
        f"colours come from limits.py, shared with the driver HUD; the "
        f"efficiency zones come from efficiency.py, likewise shared \u2014 and "
        f"both its pedal calibration and its zone boundaries are still "
        f"placeholders.")


@st.fragment(run_every=2)
def _driver_fragment():
    """Driver Telemetry tab — track position + live GPS map. Updates in place."""
    c = _live_context()
    if not c["fresh"]:
        # Position (lat/lon/has_gps) is deliberately NOT carried forward — see
        # _live_snapshot — so unlike the Live Metrics / Cell Voltages tabs,
        # which can show an old-but-labelled number, this tab would otherwise
        # keep drawing the car at its last real fix with no indication it's
        # stopped moving. This is the only warning this tab gets.
        st.warning(f":material/warning: Not live — track position and the GPS "
                  f"map are from the last sample received, "
                  f"{_age_text(c['age'])} ago." if c['age'] is not None else
                  ":material/warning: Not live — no telemetry received yet.")
    st.markdown("### :material/sports_score: Track Position")
    st.markdown(render_sector_display(c["track_status"], c["current_lap_dist_m"],
                                      c["current_sector_id"]), unsafe_allow_html=True)

    st.markdown("### :material/timer: Sector Times")
    lap, splits, deltas = read_sector_times()
    render_sector_times(lap, splits, deltas)
    st.markdown("### :material/map: Live GPS Map")
    render_gps_map(c["state"])

    # There is ONE map on this tab, deliberately. The Plotly vector circuit map
    # used to sit here as well, on the theory that imagery answers "which corner
    # is that" and a plan view answers "where are we in the lap". In practice the
    # second map earned none of the space: the sector strip at the top of this
    # tab already gives sector and lap distance as text, and the satellite map's
    # own caption already states plainly whether the dot is a real fix or the
    # default Zolder centre. Two maps of the same car, one of them ugly, reads
    # worse than one map that is good.
    #
    # track_map_view.py and its demo app have since been deleted outright, so
    # this is not a chart that is merely switched off — restoring it means
    # recovering the module from git history. What went with it is the HELD vs
    # SIMULATED distinction ("last Zolder fix, 12s old" / "placed from lap
    # distance, 3,000 km away"). If that ever needs to be visible again, put it
    # in the GPS map's caption above; it was one sentence carried by a whole
    # second chart.


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


# ttl 25 against the 30s tables fragment (was 10, which never hit). Not 45:
# this is also read on a full app rerun, and a lap change lagging by three
# quarters of a minute would be its own bug.
@st.cache_data(ttl=25, show_spinner=False)
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
    # Quantise to whole HISTORY_BUCKET_S steps: start_ts is part of
    # read_history_df's cache key, so a raw time.time() would mint a brand-new
    # key every tick and the cache would never hit once. The bucket is the CHART
    # TICK, not the cache TTL — see the constants. Only the left edge of the
    # window moves in steps; end_ts stays None, so every read still runs through
    # to the newest row and the live end of the chart is never stale.
    now_q = int(time.time() // HISTORY_BUCKET_S) * HISTORY_BUCKET_S
    return now_q - window_min * 60, None


def _hist_frame():
    """(DataFrame, start_ts, end_ts, total, step) for the current range.

    `total` and `step` describe what SQLite did: how many samples the range
    holds and the stride taken through them. Charts may draw a thinned frame,
    but nothing downstream is allowed to *claim* it is the whole range.
    """
    start_ts, end_ts = _hist_bounds()
    df, total, step = read_history_df(start_ts=start_ts)
    if end_ts is not None and not df.empty:
        df = df[df["Time"] <= datetime.fromtimestamp(end_ts)]
    return df, start_ts, end_ts, total, step


def _hist_selected_charts():
    labels = st.session_state.get("hist_metrics")
    if not labels:
        return []
    return [c for c in HISTORY_CHARTS if c[1] in labels]


# ttl 6 against the 5s status fragment (was 4, i.e. always expired on arrival).
@st.cache_data(ttl=6, show_spinner=False)
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


def _hist_rangebreaks(plot_df, factor=8.0, max_breaks=60):
    """Spans containing no samples, for Plotly to leave OUT of the x axis.

    Why this exists: the store spans months but the car only runs in short
    sessions, so on a wide window ("All" = 22 Jun -> 20 Aug here) more than 99 %
    of the axis is time when nothing was recorded. Every real burst then
    compresses into a slice a pixel or two wide, and a metric that swings hard
    inside a burst - RPM going -2,152 to 6,352 - renders as a bare vertical
    line. The chart was technically correct and completely unreadable.

    Handing those spans to xaxis.rangebreaks removes them from the axis, so the
    sessions expand to fill the width and the shape of each one becomes visible.
    The ticks stay real dates; only the empty stretches between them go.

    Deliberately NOT a fix for the line-bridging problem - _break_time_gaps
    already handles that, and the two work together: the break keeps a session
    boundary from being drawn as a diagonal, this stops the boundary being three
    weeks wide.
    """
    # Filler rows from _break_time_gaps are all-NaN by construction; they mark
    # gaps rather than data, so a gap must not be measured from one.
    real = plot_df.dropna(how="all")
    if len(real) < 3:
        return []
    deltas = real.index.to_series().diff()
    typical = deltas.median()
    if pd.isna(typical) or typical <= pd.Timedelta(0):
        return []
    threshold = typical * factor
    big = deltas[deltas > threshold]
    if big.empty:
        return []

    # Widest gaps first, so a cap keeps the ones actually distorting the axis.
    breaks = []
    for end_ts in big.sort_values(ascending=False).index[:max_breaks]:
        gap = deltas.loc[end_ts]
        start_ts = end_ts - gap
        # Leave a sliver of real gap at each end. Without it the two sessions
        # butt together and read as continuous telemetry, which is the very
        # illusion _break_time_gaps exists to prevent. 2 % of the gap, capped so
        # a month-long break does not leave a visible week.
        pad = min(gap * 0.02, typical * 3)
        lo, hi = start_ts + pad, end_ts - pad
        if hi <= lo:
            continue
        breaks.append(dict(bounds=[lo.isoformat(), hi.isoformat()]))
    return breaks


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
        # rangebreaks: drop the stretches where nothing was recorded. See
        # _hist_rangebreaks - without it a two-month window is 99 % dead air and
        # every session is a one-pixel spike.
        xaxis=dict(gridcolor=grid, zeroline=False, showspikes=True,
                   spikemode="across", spikesnap="cursor", spikethickness=1,
                   spikedash="dot", spikecolor=fg, automargin=True,
                   rangebreaks=_hist_rangebreaks(plot_df)),
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


def _hist_full_frame(start_ts, end_ts):
    """Every sample in the range, unthinned. For the EXPORT only.

    The chart's frame may have been strided in SQL, so writing the file from it
    would silently drop rows from an export that promises "uses all". This is
    the same read with the stride switched off, and it only ever runs from a
    download click — never on a refresh tick.
    """
    df, _total, _step = read_history_df(start_ts=start_ts, stride_target=None)
    if end_ts is not None and not df.empty:
        df = df[df["Time"] <= datetime.fromtimestamp(end_ts)]
    return df


def _render_hist_export(df, charts, start_ts=None, end_ts=None, total=None):
    """CSV for exactly the range the caption above names.

    `df` is only used for the file name and the row count shown on the button;
    the bytes come from _hist_full_frame so the file holds every sample in the
    range even when the chart above it was drawn from a stride. `data=` takes a
    callable, which Streamlit runs on click — so neither the read nor the CSV is
    generated on the refresh tick."""
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
    # What the FILE will contain (every sample in range), not what the chart
    # was drawn from -- the button promises a row count and then writes it.
    rows = len(df) if total is None else total
    for col, style, label, tip in (
        (c2, "data", "CSV (data)", "Clean table — opens straight into Excel or Sheets."),
        (c3, "report", "CSV (report)", "Same table behind a documented header block."),
    ):
        col.download_button(
            f":material/table_view: {label} · {rows:,}",
            # A closure, not bytes: Streamlit only calls it if the user actually
            # clicks. Must stay pure pandas/csv — st.* calls inside are ignored.
            data=lambda s=style: export.history_csv_bytes(
                _hist_full_frame(start_ts, end_ts), charts, style=s,
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
    df, start_ts, end_ts, hist_total, hist_step = _hist_frame()
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
    # `hist_total` is what the RANGE holds, which is not what the chart drew:
    # SQLite may have strided (db.fetch_series) and pandas may have thinned again
    # (_downsample). Both are legitimate; quietly reporting the drawn count as
    # the sample count would not be.
    note = (f"Range **{_hist_span_text(df['Time'].min(), df['Time'].max())}** · "
            f"{hist_total:,} samples")
    if drawn != hist_total:
        note += f" · chart drawing {drawn:,} of them"
        # Name the SQL stride explicitly when there is one. "1 in 13" tells an
        # engineer looking at a 24h trace that a one-sample spike could be
        # between the points -- which is exactly what they need to know before
        # concluding the trace is clean.
        if hist_step > 1:
            note += f" (1 in {hist_step})"
        note += " (exports use all)"
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
    _render_hist_export(df, charts, start_ts, end_ts, hist_total)


def _hist_recent_rows(n=200):
    """The newest `n` samples in the selected range, never strided.

    Deliberately separate from the chart's frame: see the note at the call site.

    NOT cached, on purpose: it reads the selected window from session_state,
    which st.cache_data cannot see, so a cached copy would keep serving the old
    window's rows after someone changed it. read_history_df underneath is
    cached on start_ts, so this is a dictionary lookup in the common case.
    """
    start_ts, end_ts = _hist_bounds()
    df, _total, _step = read_history_df(start_ts=start_ts, limit=n,
                                        stride_target=None)
    if end_ts is not None and not df.empty:
        df = df[df["Time"] <= datetime.fromtimestamp(end_ts)]
    return df.tail(n)


@st.fragment(run_every=TABLE_TICK_S)
def _history_tables_fragment():
    """Recent samples, per-lap charts and fault history.

    Split off the chart's cadence and slowed to 30s: these were most of the cost
    of the old 10s tick and none of them need to be live while you read a chart.
    (An st.expander still executes its body — it tidies the page, it does not
    defer the work. The saving here comes from the slower tick plus caching.)"""
    with st.expander(":material/history: Recent Samples"):
        # Its own read, NOT the chart's frame. That frame may have been strided
        # in SQL on a wide window, and "the last 200 rows of a 1-in-13 sample"
        # is not what a table called Recent Samples is claiming to be. 200 rows
        # unstrided is cheap at any window width.
        df = _hist_recent_rows()
        if df.empty:
            st.caption("Nothing in this range.")
        else:
            st.dataframe(df.round(2), width="stretch", hide_index=True)

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
            # A new send is a new question: restart the polling window and drop
            # the previous answer, so a stale "confirmed" cannot survive it.
            st.session_state.strategy_sent_at = time.time()
            st.session_state.strategy_ack = None
            st.toast(f"Sent {chosen['label']} to the car",
                     icon=":material/check_circle:")
        except Exception as e:
            st.toast("Strategy send failed", icon=":material/error:")
            st.error(f"Strategy send failed: {e}")

    sent = st.session_state.get("strategy_sent")
    if sent:
        sent_at = st.session_state.get("strategy_sent_at", 0.0)
        ack = st.session_state.get("strategy_ack")
        polling = _ack_polling(sent_at, ack)
        if polling:
            ack = _cached_strategy_ack(sent_at)
            # Once the car has said anything at all, settle it in session_state
            # so the answer stays on screen without the node being read again.
            if isinstance(ack, dict) and (ack.get("applied") or ack.get("note")):
                st.session_state.strategy_ack = ack

        if isinstance(ack, dict) and ack.get("applied") \
                and ack.get("strategy") == sent[0]:
            st.success(f"Car confirmed **{ack.get('strategy')}** · sent {sent[1]}",
                       icon=":material/check_circle:")
        elif isinstance(ack, dict) and ack.get("note"):
            # The car rejected it — usually a profile it does not have, which
            # means the generated CSVs have not been deployed to the Pi.
            st.warning(f"Car reported: {ack['note']}", icon=":material/warning:")
        elif polling:
            st.info(f"Sent {sent[0]} at {sent[1]} — awaiting the car's "
                    f"confirmation.", icon=":material/schedule:")
        else:
            # Carefully worded: we stopped ASKING, which is not the same as the
            # command having failed. It may well have landed.
            st.warning(f"Sent {sent[0]} at {sent[1]} — no confirmation within "
                       f"{ACK_POLL_WINDOW_S}s. The command may still have "
                       f"landed; the pit stopped asking.",
                       icon=":material/schedule:")
            st.button("Check again", key="strategy_ack_recheck",
                      on_click=lambda: st.session_state.update(
                          strategy_sent_at=time.time(), strategy_ack=None))


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
            st.session_state.cut_lap_at = time.time()   # restart the ack window
            st.session_state.cut_lap_ack = None
            st.toast("Cut Lap sent to the car", icon=":material/check_circle:")
        except Exception as e:
            st.toast("Cut Lap failed", icon=":material/error:")
            st.sidebar.error(f"Cut Lap failed: {e}")

    sent = st.session_state.get("cut_lap_last")
    if sent:
        # Show the car's acknowledgement when it comes back, so the engineer
        # knows the command actually landed rather than just left the pit.
        # Bounded: this panel is drawn from main(), so an unbounded read here
        # fired a blocking GET on every single full app rerun, forever.
        sent_at = st.session_state.get("cut_lap_at", 0.0)
        ack = st.session_state.get("cut_lap_ack")
        polling = _ack_polling(sent_at, ack)
        if polling:
            ack = _cached_lap_ack(sent_at)
            if isinstance(ack, dict) and ack.get("applied"):
                st.session_state.cut_lap_ack = ack

        if isinstance(ack, dict) and ack.get("applied"):
            st.sidebar.caption(
                f":material/check_circle: Car confirmed — now on lap "
                f"**{ack.get('lap')}** · sent {sent}")
        elif polling:
            st.sidebar.caption(f":material/schedule: Cut Lap sent {sent} — "
                               "awaiting the car's confirmation.")
        else:
            st.sidebar.caption(f":material/schedule: Cut Lap sent {sent} — no "
                               f"confirmation within {ACK_POLL_WINDOW_S}s. It "
                               "may still have landed.")
            st.sidebar.button("Check again", key="cut_lap_ack_recheck",
                              on_click=lambda: st.session_state.update(
                                  cut_lap_at=time.time(), cut_lap_ack=None))


def render_trip_reset_panel():
    """Ask the CAR to zero its own tracked distance total (state["trip_m"] /
    lap_tracker.odometer_m). Called from _live_metrics_fragment immediately
    after the "Lap & Distance" group, so it sits directly under the Trip tile
    on the page rather than in the sidebar like Cut Lap -- "under the Trip
    tile" is where this control was asked for. Uses bare st.* calls (not
    st.sidebar.*) for that reason.

    Deliberately rendered BETWEEN groups rather than inside a grid cell: this
    panel grows extra widgets once a command has been sent (an ack caption,
    and a "Check again" button), and a column whose element count changes
    between 2 s reruns is what desynced the fragment's element tree and left
    ghost tiles in the half-empty rows.

    Distinct from Cut Lap: doesn't touch lap counting or energy. Also does NOT
    reset the controller's own hardware TRIP register (0x620) -- there is no
    documented CAN command for that; see lap_tracker.reset_trip()'s docstring.
    This only re-datums the car's OWN running total, the same way
    reset_energy (car-side plumbing only, no pit button yet) only re-datums
    energy.

    Shares /lap_command + /lap_command_ack with Cut Lap/Set Lap/Reset Energy
    (see driver_message.send_trip_reset(), main.py's _apply_lap_commands), so
    the ack read below filters on action == "reset_trip" -- otherwise a Cut
    Lap ack landing on the same node in between could be mistaken for this
    command's own confirmation.
    """
    import driver_message  # lazy import: keep FB-write deps out of app startup

    st.caption("Zeroes the car's own tracked Trip/Odometer total. Does not "
              "touch lap count or energy.")
    if st.button(":material/restart_alt: RESET TRIP", width="stretch",
                key="trip_reset_btn"):
        try:
            driver_message.send_trip_reset()
            st.session_state.trip_reset_last = time.strftime("%H:%M:%S")
            st.session_state.trip_reset_at = time.time()   # restart the ack window
            st.session_state.trip_reset_ack = None
            st.toast("Trip Reset sent to the car", icon=":material/check_circle:")
        except Exception as e:
            st.toast("Trip Reset failed", icon=":material/error:")
            st.error(f"Trip Reset failed: {e}")

    sent = st.session_state.get("trip_reset_last")
    if sent:
        sent_at = st.session_state.get("trip_reset_at", 0.0)
        ack = st.session_state.get("trip_reset_ack")
        polling = _ack_polling(sent_at, ack)
        if polling:
            candidate = _cached_lap_ack(sent_at)
            if (isinstance(candidate, dict) and candidate.get("applied")
                    and candidate.get("action") == "reset_trip"):
                ack = candidate
                st.session_state.trip_reset_ack = ack

        if isinstance(ack, dict) and ack.get("applied"):
            st.caption(f":material/check_circle: Car confirmed — trip reset "
                      f"· sent {sent}")
        elif polling:
            st.caption(f":material/schedule: Sent {sent} — awaiting the "
                      "car's confirmation.")
        else:
            st.caption(f":material/schedule: Sent {sent} — no confirmation "
                      f"within {ACK_POLL_WINDOW_S}s. It may still have landed.")
            st.button("Check again", key="trip_reset_ack_recheck",
                      on_click=lambda: st.session_state.update(
                          trip_reset_at=time.time(), trip_reset_ack=None))


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
@st.cache_data(ttl=30, show_spinner=False)
def _export_bounds():
    """(lo_ts, hi_ts, total) for the export panel's caption and date defaults.

    Cached because render_export_panel is called from main(), so this ran on
    every FULL APP RERUN — and a rerun here is a theme toggle, a font-size
    click, a freeze, or any race-control button. Three queries against a 96 MB
    store to redraw a sidebar caption nobody asked to refresh.

    30 seconds stale is invisible on a caption, and it cannot disturb a range
    the engineer has already picked: the date/time widgets below own their
    values by key (exp_sd/exp_st/exp_ed/exp_et), so once instantiated their
    `value=` default is ignored.

    This used to call db.init_db too. main() runs it before any of this, so it
    was pure duplication — and init_db is far from free (see _ensure_schema).
    """
    conn = db.get_conn()
    try:
        lo, hi = db.time_bounds(conn)
        return lo, hi, db.count_samples(conn)
    finally:
        conn.close()


def render_export_panel():
    st.sidebar.markdown("### :material/download: Export Telemetry (Excel)")

    lo, hi, total = _export_bounds()

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
    _ensure_schema()
    if "race_loaded" not in st.session_state:
        conn = db.get_conn()
        try:
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

    # LAZY TABS, and this is the single most important line on the page.
    #
    # By default st.tabs executes EVERY tab body on every run, and Streamlit
    # serialises all of a session's fragment reruns onto one script thread. So
    # the History chart's 10s tick ran while you were looking at Driver
    # Telemetry, and for however long it took, none of the 2s fragments could
    # run at all. That is why the whole page moved at 7-10s while the car was
    # publishing every 0.6s: the tiles were not slow, they were queued behind a
    # chart on a tab nobody was looking at.
    #
    # on_change="rerun" + the .open property (streamlit >= 1.58) makes only the
    # visible tab's body execute. A fragment that stops being rendered stops
    # ticking, cleanly -- the runtime drops it on the same rerun that hides it.
    #
    # The cost is that switching tabs is now a server round trip rather than a
    # client-side toggle. main() is cheap (schema is cache_resource, bounds are
    # ttl-cached), so that is ~100-200ms, and it buys the live tiles a thread
    # that is idle the rest of the time.
    tab_driver, tab_live, tab_cells, tab_history, tab_weather, tab_strategy = st.tabs(
        [":material/speed: Driver Telemetry", ":material/dashboard: Live Metrics",
         ":material/battery_full: Cell Voltages",
         ":material/show_chart: History",
         ":material/cloud: Weather", ":material/insights: Strategy"],
        key="active_tab", on_change="rerun",
    )
    with tab_driver:
        if tab_driver.open:
            _driver_fragment()  # fast (2s)
    with tab_live:
        if tab_live.open:
            _live_metrics_fragment()  # fast (2s)
    with tab_cells:
        if tab_cells.open:
            _cell_voltage_fragment()  # fast (2s)
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

        # EVERYTHING ABOVE THIS LINE STAYS OUTSIDE THE GUARD, and it has to.
        # Streamlit garbage-collects the state of any widget that was not
        # rendered during a run, so gating the window / metrics / filter
        # controls would silently reset the engineer's chosen range and traces
        # every time they glanced at another tab. Rendering a few widgets into
        # a closed tab costs a handful of deltas; re-picking your metrics after
        # every tab switch costs the crew's patience mid-race.
        #
        # What IS gated is the expensive part: the reads and the Plotly figure.
        if tab_history.open:
            _history_status_fragment()   # 5s — badge only, ticks even while frozen
            # run_every is applied HERE rather than as a decorator so a freeze
            # can stop the tick outright: a frozen chart is never redrawn, so
            # nothing can shift under the cursor while it is being read.
            # Fragment identity is module + function name + position (not
            # run_every), so swapping the interval like this keeps the very
            # same fragment.
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
        if tab_weather.open:
            _weather_fragment()  # slow (10s)
    with tab_strategy:
        # Known and accepted side effect of gating this one: the strategy
        # SELECTBOX is inside the fragment, so leaving the tab resets the
        # picker to the default. It is a picker, not committed state -- what
        # the car is actually running is shown separately by the ack.
        if tab_strategy.open:
            _strategy_fragment()  # slow (10s)


if __name__ == "__main__":
    main()
