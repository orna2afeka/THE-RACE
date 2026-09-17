# Pit_Web/api.py — FastAPI backend for the React pit dashboard.
#
# Boundaries, all load-bearing:
#
#   * NO new SQL. Every read goes through Pit_Dashboard/db.py's helpers.
#   * Reads open the store mode=ro, so a backend pointed at the live database
#     during a race cannot corrupt it. The few WRITE endpoints open their own
#     read-write connection, explicitly and only for that call.
#   * NO physics in JavaScript. Everything derived — speed, target speed, track
#     position, strategy — is computed here from drivetrain.py / track.py /
#     speed_profile.py / limits.py and served as a finished value.
#   * A missing reading is None -> JSON null. Never 0, never coalesced.
#
# It imports strategy_engine and weather_service rather than re-implementing
# them. Both came from the earlier Streamlit dashboard; their @st.cache_data
# decorators are now Pit_Dashboard/memo.py, so nothing here pulls in Streamlit.
# The alternative, forking the strategy maths into a second implementation, is
# the one thing the brief forbids outright.
#
# Run from the repo root:
#     python -m uvicorn Pit_Web.api:app --host 0.0.0.0 --port 8000

import asyncio
import io
import os
import sqlite3
import sys
import threading
import time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone

from fastapi import (FastAPI, HTTPException, Query, WebSocket,
                     WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PIT = os.path.join(_ROOT, "Pit_Dashboard")
for _p in (_ROOT, _PIT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import db                                              # noqa: E402
import export                                          # noqa: E402
import limits                                          # noqa: E402
import live_metrics                                    # noqa: E402
from metrics import HISTORY_CHARTS, value_from_row     # noqa: E402
from pit_config import SQLITE_PATH, export_local, export_zone   # noqa: E402
# The state helpers and the stint rule are the web backend's own, so they live
# in Pit_Web/store.py rather than in the shared Pit_Dashboard/ modules.
from .store import (                                   # noqa: E402
    save_app_state, load_app_state,
    DRIVER_STINT_LIMIT_S, DRIVER_STINT_WARN_S, DRIVER_STINT_CRIT_S,
    DRIVER_STINT_UNDO_S, RACE_UNDO_S,
)
import constants as C                                  # noqa: E402
import efficiency                                      # noqa: E402
import strategy_engine                                 # noqa: E402
from strategy_engine import (                          # noqa: E402
    calculate_all_strategies, load_velocity_profile,
    get_live_track_status, profile_to_df, SECTIONS_INFO,
)
import speed_profile                                   # noqa: E402
import cell_extremes                                   # noqa: E402
from memo import memo                                  # noqa: E402

DB_PATH = os.environ.get("SOLARRACE_DB_PATH") or SQLITE_PATH

# REPLAY MODE — for developing and testing without a car or a collector.
#
# The dev snapshot is static, so /ws/history has nothing newer than the cursor
# to send and the append path never runs. With replay on, the initial history
# window stops short of the newest rows and the socket walks forward through
# them on a wall clock. These are REAL samples with their real gaps and real
# nulls; only the clock is replayed.
#
# Off by default, so pointing this at a live store during a race does the
# obvious thing. Set SOLARRACE_REPLAY=1 to develop against a snapshot.
REPLAY = os.environ.get("SOLARRACE_REPLAY", "0") == "1"
REPLAY_HELD_BACK = int(os.environ.get("SOLARRACE_REPLAY_HOLDBACK", "600"))
REPLAY_BATCH = int(os.environ.get("SOLARRACE_REPLAY_BATCH", "5"))
VELOCITY_PROFILE_PATH = os.path.join(_PIT, "210s.xlsx")
DEVICE_ID = db.DEVICE_ID
MISSING = None          # JSON null. The frontend renders it as an em dash.


def ro_conn() -> sqlite3.Connection:
    """Read-only connection carrying db.py's row factory.

    Deliberately NOT db.get_conn(): that issues PRAGMA journal_mode=WAL, which
    is a write and fails on a mode=ro handle. db.py's helpers only SELECT
    through whatever connection they are handed, so this keeps "no new SQL"
    true while making a write physically impossible.
    """
    conn = sqlite3.connect("file:{}?mode=ro".format(DB_PATH), uri=True,
                           timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def rw_conn() -> sqlite3.Connection:
    """Read-WRITE connection. Only the handful of write endpoints call this,
    and each one opens it for the duration of that call alone."""
    return db.get_conn(DB_PATH)


# --------------------------------------------------------------------------- #
# Heavy-read cache
# --------------------------------------------------------------------------- #
# The History reads are the expensive ones: an "All" window over 124k samples
# costs ~5.5 s, almost all of it materialising rows out of SQLite. Sync FastAPI
# endpoints run in a threadpool but the row construction holds the GIL, so three
# viewers asking at once took 21 s — they serialise. On a pit LAN with a phone,
# a tablet and two laptops each polling the heavy tier, that compounds.
#
# So heavy reads are memoised for slightly less than the 10 s heavy-tier poll,
# which is exactly what read_history_df() did in the earlier Streamlit app with
# @st.cache_data(ttl=8). Every viewer within the window shares one read, and the
# TTL is under the poll interval so nobody ever waits an extra cycle to see a
# sample that has landed.
HEAVY_CACHE_TTL_S = float(os.environ.get("SOLARRACE_CACHE_TTL", "8.0"))
_cache: dict = {}
_cache_lock = threading.Lock()


def cached(key, build, ttl=HEAVY_CACHE_TTL_S):
    """Memoise `build()` under `key` for `ttl` seconds.

    The lock guards the dict, NOT the build: two viewers racing on a cold key
    both compute, which is the same cost as today and avoids holding a lock
    across a multi-second read.
    """
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = build()
    with _cache_lock:
        # Drop expired entries rather than growing forever; the keyspace is
        # small (a handful of window/metric combinations).
        for k in [k for k, (exp, _) in _cache.items() if exp <= now]:
            _cache.pop(k, None)
        _cache[key] = (now + ttl, value)
    return value


# --------------------------------------------------------------------------- #
# Live state — the latest sample, with carry-forward
# --------------------------------------------------------------------------- #
def _val(row, key, default=None):
    if row is None:
        return default
    try:
        v = row[key]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


# state key -> telemetry.db column. Kept as DATA so the whole mapping reads at
# a glance; the fields with their own rules are handled explicitly below.
_STATE_COLUMNS = {
    "soc": "bms_soc_percent", "voltage": "bms_voltage_V",
    "current": "bms_current_A", "pack_voltage": "mms_measured_voltage_V",
    # Battery B's BMS (can1). The unprefixed three above are battery A's.
    "soc_b": "bms2_soc_percent", "voltage_b": "bms2_voltage_V",
    "current_b": "bms2_current_A",
    "motor_current": "mms_current_A", "regen_energy": "regen_energy",
    "target_speed_kmh": "target_speed_kmh",
    "soc_ctrl": "mms_estimated_soc_percent", "trip_m": "mms_trip_m",
    "rpm": "mms_rpm",
    "temp": "mms_temperature_C", "power_w": "mms_power_W",
    "motor_temp": "mms_motor_temp_C", "motor_ohms": "mms_motor_ohms",
    "motor_map": "mms_motor_map", "motor_map_raw": "mms_motor_map_raw",
    "throttle_pct": "mms_throttle_percent", "throttle_mv": "mms_throttle_mv",
    "last_lap_energy": "last_lap_energy",
    "total_race_energy": "total_race_energy",
    "last_lap_regen_energy": "last_lap_regen_energy",
    "stint_energy": "stint_energy", "stint_regen_energy": "stint_regen_energy",
    "last_lap_time_s": "last_lap_time_s", "lap_distance_m": "lap_distance_m",
    # Road speed comes from ONE place: the controller's own field on 0x610, the
    # same one the driver HUD reads. NO fallback to a value derived from RPM.
    "speed_kmh": "mms_vehicle_speed_kmh",
    # Which speed profile the car says it is flying. Carried forward
    # like any other reading: the car publishes it every sample, so a
    # value with an age means "last reported N seconds ago".
    "active_strategy": "active_strategy",
}

# Not carried forward, deliberately:
#   lap_source is only meaningful paired with the lap that just happened.
_NO_CARRY_FORWARD = {"lap_source"}


def read_live_state(conn):
    """Latest sample + freshness, as (state, age_seconds).

    Includes CARRY-FORWARD: a field the newest row does not carry falls back
    to the last value this device ever reported, via db.latest_known(). Without
    it a quiet CAN bus blanks half the tiles a second after
    they were fine.

    `_field_ages` says how old each carried-forward value is, so the UI can say
    "3s" or "40 min" rather than presenting stale data as current. Every field
    defaults to None, never 0: an absent reading is not a measurement.
    """
    row = db.latest_sample(conn)
    known = db.latest_known(conn)
    now = time.time()

    state = {k: None for k in _STATE_COLUMNS}
    state.update({
        "motor_map": None, "throttle_zone": None,
        "batt_temp": None,
        "lap_source": None, "auto_lap": None, "odometer_km": None,
        # lat/lon fall back to the Zolder paddock so the map has somewhere to
        # centre. has_gps says whether the pin is REAL: 0,0 is a real place in
        # the Atlantic, and a placeholder must never be mistakable for a fix.
        "lat": 50.9895, "lon": 5.2568, "has_gps": False,
        "bms_has_error": 0, "bms_error_code": 0, "bms_protections": "",
        "mms_has_error": 0, "mms_error_code": 0, "mms_alerts": "",
    })
    field_ages = {}
    if row is None:
        state["_field_ages"] = field_ages
        return state, None

    def cf(key, column):
        """The row's value, or the last known one, with its age."""
        try:
            v = row[column]
        except (IndexError, KeyError):
            v = None
        if v is not None:
            return v
        if key in _NO_CARRY_FORWARD:
            return None
        hit = known.get(column)
        if hit and hit[0] is not None:
            if hit[1]:
                field_ages[key] = now - hit[1]
            return hit[0]
        return None

    for key, column in _STATE_COLUMNS.items():
        state[key] = cf(key, column)
    state["motor_map"] = cf("motor_map", "mms_motor_map")
    state["motor_map_raw"] = cf("motor_map_raw", "mms_motor_map_raw")
    state["lap_source"] = _val(row, "lap_source", None)

    # Prefer the zone the CAR classified — what the driver's bar actually
    # showed. Fall back to classifying here only for rows written before the
    # column existed; efficiency.zone() is the same function the car ran.
    zone = cf("throttle_zone", "mms_throttle_zone")
    state["throttle_zone"] = zone or efficiency.zone(state["throttle_pct"])

    # Per-cell thermistor temperatures, however many are configured.
    for i in range(1, db.THERMISTOR_CELL_COLUMN_COUNT + 1):
        col = "bms_cell_temp_%02d_C" % i
        state[col] = cf(col, col)

    # BATTERY TEMP IS THE HOTTEST ORION CELL, a deliberate difference from the
    # earlier Streamlit dashboard, which showed battery_temp_C: the thermistor
    # module's own AVERAGE. The tile has always said "hottest cell in the
    # pack", and an average hides the one cell that is running away. Same gate as the Cell
    # Voltages tab, so a failed thermistor's nonsense negative never counts.
    # Null when no thermistor reports: never the average as a stand-in.
    hottest = None
    for i in range(1, db.THERMISTOR_CELL_COLUMN_COUNT + 1):
        col = "bms_cell_temp_%02d_C" % i
        v = limits.plausible_cell_temp(state[col])
        if v is not None and (hottest is None or v > hottest[0]):
            hottest = (v, col)
    if hottest is not None:
        state["batt_temp"] = hottest[0]
        if hottest[1] in field_ages:
            field_ages["batt_temp"] = field_ages[hottest[1]]

    # The two BMS units' own NTC probes (BMS A = bms_, BMS B = bms2_), keyed
    # as their columns and carried forward like the thermistors.
    for col in PROBE_COLUMNS:
        state[col] = cf(col, col)

    for key, column in (("bms_has_error", "bms_has_error"),
                        ("bms_error_code", "bms_error_code"),
                        ("mms_has_error", "mms_has_error"),
                        ("mms_error_code", "mms_error_code")):
        state[key] = _val(row, column, 0)
    state["bms_protections"] = _val(row, "bms_protections", "")
    state["mms_alerts"] = _val(row, "mms_alerts", "")

    # The car's own health block, from the LATEST ROW ONLY. Never cf(): these
    # answer "what is true right now", and a carried-forward can_state of
    # "live" from four minutes ago would report a healthy bus while the bus is
    # exactly what has died. See car_health().
    for col in _HEALTH_COLUMNS:
        state[col] = _val(row, col, None)

    # Per-cell voltages for the Cell Voltages tab, carried forward like the
    # thermistors above, with bms_string_count as the "is this cell real" gate.
    for i in range(1, db.BMS_CELL_COLUMN_COUNT + 1):
        col = "bms_cell_%02d_V" % i
        state[col] = cf(col, col)
    state["bms_string_count"] = cf("bms_string_count", "bms_string_count")

    auto_lap = cf("auto_lap", "calculated_lap")
    state["auto_lap"] = None if auto_lap is None else int(auto_lap)
    odo = cf("odometer_km", "odometer_m")
    state["odometer_km"] = None if odo is None else odo / 1000.0
    state["has_gps"] = (_val(row, "lat", None) is not None
                        and _val(row, "lon", None) is not None)
    state["lat"] = _val(row, "lat", 50.9895)
    state["lon"] = _val(row, "lon", 5.2568)
    state["_field_ages"] = field_ages

    device_ts = row["device_ts"]
    return state, ((now - device_ts) if device_ts else None)


def _race_clock(conn):
    r = db.load_race_state(conn)
    elapsed_min = 0.0
    if r["is_racing"] and r["race_start_time"]:
        elapsed_min = (time.time() - r["race_start_time"]) / 60.0
    return r, elapsed_min, max(0.0, 1440.0 - elapsed_min)


DRIVER_STINT_KEY = "driver_stint"
RACE_UNDO_KEY = "race_undo"


def race_undo_available(conn, now=None):
    """Whether a race reset can still be taken back, and what it would restore."""
    now = time.time() if now is None else now
    u = load_app_state(conn, RACE_UNDO_KEY) or {}
    at = u.get("at")
    if not at or (now - at) >= RACE_UNDO_S:
        return None
    return u


def _stint_elapsed(st, now):
    """Seconds this driver has been in the car, counting RACE time only.

    The record keeps a banked total plus the instant the current running period
    began; `running_since` is None whenever the race clock is stopped.

    A legacy record (started_at only, written before stint time was gated on
    the race) reads as "running since it started", so upgrading mid-stint never
    silently resets a countdown that is already ticking.
    """
    if "accumulated_s" not in st and "running_since" not in st:
        started = st.get("started_at")
        return max(0.0, now - started) if started else 0.0
    banked = float(st.get("accumulated_s") or 0.0)
    since = st.get("running_since")
    return banked + (max(0.0, now - since) if since else 0.0)


def driver_stint(conn, now=None):
    """The driver-change countdown, computed here so every viewer agrees.

    Stored in SQLite beside the race clock rather than in a browser, because
    "when did this driver get in" must survive a refresh and must read the same
    on the pit laptop, the strategist's tablet and a phone on the wall.

    STINT TIME ONLY RUNS WHILE THE RACE DOES. Before the green flag, and through
    any stoppage, the countdown holds. Otherwise it drains through setup and
    shows a false OVERDUE before the race has even started — the cry-wolf
    failure limits.py exists to prevent. The clock is banked on stop and
    resumed on start.

    `remainingS` goes NEGATIVE once the change is overdue — the UI then shows
    how far past the limit you are, which is the number that matters.
    """
    now = time.time() if now is None else now
    st = load_app_state(conn, DRIVER_STINT_KEY) or {}
    started = st.get("started_at")
    base = {
        "limitS": DRIVER_STINT_LIMIT_S,
        "warnS": DRIVER_STINT_WARN_S,
        "critS": DRIVER_STINT_CRIT_S,
    }
    base["publicSynced"] = public_driver_synced(st.get("driver") or None)
    if not started:
        # A name typed before the green flag is kept here and carried into
        # stint one when the race starts.
        return {**base, "startedAt": None, "stint": 0,
                "driver": st.get("driver") or None,
                "elapsedS": None, "remainingS": None, "tier": limits.NORMAL,
                "overdue": False, "canUndo": False, "previousStintS": None,
                "accumulatedS": 0.0, "runningSince": None, "running": False,
                "followsRace": False}

    elapsed = _stint_elapsed(st, now)
    remaining = DRIVER_STINT_LIMIT_S - elapsed
    if remaining <= DRIVER_STINT_CRIT_S:
        tier = limits.CRITICAL
    elif remaining <= DRIVER_STINT_WARN_S:
        tier = limits.WARNING
    else:
        tier = limits.NORMAL

    legacy = "accumulated_s" not in st and "running_since" not in st
    since = started if legacy else st.get("running_since")
    return {
        **base,
        # Would correcting the race start move this stint too? Answered HERE so
        # the UI can say what will happen instead of guessing at the rule; the
        # same predicate the race endpoint acts on, so they cannot disagree.
        "followsRace": _stint_follows_race(
            st, db.load_race_state(conn).get("race_start_time")),
        "startedAt": started,
        "stint": int(st.get("stint", 1)),
        "driver": st.get("driver") or None,
        "elapsedS": elapsed,
        "remainingS": remaining,
        "tier": tier,
        "overdue": remaining < 0,
        # The two fields the browser needs to tick the display itself: banked
        # seconds, and the instant the running period began (null = holding).
        "accumulatedS": 0.0 if legacy else float(st.get("accumulated_s") or 0.0),
        "runningSince": since,
        "running": since is not None,
        # Undo is offered briefly, and only when there is something to restore.
        "canUndo": bool(st.get("previous_started_at")) and elapsed < DRIVER_STINT_UNDO_S,
        "previousStintS": st.get("previous_stint_s"),
    }


def _set_stint_running(conn, running, now=None):
    """Start or hold the stint clock, banking whatever has run so far.

    Called by the race endpoint so the two clocks move together. Idempotent:
    resuming an already-running stint changes nothing, which matters because a
    mid-race restart must never reset a two-hour countdown.
    """
    now = time.time() if now is None else now
    st = load_app_state(conn, DRIVER_STINT_KEY) or {}
    if not st.get("started_at"):
        return
    already = st.get("running_since") is not None
    if running and already:
        return
    st["accumulated_s"] = _stint_elapsed(st, now)
    st["running_since"] = now if running else None
    save_app_state(conn, DRIVER_STINT_KEY, st)


# ── The driver's name on the public spectator page ─────────────────────────── #
# The stint's driver name is mirrored to Firebase /public/driver, which
# docs/index.html reads. No default: an unnamed stint deletes the node and the
# page hides its driver card.
#
# The write happens on a background thread, never inside the request. The
# "Driver changed" button is pressed mid pit stop and must not wait on the
# internet. Endpoints that change the name wake the thread; it also re-checks
# every PUBLIC_DRIVER_RESYNC_S, so a failed write is retried by itself.
#
# Only the REAL store publishes. The demo dashboard (SOLARRACE_DB_PATH pointed
# at a demo store) must never put a made-up name in front of the public.
PUBLIC_DRIVER_ENABLED = os.path.abspath(DB_PATH) == os.path.abspath(SQLITE_PATH)
PUBLIC_DRIVER_RESYNC_S = 15
PUBLIC_DRIVER_MAX_LEN = 40
_NOT_SENT = object()
_public_driver_sent = _NOT_SENT
_public_driver_lock = threading.Lock()
_public_driver_wake = threading.Event()


def public_driver_synced(name):
    """True when the public page shows `name` (or no name, for None), False
    while a write is pending or failing, None when publishing is off."""
    if not PUBLIC_DRIVER_ENABLED:
        return None
    return _public_driver_sent is not _NOT_SENT and _public_driver_sent == name


def sync_public_driver():
    """Make /public/driver match the current stint. Returns True when in sync."""
    global _public_driver_sent
    if not PUBLIC_DRIVER_ENABLED:
        return None
    with _public_driver_lock:
        try:
            with closing(ro_conn()) as conn:
                st = load_app_state(conn, DRIVER_STINT_KEY) or {}
            name = st.get("driver") or None
            if _public_driver_sent is not _NOT_SENT and _public_driver_sent == name:
                return True
            import driver_message
            driver_message.publish_driver_name(name)
            _public_driver_sent = name
            return True
        except Exception as e:
            print("[public driver] not published, will retry: %s" % e, flush=True)
            return False


def _kick_public_driver():
    _public_driver_wake.set()


def _public_driver_loop():
    while True:
        _public_driver_wake.clear()
        sync_public_driver()
        _public_driver_wake.wait(PUBLIC_DRIVER_RESYNC_S)


def _stint_follows_race(st, old_start):
    """Is this stint still the one auto-started by the green flag?

    True only when the stint began with the race and has had no life of its
    own since: stint one, no driver change ever logged, nothing banked from a
    stoppage, and a start that matches the race's. Under those conditions the
    stint clock IS the race clock, so correcting one has to correct the other.

    False the moment any of that stops holding. After a driver change the
    stint began at that change, and a correction to the race start says
    nothing about when the current driver got in — moving it would overwrite a
    time the crew actually observed with one inferred from an unrelated edit.
    """
    if not st or not old_start or not st.get("started_at"):
        return False
    if int(st.get("stint", 1)) != 1 or st.get("previous_started_at"):
        return False
    if float(st.get("accumulated_s") or 0.0) > 1.0:
        return False
    # Within a second: both were written from the same value, so this is an
    # equality test with room for float round-tripping through JSON.
    return abs(float(st["started_at"]) - float(old_start)) <= 1.0


# --------------------------------------------------------------------------- #
# The car's own health report
# --------------------------------------------------------------------------- #
# The badge used to say LIVE whenever telemetry was arriving, and that had a
# blind spot. Telemetry was published only when a CAN frame arrived, or on a
# GPS timer gated on having a fix. So a Pi that was powered, networked and
# running perfectly, with its CAN unplugged, in a garage with no view of the
# sky, published NOTHING -- indistinguishable on the pit wall from a dead Pi, a
# flat battery or a WiFi dropout, and the crew went looking for the wrong
# fault while the car sat there fine.
#
# The car now heartbeats every few seconds regardless of CAN and GPS, carrying
# a small block describing itself. These fields are read with a plain "value
# on the latest row" and NEVER carried forward: every other metric falls back
# to its last known value, but these answer "what is true right now", and a
# carried-forward can_state of "live" from four minutes ago would report a
# healthy bus while the bus is exactly what has died.

# A channel quiet for longer than this is worth naming. Matches
# CHANNEL_QUIET_AFTER_S on the car; the car decides WHICH channel, this only
# decides whether to raise the subject at all.
CAN_QUIET_AFTER_S = 3.0

_HEALTH_COLUMNS = ("pi_uptime_s", "can_state", "can_silent_s", "can_detail",
                   "can_frames", "gps_fix", "gps_detail")


def car_health(state):
    """(ok, problems): what the car says about itself, or (True, []) if it
    says nothing.

    ok=True for a car that predates the heartbeat, on purpose. Silence from an
    old build is not evidence of a fault, and turning the badge amber for
    every team member still running last week's image would train everyone
    to ignore it by Saturday.

    Ported line for line from the earlier Streamlit dashboard's car_health();
    tools/check_health.py holds the nine cases it must get right.
    """
    if state.get("can_state") is None and state.get("gps_fix") is None:
        return True, []

    problems = []
    can_state = state.get("can_state")
    silent = state.get("can_silent_s")
    detail = (state.get("can_detail") or "").strip()
    if can_state == "disconnected":
        problems.append("CAN bus not open")
    elif can_state in ("silent", "starting") or (
            silent is not None and silent > CAN_QUIET_AFTER_S):
        # The car names the channel, because the channel names live on the
        # car: socketcan gives can0/can1, a USB adapter something else.
        problems.append(detail or "CAN silent")
    elif detail:
        # Live overall, but the car still flagged a channel. This is the one
        # that used to be invisible: can0 talking normally while can1 (the
        # second BMS) has been dead for an hour.
        problems.append(detail)

    if state.get("gps_fix") == 0:
        problems.append("no GPS fix")

    return (not problems), problems


def _health_json(state):
    ok, problems = car_health(state)
    return {
        "ok": ok,
        "problems": problems,
        "piUptimeS": state.get("pi_uptime_s"),
        "canState": state.get("can_state"),
        "canSilentS": state.get("can_silent_s"),
        "canFrames": state.get("can_frames"),
        "gpsFix": state.get("gps_fix"),
        "gpsDetail": state.get("gps_detail"),
    }


# --------------------------------------------------------------------------- #
# Cell Voltages tab: DS003 per-cell temperature, DS004 per-module voltage
# --------------------------------------------------------------------------- #
# Ported from the earlier Streamlit dashboard's Cell Voltages tab. The rulebook
# asks for 26 live module voltages; this is the screen that claims compliance,
# so it reports the SHORTFALL rather than quietly rendering 26 tiles of which
# half are dashes. Classification happens here with the same Threshold the driver
# HUD uses -- the browser never compares a cell against a limit.

# The BMS units' NTC probes, (pack, prefix) as main._remap_bms_frame names them.
PROBE_PACKS = (("A", "bms"), ("B", "bms2"))
PROBE_COLUMNS = ["%s_temp_%d_C" % (pre, n) for _pack, pre in PROBE_PACKS
                 for n in (1, 2, 3)]

# DS004's requirement. A hand-kept constant, like db.THERMISTOR_CELL_COLUMN_COUNT.
DS004_MODULE_COUNT = 26


def _cell_value(state, i):
    """One cell's voltage, or None if it is not a real reading.

    Gated on bms_string_count, not just on the stored value being present: a
    CAN ID can be POLLED (so its frame arrives and decodes to a literal
    0.000 V) without the tap being electrically wired to a real cell -- seen
    on this exact hardware in bench captures. Treating that as a genuine
    0.000 V would be indistinguishable from a shorted cell; treating it as
    unreported is the honest answer.
    """
    v = state.get("bms_cell_%02d_V" % i)
    if v is None:
        return None
    known = state.get("bms_string_count")
    if known is not None and i > known:
        return None
    return v


def _cell_temp_value(state, i):
    """One thermistor, or None if it never reported or reports a broken-sensor
    value. The Orion module reports a failed thermistor as a nonsense
    NEGATIVE; limits.plausible_cell_temp gates the low side only, because an
    implausibly HIGH reading must still reach the screen."""
    return limits.plausible_cell_temp(state.get("bms_cell_temp_%02d_C" % i))


def _compact_ranges(nums):
    """13-26 instead of fourteen separate numbers."""
    out, start, prev = [], None, None
    for n in list(nums) + [None]:
        if start is None:
            start = prev = n
        elif n is not None and n == prev + 1:
            prev = n
        else:
            out.append(str(start) if start == prev else "%d-%d" % (start, prev))
            start = prev = n
    return ", ".join(out)


def ds004_compliance(state):
    """(valid, required, missing ids). "Valid" means a reading this screen
    would actually show a scrutineer: present, past the string-count gate,
    and non-zero. An unwired tap decodes to a literal 0.000 V, and counting
    that toward compliance is exactly the fabrication this screen exists to
    avoid."""
    missing, valid = [], 0
    for i in range(1, DS004_MODULE_COUNT + 1):
        v = _cell_value(state, i)
        if v is None or float(v) <= 0.0:
            missing.append(i)
        else:
            valid += 1
    return valid, DS004_MODULE_COUNT, missing


def build_cells(state, age, fresh):
    ages = state.get("_field_ages", {})

    def temp_tile(i, label):
        v = _cell_temp_value(state, i)
        return {"id": i, "label": label, "value": v,
                "tier": limits.classify(v, limits.CELL_TEMP),
                "staleS": ages.get("bms_cell_temp_%02d_C" % i)}

    def volt_tile(i, label):
        v = _cell_value(state, i)
        return {"id": i, "label": label, "value": v,
                "tier": limits.classify(v, limits.CELL_VOLTAGE),
                "staleS": ages.get("bms_cell_%02d_V" % i)}

    n_therm = db.THERMISTOR_CELL_COLUMN_COUNT
    configured = any(_cell_temp_value(state, i) is not None
                     for i in range(1, n_therm + 1))
    groups, mapped = [], set()
    for name, lo, hi in limits.THERMISTOR_GROUP_RANGES:
        ids = list(range(lo, hi + 1))
        mapped.update(ids)
        groups.append({
            "name": name, "lo": lo, "hi": hi,
            "label": "%s-%s" % (limits.cell_temp_label(lo), limits.cell_temp_label(hi)),
            "cells": [temp_tile(i, limits.cell_temp_label(i)) for i in ids],
        })
    # Anything reporting outside the mapped ranges, shown only when it
    # actually reports: no data is silently dropped, but an empty section is
    # clutter on a compliance screen.
    extra_t = [i for i in range(1, n_therm + 1)
               if i not in mapped and _cell_temp_value(state, i) is not None]

    valid, required, missing = ds004_compliance(state)
    n_cells = db.BMS_CELL_COLUMN_COUNT
    extra_v = [i for i in range(DS004_MODULE_COUNT + 1, n_cells + 1)
               if _cell_value(state, i) is not None]

    # BMS probes, live. Gated like the thermistors: a disconnected probe
    # reads about -273 C and must show as a dash. The age is only quoted past
    # DATA_STALE_AFTER_S and only beside a value, as ui.render_metric does.
    def probe_tile(pack, pre, n):
        col = "%s_temp_%d_C" % (pre, n)
        v = limits.plausible_cell_temp(state.get(col))
        stale_s = ages.get(col)
        return {"id": n, "label": "BMS %s T%d" % (pack, n), "value": v,
                "tier": limits.classify(v, limits.CELL_TEMP), "staleS": stale_s,
                "stale": (v is not None and stale_s is not None
                          and stale_s > C.DATA_STALE_AFTER_S)}

    return {
        "fresh": fresh, "age": age,
        "probes": [{"pack": pack, "cells": [probe_tile(pack, pre, n) for n in (1, 2, 3)]}
                   for pack, pre in PROBE_PACKS],
        "temps": {
            "configured": configured,
            "warn": limits.CELL_TEMP.warn, "crit": limits.CELL_TEMP.crit,
            "groups": groups,
            "unmapped": [temp_tile(i, limits.cell_temp_label(i)) for i in extra_t],
        },
        "voltages": {
            "stringCount": state.get("bms_string_count"),
            "required": required, "valid": valid,
            "ok": valid >= required,
            "missing": _compact_ranges(missing),
            "warn": limits.CELL_VOLTAGE.warn, "crit": limits.CELL_VOLTAGE.crit,
            "modules": [volt_tile(i, "Module %d" % i)
                        for i in range(1, DS004_MODULE_COUNT + 1)],
            "extra": [volt_tile(i, "Cell %d" % i) for i in extra_v],
            "extraRange": "%d-%d" % (DS004_MODULE_COUNT + 1, n_cells),
        },
    }


def build_live(conn, manual_lap=-1):
    """The whole fast tier in one payload: tiles, sidebar, sectors, map.

    One SQLite read feeds all of it.
    """
    state, age = read_live_state(conn)
    race, elapsed_min, left_min = _race_clock(conn)
    fresh = age is not None and age <= C.DATA_STALE_AFTER_S

    active_lap = manual_lap if manual_lap >= 0 else state["auto_lap"]
    expected = elapsed_min / C.TARGET_LAP_TIME_MIN if C.TARGET_LAP_TIME_MIN else 0

    # Prefer the car's own "metres since the last lap trigger". Once laps are
    # cut at the GPS finish line, odometer % 4000 no longer lines up with the
    # real boundary and the sector display drifts further out of step each lap.
    odo_km = state["odometer_km"]
    if state["lap_distance_m"] is not None:
        lap_dist = float(state["lap_distance_m"]) % C.TRACK_LENGTH_METERS
    elif odo_km is not None:
        lap_dist = (odo_km * 1000.0) % C.TRACK_LENGTH_METERS
    else:
        # Position still needs a number to draw a strip, and 0 m is where it sat
        # before the car reported. The distance READOUTS stay null.
        lap_dist = 0.0

    profile_df, profile_meta = _active_profile(state)
    track = get_live_track_status(lap_dist, profile_df)
    try:
        raw = track.get("section", "Section 1")
        sector_id = int(raw.split(" ")[-1]) if "Section" in raw else 1
    except (ValueError, AttributeError):
        sector_id = 1

    faults = []
    if state["bms_has_error"]:
        faults.append("BMS: " + (
            state["bms_protections"]
            or C.decode_error_bits(state["bms_error_code"], C.BMS_PROTECTION_BITS)
            or "code 0x%X" % int(state["bms_error_code"] or 0)))
    if state["mms_has_error"]:
        faults.append("MMS: " + (
            state["mms_alerts"]
            or C.decode_error_bits(state["mms_error_code"], C.MMS_ERROR_BITS)
            or "error 0x%X" % int(state["mms_error_code"] or 0)))

    ctx = {
        "active_lap": active_lap,
        "current_lap_dist_m": lap_dist,
        "odometer_km": odo_km,
    }

    # The Live Metrics tiles, resolved and CLASSIFIED here. The browser is
    # never handed a threshold to compare against — limits.classify() is the one
    # comparison in the project and both dashboards call it.
    tiles = []
    for group, entries in live_metrics.LIVE_METRIC_GROUPS:
        out = []
        for e in entries:
            value = live_metrics.resolve(e, state, ctx)
            tier = limits.NORMAL
            lim = getattr(limits, e["limit"]) if e.get("limit") else None
            if lim is not None:
                judged = abs(value) if (e.get("mag") and value is not None) else value
                tier = limits.classify(judged, lim)
            out.append({
                "label": e["label"], "unit": e.get("unit", ""),
                "spec": e.get("spec", ".0f"), "note": e.get("note"),
                "text": bool(e.get("text")),
                "value": value, "tier": tier,
            })
        tiles.append({"group": group, "metrics": out})

    return {
        "ts": time.time(),
        "age": age,
        "fresh": fresh,
        "state": state,
        "faults": faults,
        "health": _health_json(state),
        "race": {
            "isRacing": race["is_racing"],
            "startTime": race["race_start_time"],
            "elapsedMin": elapsed_min,
            "hoursLeft": int(left_min // 60),
            "minsLeft": int(left_min % 60),
            "secsLeft": int((left_min * 60) % 60),
            # A reset is reversible for a couple of minutes; the Danger zone
            # shows the undo button only while this is true.
            "canUndo": race_undo_available(conn) is not None,
        },
        "activeLap": active_lap,
        # None when the car has not reported a lap count and nobody has set one.
        # Comparing an unknown tally against a target produces a deficit that
        # looks like the car is losing the race.
        "lapDelta": None if active_lap is None else active_lap - expected,
        "odometerKm": odo_km,
        "lapDistanceM": lap_dist,
        "sectorId": sector_id,
        "sectorName": C.SECTION_NAMES.get(sector_id, "Section %d" % sector_id),
        "track": track,
        # Which profile the target speed above came from, and how well
        # we know it. The card says so, because an assumed target must
        # never look like a confirmed one.
        "activeProfile": profile_meta,
        # Tiers for the seven top-strip tiles. Computed here for the same reason
        # as the Live Metrics tiers above.
        "tiers": {
            "motorTemp": limits.classify(state["motor_temp"], limits.MOTOR_TEMP),
            "ctrlTemp": limits.classify(state["temp"], limits.CTRL_TEMP),
            "soc": limits.classify(state["soc"], limits.SOC),
            "battTemp": limits.classify(state["batt_temp"], limits.CELL_TEMP),
            "power": limits.classify(state["power_w"], limits.POWER),
        },
        "liveMetrics": tiles,
        "driverStint": driver_stint(conn),
    }


# --------------------------------------------------------------------------- #
@asynccontextmanager
async def _lifespan(_app):
    # Started here, not at import: the tools/check_*.py scripts import this
    # module and must not start writing to Firebase.
    if PUBLIC_DRIVER_ENABLED:
        threading.Thread(target=_public_driver_loop, name="public-driver",
                         daemon=True).start()
    yield


app = FastAPI(title="Afeka Pit Wall — React backend", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"], allow_headers=["*"],
)


@app.get("/api/config")
def api_config():
    """Everything the UI needs to style and label itself, from Python.

    Thresholds, tier colours, the chart palette, section metadata and the
    strategy list are all served rather than retyped in TypeScript. The rule is
    absolute: a hex or a threshold typed in the frontend is a bug.
    """
    def thr(t):
        return {"warn": t.warn, "crit": t.crit, "lowSide": t.low_side,
                "fullScale": t.full_scale}

    return {
        "tierColours": limits.TIER_COLOURS,
        # The light theme needs darker variants: #ff6500 on white is about
        # 2.9:1 contrast, which is too low to read a number in. The TIER and its
        # THRESHOLD are shared; only the light-mode rendering differs.
        "tierColoursLight": {limits.NORMAL: None, limits.WARNING: "#b35400",
                             limits.CRITICAL: "#c62828"},
        "tiers": {"normal": limits.NORMAL, "warning": limits.WARNING,
                  "critical": limits.CRITICAL},
        "thresholds": {n: thr(getattr(limits, n)) for n in (
            "MOTOR_TEMP", "CTRL_TEMP", "CELL_TEMP", "SOC", "PACK_VOLTAGE",
            "BATT_CURRENT", "MOTOR_CURRENT", "POWER", "SPEED")},
        "metrics": [{"key": m.key, "label": m.label, "unit": m.unit,
                     "color": m.color} for m in HISTORY_CHARTS],
        "historyWindows": {"1 min": 1, "5 min": 5, "15 min": 15, "1 hour": 60,
                           "3 hours": 180, "12 hours": 720, "24 hours": 1440,
                           "All": None},
        "historyDefaultMetrics": ["Speed", "Battery SoC"],
        "strategies": C.STRATEGIES,
        "defaultStrategyKey": C.DEFAULT_STRATEGY_KEY,
        "sections": {
            "names": C.SECTION_NAMES,
            "turnLabels": C.SECTION_TURN_LABELS,
            "risk": C.SECTION_RISK,
            "colors": C.SECTION_COLORS,
            "bounds": {sid: info["range"] for sid, info in SECTIONS_INFO.items()},
        },
        "trackLengthM": C.TRACK_LENGTH_METERS,
        "dataStaleAfterS": C.DATA_STALE_AFTER_S,
        "targetLapTimeMin": C.TARGET_LAP_TIME_MIN,
        "driverStint": {"limitS": DRIVER_STINT_LIMIT_S,
                        "warnS": DRIVER_STINT_WARN_S,
                        "critS": DRIVER_STINT_CRIT_S},
        "exportGroups": list(export.METRIC_GROUPS.keys()),
        "liveMetricCount": live_metrics.LIVE_METRIC_COUNT,
        "liveMetricsPerRow": live_metrics.LIVE_METRICS_PER_ROW,
        # Zolder paddock — where the map centres before the car reports.
        "mapFallback": {"lat": 50.9895, "lon": 5.2568},
    }


@app.get("/api/live")
def api_live(manual_lap: int = Query(-1)):
    with closing(ro_conn()) as conn:
        return build_live(conn, manual_lap)


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
def _iso(ts):
    """A sample time as a NAIVE local ISO string, in the zone the team was in
    when it was recorded (pit_config.export_local: Asia/Jerusalem before the
    14 Sept switch, Europe/Brussels after).

    Naive on purpose. Plotly does no timezone conversion on a date axis: it
    draws whatever wall-clock string it is given. Serving UTC with an offset
    put the History axis two hours off the pit's own clocks. Serving local
    wall-clock strings makes the chart, the tables and the Excel export agree,
    and the same rule the export already follows is what decides the zone.
    """
    if not ts:
        return None
    return export_local(ts).replace(tzinfo=None).isoformat(timespec="milliseconds")


@app.get("/api/cells")
def api_cells():
    """The Cell Voltages tab, classified here. Polled only while that tab is
    open, so the 70 per-cell values do not ride the live socket for every
    viewer of every other tab."""
    with closing(ro_conn()) as conn:
        state, age = read_live_state(conn)
    fresh = age is not None and age <= C.DATA_STALE_AFTER_S
    return build_cells(state, age, fresh)


# --------------------------------------------------------------------------- #
# Rule 3.5.6 — cell extremes over the last 2 hours
# --------------------------------------------------------------------------- #
# The report is a range aggregate over ~2 h of rows, so every viewer shares
# one read per 30 s.
CELL_EXTREMES_CACHE_S = 30


def _short_duration(seconds):
    """A duration as the tiles print it: "45 min", "2 h", "2 h 05 min"."""
    minutes = int(seconds // 60)
    if minutes < 60:
        return "%d min" % minutes
    h, m = divmod(minutes, 60)
    return "%d h" % h if m == 0 else "%d h %02d min" % (h, m)


def _clock(ts):
    """HH:MM:SS in the zone the sample was recorded in, like _iso()."""
    return export_local(ts).strftime("%H:%M:%S")


# (key, title, unit, spec, limit, cell label). Labels come from limits.py.
_EXTREME_TILES = (
    ("temp_max", "Highest Cell Temp", "°C", ".1f", limits.CELL_TEMP, limits.cell_temp_label),
    ("temp_min", "Lowest Cell Temp", "°C", ".1f", limits.CELL_TEMP, limits.cell_temp_label),
    ("volt_max", "Highest Cell Voltage", "V", ".3f", limits.CELL_VOLTAGE, lambda i: "Module %d" % i),
    ("volt_min", "Lowest Cell Voltage", "V", ".3f", limits.CELL_VOLTAGE, lambda i: "Module %d" % i),
)


def build_cell_extremes(report):
    """db.cell_extremes_report, labelled and classified for the Rule 3.5.6
    tiles. Every string with a time
    or a threshold in it is made here, so the browser only lays it out."""
    end_ts = report.get("end_ts")
    covers, window = report["covers_s"], report["window_s"]
    out = {"endTs": end_ts, "coversS": covers, "windowS": window,
           "refreshS": CELL_EXTREMES_CACHE_S, "state": "empty",
           "span": None, "coverText": None, "end": None, "tiles": []}
    if end_ts is None:
        return out
    full = covers >= window - 60
    out["state"] = "none" if covers <= 0 else ("full" if full else "partial")
    out["end"] = _clock(end_ts)
    out["span"] = "%s → %s" % (_clock(end_ts - (window if full else covers)), _clock(end_ts))
    out["coverText"] = _short_duration(covers)
    for key, title, unit, spec, limit, label in _EXTREME_TILES:
        reading = report.get(key)
        tile = {"key": key, "title": title, "unit": unit, "spec": spec,
                "value": None, "tier": limits.NORMAL, "cell": None,
                "ts": None, "note": "no reading in the window"}
        if reading is not None:
            value, cell, ts = reading
            when = "time unknown" if ts is None else "%s · %s before newest" % (
                _clock(ts), _short_duration(max(0.0, end_ts - ts)))
            tile.update(value=value, cell=cell, ts=ts,
                        tier=limits.classify(value, limit),
                        note="%s · %s" % (label(cell), when))
        out["tiles"].append(tile)
    return out


@app.get("/api/cell_extremes")
def api_cell_extremes():
    """Rule 3.5.6: highest/lowest cell temperature and voltage, last 2 h."""
    def build():
        with closing(ro_conn()) as conn:
            return build_cell_extremes(db.cell_extremes_report(conn))
    return cached("cell_extremes", build, ttl=CELL_EXTREMES_CACHE_S)


@app.post("/api/trip_reset")
def api_trip_reset():
    """Ask the CAR to zero its own tracked distance total.

    Distinct from Cut Lap: does not touch lap count or energy, and does not
    reset the controller's hardware TRIP register, for which there is no
    documented CAN command. Shares the lap-command node with Cut Lap, which is
    why the ack below filters on action.
    """
    import driver_message
    try:
        driver_message.send_trip_reset()
    except Exception as e:
        raise HTTPException(502, "trip reset failed: %s" % e)
    return {"ok": True, "sentAt": time.strftime("%H:%M:%S")}


@app.get("/api/trip_reset/ack")
def api_trip_reset_ack():
    """The car's acknowledgement of a trip reset, and ONLY of a trip reset.

    /lap_command_ack is shared with Cut Lap. A Cut Lap ack landing in between
    would otherwise be mistaken for this command's own confirmation.
    """
    import driver_message
    try:
        ack = driver_message.read_lap_ack()
    except Exception as e:
        return {"ack": None, "error": str(e)}
    if isinstance(ack, dict) and ack.get("action") == "reset_trip":
        return {"ack": ack}
    return {"ack": None}

@app.get("/api/history")
def api_history(
    metrics: str = Query("Speed", description="comma-separated metric keys"),
    minutes: float | None = Query(None),
    start: float | None = Query(None),
    end: float | None = Query(None),
    limit: int = Query(200000, ge=1, le=200000),
    max_points: int = Query(4000, ge=100, le=50000),
):
    """One window of history for one or more metrics.

    Returns a column per metric plus the shared time axis, and the rangebreaks
    Plotly needs to elide dead time. `cursor` is the device_ts of the last
    sample served; the WebSocket resumes from it so the client never needs the
    whole series resent.
    """
    keys = [k for k in metrics.split(",") if k]
    chosen = [m for m in HISTORY_CHARTS if m.key in keys]
    if not chosen:
        raise HTTPException(404, "no known metric in %r" % metrics)
    return cached(("history", metrics, minutes, start, end, limit, max_points),
                  lambda: _history(chosen, minutes, start, end, limit, max_points))


def _history(chosen, minutes, start, end, limit, max_points):
    with closing(ro_conn()) as conn:
        lo, hi = db.time_bounds(conn)
        if hi is None:
            # SAME KEYS AS THE POPULATED ANSWER BELOW. The History tab reads
            # count, sampled and tz unconditionally, so a short payload here is
            # not a smaller answer, it is a crash: this returned 200 without
            # them on 2026-09-17 and the tab died on
            # "Cannot read properties of undefined (reading 'toLocaleString')".
            # An empty store is the first thing the pit sees after
            # tools/archive_db.py, i.e. every practice morning and race morning.
            # Zero ROWS is a true statement here and is not the same as the
            # forbidden zero READING: nothing is being invented, the series are
            # simply empty. tools/check_empty_db.py holds these two shapes equal.
            return {"t": [], "series": {m.key: [] for m in chosen},
                    "cursor": None, "rangebreaks": [],
                    "bounds": {"lo": None, "hi": None},
                    "total": 0, "count": 0, "sampled": 0, "downsampled": False,
                    "tz": str(export_zone(None))}
        end_ts = end
        if REPLAY and end_ts is None:
            future = db.fetch_samples(conn, start_ts=lo, end_ts=hi,
                                      limit=REPLAY_HELD_BACK)
            if future:
                hi = future[0]["device_ts"] - 1e-6
                end_ts = hi
        start_ts = start
        if start_ts is None and minutes:
            start_ts = hi - minutes * 60.0
        rows = db.fetch_samples(conn, start_ts=start_ts, end_ts=end_ts, limit=limit)
        total = db.count_samples(conn)

    # Thin to ~max_points by even stride before serialising. The traces are SVG
    # (see below), and 46,836 points x N metrics is both a slow draw and a
    # multi-megabyte JSON over the pit LAN. Even stride rather than min/max
    # bucketing so a thinned trace still reads as the same shape, and the true
    # count is reported separately so nothing claims to show every sample.
    full = len(rows)
    if full > max_points:
        stride = full // max_points + 1
        rows = rows[::stride]

    times = [r["device_ts"] for r in rows]
    return {
        "t": [_iso(ts) for ts in times],
        "series": {m.key: [value_from_row(r, m) for r in rows] for m in chosen},
        # An empty window still needs a cursor, or the socket has nothing to
        # resume from and would fall back to the start of the store — every
        # row ever recorded, in one message. Resume from the window's end.
        "cursor": times[-1] if times else hi,
        "rangebreaks": _rangebreaks(times),
        "bounds": {"lo": lo, "hi": hi},
        "total": total,
        "count": len(rows),
        "sampled": full,
        "downsampled": full > len(rows),
        # Which zone the strings above are in, so the UI can say so.
        "tz": str(export_zone(hi)),
    }


def _rangebreaks(times, factor=8.0, max_breaks=60):
    """Spans containing no samples, for Plotly's xaxis.rangebreaks.

    The store spans months but the car only runs in short sessions, so on a wide
    window ("All") more than 99 % of the axis is time when nothing was recorded.
    Every real burst then compresses into a slice a pixel or two wide and a
    metric that swings hard inside one renders as a bare vertical line: correct,
    and completely unreadable. Removing the empty spans lets each session expand
    to fill the width. The ticks stay real dates.
    """
    if len(times) < 3:
        return []
    deltas = sorted(times[i] - times[i - 1] for i in range(1, len(times)))
    typical = deltas[len(deltas) // 2]
    if typical <= 0:
        return []
    threshold = typical * factor
    gaps = []
    for i in range(1, len(times)):
        gap = times[i] - times[i - 1]
        if gap > threshold:
            gaps.append((gap, times[i - 1], times[i]))
    gaps.sort(reverse=True)
    out = []
    for gap, a, b in gaps[:max_breaks]:
        # Leave a sliver of real gap at each end, or the two sessions butt
        # together and read as continuous telemetry — the very illusion this
        # exists to prevent.
        pad = min(gap * 0.02, typical * 3)
        lo, hi = a + pad, b - pad
        if hi > lo:
            out.append({"bounds": [_iso(lo), _iso(hi)]})
    return out


@app.get("/api/history/stats")
def api_history_stats(metrics: str = Query("Speed"),
                      minutes: float | None = Query(None),
                      start: float | None = Query(None),
                      end: float | None = Query(None)):
    """min / avg / max / now per metric over the served range.

    Computed here, skipping nulls, so a dropout cannot drag an average toward
    zero. The count of missing samples is returned rather than hidden.
    """
    keys = [k for k in metrics.split(",") if k]
    chosen = [m for m in HISTORY_CHARTS if m.key in keys]
    return cached(("stats", metrics, minutes, start, end),
                  lambda: _history_stats(chosen, minutes, start, end))


def _history_stats(chosen, minutes, start, end):
    with closing(ro_conn()) as conn:
        lo, hi = db.time_bounds(conn)
        if hi is None:
            return {"stats": []}
        start_ts = start if start is not None else (hi - minutes * 60.0 if minutes else None)
        rows = db.fetch_samples(conn, start_ts=start_ts, end_ts=end)
    out = []
    for m in chosen:
        vals = [value_from_row(r, m) for r in rows]
        clean = [v for v in vals if v is not None]
        out.append({
            "key": m.key, "label": m.label, "unit": m.unit, "color": m.color,
            "min": min(clean) if clean else None,
            "avg": (sum(clean) / len(clean)) if clean else None,
            "max": max(clean) if clean else None,
            "now": clean[-1] if clean else None,
            "samples": len(clean), "missing": len(vals) - len(clean),
        })
    return {"stats": out}


@app.get("/api/samples")
def api_samples(limit: int = Query(50, ge=1, le=500)):
    """Most recent raw samples, for the History tab's table."""
    with closing(ro_conn()) as conn:
        rows = db.fetch_samples(conn, limit=limit)
    out = []
    for r in rows:
        rec = {"t": _iso(r["device_ts"])}
        for m in HISTORY_CHARTS:
            rec[m.key] = value_from_row(r, m)
        out.append(rec)
    out.reverse()          # newest first, the way a log is read
    return {"rows": out}


@app.get("/api/laps")
def api_laps():
    """One row per completed lap. No integration happens here — the car
    computed each lap's energy and time when it cut the lap, so a dropped
    telemetry link cannot punch holes in these charts."""
    with closing(ro_conn()) as conn:
        rows = db.fetch_lap_summary(conn)
    laps = [{"lap": r["lap"], "energyWh": r["energy_wh"],
             "lapTimeS": r["lap_time_s"], "distanceM": r["distance_m"]}
            for r in rows]
    times = [l["lapTimeS"] for l in laps if l["lapTimeS"]]
    energy = [l["energyWh"] for l in laps if l["energyWh"] is not None]
    return {
        "laps": laps,
        "summary": {
            "count": len(laps),
            "bestS": min(times) if times else None,
            "avgS": (sum(times) / len(times)) if times else None,
            "avgWh": (sum(energy) / len(energy)) if energy else None,
        },
    }


@app.get("/api/faults")
def api_faults(limit_rows: int = Query(3000, ge=1, le=20000),
               gap_s: float = Query(3.0)):
    """Per-sample fault rows collapsed into discrete episodes, so a fault that
    persisted for 200 samples reads as one row rather than 200."""
    with closing(ro_conn()) as conn:
        rows = db.fetch_faults(conn, limit=limit_rows)
    episodes = []
    for r in rows:
        sig = []
        if r["bms_has_error"]:
            sig.append("BMS: " + (r["bms_protections"]
                       or C.decode_error_bits(r["bms_error_code"], C.BMS_PROTECTION_BITS)
                       or "code 0x%X" % int(r["bms_error_code"] or 0)))
        if r["mms_has_error"]:
            sig.append("MMS: " + (r["mms_alerts"]
                       or C.decode_error_bits(r["mms_error_code"], C.MMS_ERROR_BITS)
                       or "error 0x%X" % int(r["mms_error_code"] or 0)))
        signature = " | ".join(sig)
        ts = r["device_ts"]
        if (episodes and episodes[-1]["sig"] == signature
                and ts - episodes[-1]["end"] <= gap_s):
            episodes[-1]["end"] = ts
            episodes[-1]["samples"] += 1
        else:
            episodes.append({"sig": signature, "start": ts, "end": ts,
                             "samples": 1})
    return {"episodes": [{
        "sig": e["sig"], "start": _iso(e["start"]),
        "durationS": e["end"] - e["start"], "samples": e["samples"],
    } for e in reversed(episodes)]}          # newest first


# --------------------------------------------------------------------------- #
# The profile the CAR is running
# --------------------------------------------------------------------------- #
# The pit's target speed must come from the profile the car is ACTUALLY flying,
# not from whichever file this module was pinned to. Before this, api.py loaded
# 210s.xlsx unconditionally, so selecting the 189 s strategy moved the driver's
# HUD target and left the strategist reading the 210 s baseline: two numbers
# called "target speed", disagreeing, on the two screens the crew compares.
#
# strategy_engine.profile_to_df() already existed for exactly this and was
# never called from here. It loads through speed_profile.load_csv -- the CAR's
# own loader -- so the pit and the car cannot interpret the same file
# differently.
#
# THE SELECTION IS NEVER THE ANSWER. /api/strategy/select sends a name over the
# radio and stores nothing, deliberately: "the message left the pit" is not
# "the car changed profile". Only the car's own report counts.


@memo(ttl=30)
def _profile_frame(key, path, mtime):
    """One profile as the frame get_live_track_status() expects.

    `mtime` is in the key because profile_builder.py rewrites these CSVs while
    the dashboard is running; without it the pit would serve a stale curve for
    as long as the process lived.
    """
    return profile_to_df(path)


@memo(ttl=20)
def _acked_key():
    """The profile key the car acknowledged over Firebase, or None.

    Memoised because this is a network call and build_live() runs every 2 s per
    socket. read_strategy_ack() already swallows every exception and returns
    None, and memo() deliberately does not cache a raised exception, so a
    flapping link retries rather than latching a failure.
    """
    try:
        import driver_message
        ack = driver_message.read_strategy_ack()
    except Exception:
        return None
    if not isinstance(ack, dict) or not ack.get("applied"):
        return None
    # The CAR writes "strategy". Not "key" -- see firebase_client.ack_strategy.
    key = ack.get("strategy")
    return key if isinstance(key, str) else None


def _active_profile(state):
    """(frame, {key, source, ageS}) for the curve the car is running.

    Source order, best first:

      1. "car"  -- the telemetry column `active_strategy`. The car sets it in
         the same block that sends the radio ack, so it is the same fact
         arriving over a better path: it survives the radio being down, it
         works in replay, and it costs nothing extra because the live read
         already fetches it.
      2. "ack"  -- the Firebase acknowledgement, for a car build that predates
         the column.
      3. "default" -- nothing has been reported. Flagged so the UI can SAY the
         target speed is assumed. A target from an assumed profile must never
         look like one from a confirmed profile; that is the same rule as
         has_gps versus the paddock fallback.
    """
    try:
        available = speed_profile.available_profiles()
    except Exception:
        available = {}

    key = state.get("active_strategy") if isinstance(state, dict) else None
    source = "car"
    if not (isinstance(key, str) and key in available):
        key, source = _acked_key(), "ack"
        if not (isinstance(key, str) and key in available):
            key, source = C.DEFAULT_STRATEGY_KEY, "default"

    path = available.get(key)
    frame = None
    if path:
        try:
            frame = _profile_frame(key, path, os.path.getmtime(path))
        except Exception:
            frame = None
    if frame is None:
        # Last ditch. A target readout that vanishes is worse than a generic
        # one, and the label already says the profile is not confirmed.
        frame = load_velocity_profile(VELOCITY_PROFILE_PATH)
        if source != "car":
            source = "default"
    return frame, {"key": key, "source": source}


# --------------------------------------------------------------------------- #
# Sector splits
# --------------------------------------------------------------------------- #
# The question this section answers is "is this lap better or worse than the
# last one, and where". Everything below exists to make the nine numbers
# trustworthy enough to act on, because a strategist radios a correction off
# them.
SECTOR_BOUNDS = [(sid, info["range"][0], info["range"][1])
                 for sid, info in sorted(SECTIONS_INFO.items())]
SECTOR_IDS = [sid for sid, _, _ in SECTOR_BOUNDS]
LAP_M = float(C.TRACK_LENGTH_METERS)

# Two samples this far apart do not bracket a timing gate. The car reports at
# roughly 2 Hz, so a real straddling pair is about half a second wide. Anything
# wider is a dropout or a session boundary, and interpolating a crossing inside
# it invents a time. Before this guard the replay store produced sector splits
# of 612,896 s, a "sector time" measured between two samples 71 days apart, and
# rendered them as if real.
#
# 3 s rather than something looser because this is a DISTANCE tolerance wearing
# a clock: at racing speed the car covers about 55 m in 3 s, and sectors 4 and 6
# are only ~100 m long. A pair wider than this can bracket a gate it never
# actually described. Deliberately NOT tied to DATA_STALE_AFTER_S (10 s), which
# answers a different question -- when to grey out a live readout -- and would
# allow a crossing interpolated across 370 m of track.
GATE_MAX_GAP_S = 3.0

# How far either side of the start/finish line a borrowed neighbour sample may
# sit before it is useless for interpolating the line crossing itself.
STITCH_MAX_M = 250.0

# Sector times from the 210 s baseline's own Time(s) column. NOT displayed: the
# dashboard compares against the previous lap, not against a baseline the car
# may not be running. Kept as a server-side sanity floor, because a split far
# below what a sector can physically take is a glitch, and letting one become
# the session best would poison every comparison after it.
REFERENCE_SPLITS = {1: 23.35, 2: 23.64, 3: 40.44, 4: 10.08, 5: 27.93,
                    6: 10.34, 7: 21.13, 8: 28.88, 9: 23.81}
IMPLAUSIBLY_FAST = 0.3          # fraction of the reference


def _runs(samples):
    """One trace cut into laps, at every distance reset.

    fetch_lap_track() returns every row tagged with one value of
    `calculated_lap`. That is normally one lap, but the counter stalls when the
    GPS trigger stops firing, and then a single tag holds hours of driving with
    a sawtooth distance. _crossing_time() takes the FIRST pair straddling a
    boundary, so on such a trace it reports a split measured between two samples
    days apart. Cutting at the resets makes each run one real lap and makes the
    LAST run the most recent one.

    Cutting at RESETS ONLY, deliberately. A dropout mid-lap does not start a new
    lap, and splitting there would throw away the sectors measured BEFORE it --
    a 40 s hole in sector 3 would silently cost sectors 1 and 2, which were
    recorded perfectly well. _crossing_time refuses any individual pair too wide
    to interpolate, so the gates inside the hole come back None while every gate
    outside it still reports. That is the honest split: lose exactly what was
    lost.
    """
    clean = [(t, d) for t, d in samples if t is not None and d is not None]
    if len(clean) < 2:
        return []
    runs, cur = [], [clean[0]]
    for i in range(1, len(clean)):
        if clean[i][1] < clean[i - 1][1]:          # the lap trigger fired
            if len(cur) >= 2:
                runs.append(cur)
            cur = [clean[i]]
        else:
            cur.append(clean[i])
    if len(cur) >= 2:
        runs.append(cur)
    return runs


def _crossing_time(samples, boundary_m):
    """When the car passed `boundary_m`, interpolated between samples.

    The car reports about twice a second and sectors 4 and 6 are only ~100 m
    long. Snapping to the nearest sample would put several tenths into a number
    shown to 0.01 s.
    """
    for i in range(1, len(samples)):
        d0, d1 = samples[i - 1][1], samples[i][1]
        if d0 is None or d1 is None:
            continue
        if d0 <= boundary_m <= d1 and d1 > d0:
            t0, t1 = samples[i - 1][0], samples[i][0]
            if t1 - t0 > GATE_MAX_GAP_S:
                continue                      # a gap, not a crossing
            return t0 + (boundary_m - d0) / (d1 - d0) * (t1 - t0)
    return None


def _stitched(run, before, after):
    """`run` extended past both ends of the lap by one borrowed sample.

    Sector 1 opens at 0 m and sector 9 closes at 4000 m, and both of those are
    the start/finish line, where lap_distance_m resets. So neither gate has a
    straddling pair inside its own lap: the old code sidestepped sector 1 by
    starting it at the first sample AFTER the line, short by up to a sample
    period and all of it landing in the one cell most compared across laps, and
    it could not see sector 9's gate at all. Sector 9 reported only on laps
    where the car's distance happened to overshoot 4000 m before the reset,
    which is luck rather than design.

    Borrowing the neighbouring lap's adjacent sample and shifting it by a lap
    length puts a real straddling pair either side of the line, so all nine
    gates get the same interpolation. When a neighbour is missing the gate stays
    None: sector 9 of the lap in progress is unknowable until the lap rolls, and
    saying so is better than inventing it. F1 lands the final sector with the
    lap time for the same reason.
    """
    out = list(run)
    if before and run[0][1] > 0:
        t, d = before[-1]
        if run[0][0] - t <= GATE_MAX_GAP_S and 0 <= LAP_M - d <= STITCH_MAX_M:
            out.insert(0, (t, d - LAP_M))
    if after and run[-1][1] < LAP_M:
        t, d = after[0]
        if t - run[-1][0] <= GATE_MAX_GAP_S and 0 <= d <= STITCH_MAX_M:
            out.append((t, d + LAP_M))
    return out


def _splits(samples):
    """{sector id: seconds} for every gate pair this trace actually brackets."""
    if len(samples) < 2:
        return {}
    out = {}
    for sid, a, b in SECTOR_BOUNDS:
        t_in, t_out = _crossing_time(samples, a), _crossing_time(samples, b)
        if t_in is not None and t_out is not None and t_out > t_in:
            out[sid] = t_out - t_in
    return out


def _lap_runs(conn, tag, cache):
    """Runs for one lap tag, read at most once per request."""
    if tag not in cache:
        try:
            rows = db.fetch_lap_track(conn, tag)
        except Exception:
            rows = []
        cache[tag] = _runs([(r["device_ts"], r["lap_distance_m"]) for r in rows])
    return cache[tag]


def _lap_splits(conn, tag, cache):
    """The nine numbers for one lap tag, stitched to its neighbours.

    THE ONE definition of "this lap's splits", used by the grid, by the deltas
    and by the session-best fold, so purple can never be computed from a
    different number than the cell it colours. Returns (splits, run) where run
    is the trace the splits came from, or (empty, None) when there is none.
    """
    runs = _lap_runs(conn, tag, cache)
    if not runs:
        return {}, None
    run = runs[-1]
    if len(runs) > 1:
        before = runs[-2]                      # the stalled-counter case
    else:
        prev = _lap_runs(conn, tag - 1, cache)
        before = prev[-1] if prev else None
    nxt = _lap_runs(conn, tag + 1, cache)
    after = nxt[0] if nxt else None
    return _splits(_stitched(run, before, after)), run


# --- Session bests, the purple cell ---------------------------------------- #
# Held in process, not in app_state, because /api/sectors is a READ endpoint and
# this file's rule is that reads open the store mode=ro so a backend pointed at
# the live database during a race cannot corrupt it. Making the most-polled
# endpoint in the app a writer, to persist a value that is by definition
# reconstructible from stored telemetry, is the wrong trade. A backend restart
# costs about a minute of polling to rebuild these.
_BEST_LOCK = threading.Lock()
_BEST = {"race_start": None, "hi": None, "lo": None, "best": {}}

# Laps folded per request while catching up. A 400-lap race rebuilds in about a
# minute of polling, then costs one lap every few minutes forever after.
BEST_CHUNK_LAPS = 20


def _fold_bests(conn, race_start, newest_completed, cache):
    """Session best per sector, as {sid: (seconds, lap tag)}.

    Keyed on the race start time, which is the whole no-leak guarantee: a reset
    writes race_start_time None and a new start writes a new epoch, so the
    record is discarded without this endpoint needing to know that the reset
    endpoint exists.
    """
    with _BEST_LOCK:
        if _BEST["race_start"] != race_start:
            _BEST.update({"race_start": race_start, "hi": None, "lo": None,
                          "best": {}})

        def fold(tag):
            """True when `tag` predates the race and the walk should stop."""
            splits, run = _lap_splits(conn, tag, cache)
            if run is None:
                return False
            if race_start is None or run[0][0] < race_start:
                return True                   # practice: never owns purple
            for sid, secs in splits.items():
                if secs < IMPLAUSIBLY_FAST * REFERENCE_SPLITS.get(sid, 0.0):
                    continue                  # a glitch, not a lap record
                have = _BEST["best"].get(sid)
                if have is None or secs < have[0]:
                    _BEST["best"][sid] = (secs, tag)
            return False

        if _BEST["hi"] is None:
            fold(newest_completed)
            _BEST["hi"] = _BEST["lo"] = newest_completed
        else:
            while _BEST["hi"] < newest_completed:
                _BEST["hi"] += 1
                fold(_BEST["hi"])

        # Walk backwards a chunk at a time until the race's first lap.
        budget = BEST_CHUNK_LAPS
        while budget > 0 and _BEST["lo"] > 0:
            _BEST["lo"] -= 1
            budget -= 1
            if fold(_BEST["lo"]):
                _BEST["lo"] = 0               # reached the green flag
                break
        return dict(_BEST["best"])


def _row(kind, label, tag, splits, prev_splits, bests, position_m):
    """One rendered row: nine classified cells plus the lap total.

    The SERVER classifies. The browser is handed "best", "faster" or "slower"
    and maps them to purple, green and yellow. It never compares two numbers,
    which is this file's no-physics-in-JavaScript rule.
    """
    cells, total, complete = [], 0.0, True
    for sid, _a, b in SECTOR_BOUNDS:
        secs = splits.get(sid)
        prev = prev_splits.get(sid)
        delta = (secs - prev) if (secs is not None and prev is not None) else None
        best = bests.get(sid)

        if secs is None:
            complete = False
            # "Not there yet" and "we lost it" mean opposite things to a
            # strategist, so they must not render as the same dash.
            pending = position_m is not None and b > position_m
            cells.append({"sector": sid, "value": None, "delta": None,
                          "cls": None,
                          "state": "pending" if pending else "missing"})
            continue

        total += secs
        if best is not None and (best[1] == tag or secs < best[0] - 1e-9):
            # Identity, not a float comparison, so JSON round-tripping cannot
            # make purple flicker between polls.
            cls = "best"
        elif delta is None:
            cls = None
        elif delta < 0:
            cls = "faster"
        elif delta > 0:
            cls = "slower"
        else:
            cls = None
        cells.append({"sector": sid, "value": secs, "delta": delta,
                      "cls": cls, "state": "ok"})

    full_prev = len(prev_splits) == len(SECTOR_BOUNDS)
    prev_total = sum(prev_splits.values()) if full_prev else None
    return {
        "kind": kind, "label": label, "lap": tag, "cells": cells,
        # A partial sum is a wrong lap time, so it stays null until all nine
        # have landed.
        "total": total if complete else None,
        "totalDelta": (total - prev_total) if (complete and prev_total) else None,
    }


# Just under the 4 s poll, so every browser in the pit shares one read and
# nobody waits an extra cycle for a sample that has landed. The 8 s heavy TTL
# is wrong here: it would leave the CURRENT row up to 8 s stale, and the
# current row is the point of the feature.
SECTOR_CACHE_TTL_S = float(os.environ.get("SOLARRACE_SECTOR_TTL", "3.5"))


@app.get("/api/sectors")
def api_sectors():
    """Two rows of nine: the last completed lap, and the lap in progress.

    Before this, the grid showed only the lap IN PROGRESS, so most cells were
    dashes most of the time and a complete set of splits was on screen for
    about one poll before the lap rolled and cleared it.
    """
    with closing(ro_conn()) as conn:
        race = db.load_race_state(conn)
        start = race.get("race_start_time")
        if not (race.get("is_racing") and start):
            # Gated on the SERVER. The browser gets the race clock over a
            # different transport, so deciding here is the only way the section
            # and the splits cannot disagree for a tick. Costs one row read.
            return {"racing": False, "sectors": SECTOR_IDS, "rows": [],
                    "best": [], "note": "Sector times start with the race "
                                        "clock. Press Start race."}
        laps = db.recent_laps(conn, 4)
        return cached(("sectors", laps[0] if laps else None, start),
                      lambda: _build_sectors(conn, laps, start),
                      ttl=SECTOR_CACHE_TTL_S)


def _build_sectors(conn, laps, race_start):
    cache = {}
    if not laps:
        return {"racing": True, "sectors": SECTOR_IDS, "rows": [], "best": [],
                "note": "Waiting for the car to complete a sector."}

    cur_tag = laps[0]
    cur_splits, cur_run = _lap_splits(conn, cur_tag, cache)
    # The last sample of the lap in progress IS the car's position on it, so
    # "has the car reached this gate yet" costs nothing extra to answer.
    position_m = cur_run[-1][1] if cur_run else None

    last_tag = laps[1] if len(laps) > 1 else None
    last_splits, prev_splits = {}, {}
    if last_tag is not None:
        last_splits, _ = _lap_splits(conn, last_tag, cache)
        if len(laps) > 2:
            prev_splits, _ = _lap_splits(conn, laps[2], cache)

    bests = (_fold_bests(conn, race_start, last_tag, cache)
             if last_tag is not None else {})

    rows = []
    if last_tag is not None:
        rows.append(_row("last", "Last lap", last_tag, last_splits,
                         prev_splits, bests, None))
    rows.append(_row("current", "Current", cur_tag, cur_splits,
                     last_splits, bests, position_m))
    return {
        "racing": True, "sectors": SECTOR_IDS, "rows": rows, "note": None,
        "best": [{"sector": sid, "value": v[0], "lap": v[1]}
                 for sid, v in sorted(bests.items())],
    }


@app.get("/api/lap_track/{lap}")
def api_lap_track(lap: int):
    """GPS trace for one lap, so the map can draw the racing line.

    fetch_lap_track() deliberately selects only (device_ts, lap_distance_m) —
    it exists for sector timing and keeps that read cheap — so it has no
    lat/lon. Use it for the lap's TIME WINDOW, then read the full rows inside
    that window with fetch_samples(). Two existing helpers, no new SQL, and a
    lap is ~4 minutes at 1 Hz so the second read is a few hundred rows.
    """
    with closing(ro_conn()) as conn:
        track = db.fetch_lap_track(conn, lap)
        if not track:
            return {"points": []}
        t0, t1 = track[0]["device_ts"], track[-1]["device_ts"]
        rows = db.fetch_samples(conn, start_ts=t0, end_ts=t1)
    return {"points": [{"lat": r["lat"], "lon": r["lon"]}
                       for r in rows
                       if r["lat"] is not None and r["lon"] is not None]}


# --------------------------------------------------------------------------- #
# Weather / Strategy
# --------------------------------------------------------------------------- #
@app.get("/api/weather")
async def api_weather():
    """Solar irradiance forecast. Cached an hour inside weather_service.

    Bounded: weather_service's requests.get has no timeout, and on a pit LAN
    with a black-holed default route that call can hang for minutes. The pit
    LAN is offline by design, so "unavailable" must come back fast.
    """
    import pandas as pd
    from weather_service import fetch_zolder_weather
    try:
        df = await asyncio.wait_for(asyncio.to_thread(fetch_zolder_weather), 8.0)
    except asyncio.TimeoutError:
        df = None
    if df is None:
        return {"available": False, "rows": []}
    # Open-Meteo reports a missing hour as null, which pandas turns into NaN.
    # Send it back as null (shown as "—"), never as 0 and never as bare NaN,
    # which is not valid JSON.
    def val(x):
        return None if pd.isna(x) else x
    return {"available": True, "rows": [
        {"t": str(r["Time"]), "temp": val(r["Temp (°C)"]),
         "cloud": val(r["Cloud Cover (%)"]), "radiation": val(r["Solar Radiation (W/m²)"]),
         "rain": val(r["Rain (mm)"]), "rainChance": val(r["Rain Chance (%)"])}
        for _, r in df.iterrows()]}


# --------------------------------------------------------------------------- #
# Strategy matrix
# --------------------------------------------------------------------------- #
# ONE SIMULATION FEEDS BOTH THE TABLE AND THE CHART. Never two.
#
# strategy_engine plans each driving strategy once and attaches the full
# (minute, Wh) trace to the row it produced. This endpoint serves the rows and
# those traces in ONE payload, and the browser's chart only draws what it is
# handed -- it derives nothing, not even the floor line.
#
# That is not a style preference. The previous version of the engine had the
# chart re-simulating with its own private copies of the constants, and the two
# drifted: the table was computed from a tapering charge curve while the chart
# drew straight charging lines from a flat rate. The screen looked coherent and
# was lying. The combined graph used to be rendered here as a PNG for the same
# reason; now that the engine hands over the trace it drew from, the browser
# can draw it as a real, zoomable chart with no second implementation.
#
# The engine itself is the reference: `python Pit_Dashboard/strategy_engine.py`
# self-checks every plan against the race rules and checks the trace against
# the table. tools/check_strategy.py repeats those checks on THIS payload, so a
# mistake in the serialisation cannot pass on the engine's good name.

# Inputs are rounded before planning so the memo actually hits: the strategy
# tab polls every 10 s and the race clock moves every second, but a plan does
# not change meaningfully inside a minute or inside 50 Wh. Same numbers the
# earlier Streamlit app used.
STRATEGY_TIME_ROUND_MIN = 1.0
STRATEGY_WH_ROUND = 50.0

# Energy per lap: what the car MEASURED under a profile once it has driven
# enough laps of it to mean something, otherwise the stored estimate. Per
# profile, so a profile nobody has driven keeps its original number.
MIN_LAPS_FOR_MEASURED = C.MIN_LAPS_FOR_MEASURED
# The car finishes a lap about every 3.5 minutes, so nothing here can change
# faster than that. Measured on the pit's own store the grouped query is
# ~0.6 s, which must not run on every poll.
ENERGY_CACHE_S = 120


@memo(ttl=ENERGY_CACHE_S)
def _measured_energy_wh():
    """{strategy_key: (median_wh, n_laps)} from laps the car actually drove."""
    import statistics
    try:
        with closing(ro_conn()) as conn:
            raw = db.lap_energy_by_strategy(conn)
    except Exception:
        return {}
    return {k: (statistics.median(v), len(v))
            for k, v in raw.items() if len(v) >= MIN_LAPS_FOR_MEASURED}


@memo(ttl=90)
def _plan_strategies(time_left_min, battery_wh, active_lap, table_key):
    """The cached search. `table_key` is a tuple so it can be hashed."""
    consumption = [{"label": l, "lap_time_min": t, "energy_wh": w}
                   for l, t, w in table_key]
    return calculate_all_strategies(time_left_min, battery_wh, active_lap,
                                    consumption)


def _trace_json(plan):
    """One plan's simulation, exactly as the engine ran it, for the chart."""
    if not plan:
        return None
    return {
        "label": plan["label"],
        "laps": plan["laps"],
        "swaps": plan["swaps"],
        "lapTimeMin": plan["lap_time_min"],
        "totalTimeMin": plan["total_time_min"],
        "timeUsedMin": plan["time_used"],
        "pitMin": plan["pit_min"],
        "capacityWh": plan["capacity_wh"],
        "startWh": plan["start_wh"],
        "finalWh": plan["final_wh"],
        "stops": [{
            "number": s["number"], "afterLap": s["after_lap"],
            "atMin": s["at_min"], "socBefore": s["soc_before"],
            "socAfter": s["soc_after"], "chargeMin": s["charge_min"],
            "stopMin": s["stop_min"],
        } for s in plan["stops"]],
        # The (minute, Wh) curve, with what each point is: start | lap | swap
        # | stop | charge | hold. Charge segments are sampled along the real
        # curve, so they are genuinely concave.
        "points": [{"minute": t, "wh": wh, "kind": k}
                   for t, wh, k in plan["trace"]],
    }


def _strategy_payload(time_left_min, battery_wh, active_lap, table,
                      measured_note=None):
    """The whole Strategy screen for one set of inputs.

    Pure apart from the memo: tools/check_strategy.py calls this directly with
    the engine's own five scenarios and checks the served traces against the
    served rows, so the wire format is verified, not just the engine.
    """
    rounded_left = round(time_left_min / STRATEGY_TIME_ROUND_MIN) * STRATEGY_TIME_ROUND_MIN
    rounded_wh = round(battery_wh / STRATEGY_WH_ROUND) * STRATEGY_WH_ROUND
    table_key = tuple((r["label"], float(r["lap_time_min"]), float(r["energy_wh"]))
                      for r in table)
    rows = _plan_strategies(rounded_left, rounded_wh, int(active_lap or 0),
                            table_key)
    return {
        "rows": [{k: v for k, v in r.items() if k != "_graph_data"} for r in rows],
        # Index-aligned with rows. Null where a strategy could not plan at all.
        "traces": [_trace_json(r.get("_graph_data")) for r in rows],
        "floorWh": strategy_engine.BATTERY_FLOOR_WH,
        "capacityWh": strategy_engine.BATTERY_FULL_WH,
        "minStopMin": strategy_engine.MIN_STOP_DURATION_MIN,
        "maxStops": strategy_engine.MAX_STOPS,
        # Carried across from the engine so the screen shows its warning: the
        # curve's shape is right, its numbers
        # have never been checked against this charger or pack.
        "chargingCurveIsMeasured": strategy_engine.CHARGING_CURVE_IS_MEASURED,
        "measured": measured_note or {},
        "minLapsForMeasured": MIN_LAPS_FOR_MEASURED,
        "timeLeftMin": time_left_min,
    }


@app.get("/api/strategy")
def api_strategy(manual_lap: int = Query(-1)):
    with closing(ro_conn()) as conn:
        state, _ = read_live_state(conn)
        _, _, left_min = _race_clock(conn)
    soc = state["soc"]
    active_lap = manual_lap if manual_lap >= 0 else state["auto_lap"]
    # `not soc` covers both a missing reading and a reported 0: neither is a
    # usable capacity, so the matrix assumes a full pack rather than telling
    # the strategist the car is empty.
    battery_wh = (strategy_engine.BATTERY_FULL_WH if not soc
                  else (soc / 100.0) * strategy_engine.BATTERY_FULL_WH)

    measured = _measured_energy_wh()
    table, measured_note = [], {}
    for s in C.STRATEGIES:
        wh, n = measured.get(s["key"], (None, 0))
        if wh is None:
            wh = s.get("energy_wh")
        else:
            measured_note[s["label"]] = n
        if wh is not None:
            table.append({"label": s["label"], "lap_time_min": s["lap_time_min"],
                          "energy_wh": wh})

    out = _strategy_payload(left_min, battery_wh, active_lap, table, measured_note)
    out.update({
        "assumedFullPack": not soc,
        "missing": [n for n, v in (("battery SoC", soc), ("lap count", active_lap))
                    if v is None],
    })
    return out


# --------------------------------------------------------------------------- #
# Exports — the bytes come from export.py. Never rebuilt in JavaScript.
# --------------------------------------------------------------------------- #
@app.get("/api/export/history.csv")
def api_export_history_csv(metrics: str = Query("Speed"),
                           minutes: float | None = Query(None),
                           start: float | None = Query(None),
                           end: float | None = Query(None),
                           style: str = Query("data"),
                           session: str = Query("")):
    """History CSV in either flavour. export.py owns _safe() (formula-injection
    defence) and the utf-8-sig BOM Excel needs for °C and Ω."""
    import pandas as pd
    keys = [k for k in metrics.split(",") if k]
    chosen = [m for m in HISTORY_CHARTS if m.key in keys]
    if not chosen:
        raise HTTPException(404, "no known metric in %r" % metrics)
    with closing(ro_conn()) as conn:
        lo, hi = db.time_bounds(conn)
        start_ts = start if start is not None else (
            hi - minutes * 60.0 if (minutes and hi) else None)
        rows = db.fetch_samples(conn, start_ts=start_ts, end_ts=end)
    recs = []
    for r in rows:
        rec = {"Time": datetime.fromtimestamp(r["device_ts"]) if r["device_ts"] else None}
        for m in HISTORY_CHARTS:
            rec[m.key] = value_from_row(r, m)
        recs.append(rec)
    df = pd.DataFrame(recs, columns=["Time"] + [m.key for m in HISTORY_CHARTS])
    # export.history_csv_bytes unpacks the ORIGINAL 4-tuple shape
    # (`for _c, lbl, unit, _k in charts`). metrics.py carries 6-field
    # namedtuples here, which unpack as "too many values" — invisible until
    # somebody exports a CSV. Hand it the 4-tuples it expects.
    as_tuples = [(m.key, m.label, m.unit, m.color) for m in chosen]
    data = export.history_csv_bytes(df, as_tuples, style=style, session=session)
    stem = "history_" + "-".join(m.label.lower().replace(" ", "")
                                 for m in chosen[:3])
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             'attachment; filename="%s.csv"' % stem})


@app.get("/api/export/telemetry.xlsx")
def api_export_xlsx(start: float | None = Query(None),
                    end: float | None = Query(None),
                    groups: str = Query("")):
    """The 3-sheet workbook, straight from export.py. 588 lines of openpyxl,
    formula-injection defence and number formats — not rebuilt in JS."""
    names = [g for g in groups.split(",") if g] or list(export.METRIC_GROUPS)
    cols = export.metrics_for_groups(names)
    # write_xlsx takes a connection; to_xlsx_bytes does not, and opens its own
    # on db.SQLITE_PATH. With SOLARRACE_DB_PATH set that meant the bounds in
    # the sidebar came from one store and the workbook from another. Same file
    # on race day, but a demo or a test exported the wrong one.
    buf = io.BytesIO()
    with closing(ro_conn()) as conn:
        n = export.write_xlsx(buf, start_ts=start, end_ts=end, metrics=cols,
                              conn=conn)
    data = buf.getvalue()
    a = datetime.fromtimestamp(start) if start else datetime.now()
    b = datetime.fromtimestamp(end) if end else datetime.now()
    name = "telemetry_%s_%s.xlsx" % (a.strftime("%Y%m%d-%H%M"), b.strftime("%H%M"))
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="%s"' % name,
                 "X-Row-Count": str(n)})


@app.get("/api/export/estimate")
def api_export_estimate(start: float | None = Query(None),
                        end: float | None = Query(None)):
    """How many rows is this export, before committing to it?

    Nothing is sent until the whole workbook exists, so a full-race export is
    minutes of apparent silence. The sidebar uses this to say what it is about
    to ask for, instead of leaving the crew wondering whether they clicked it.

    NO DURATION IS RETURNED, deliberately. A cost model fitted to in-process
    builds was clean and linear, but over HTTP the same export is not
    reproducible -- 6,018 rows took 14.30, 5.97, 5.75 and 5.04 s back to back,
    and a 1,047-row export sometimes took LONGER than a 6,018-row one. A
    predicted time would be fiction the crew could plan around.

    Counted with db.count_samples_since twice rather than new SQL: everything
    from `start`, less everything after `end`.
    """
    with closing(ro_conn()) as conn:
        lo, _hi = db.time_bounds(conn)
        if lo is None:
            return {"rows": 0}
        rows = db.count_samples_since(conn, lo if start is None else start)
        if end is not None:
            # The helper counts >=, so anything at or past the instant just
            # after `end` is outside the range.
            rows -= db.count_samples_since(conn, end + 1e-6)
    rows = max(0, rows)
    return {"rows": rows}


@app.get("/api/export/bounds")
def api_export_bounds():
    with closing(ro_conn()) as conn:
        lo, hi = db.time_bounds(conn)
        total = db.count_samples(conn)
    return {"lo": lo, "hi": hi, "total": total,
            "groups": list(export.METRIC_GROUPS.keys())}


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
class RaceBody(BaseModel):
    isRacing: bool
    startTime: float | None = None


@app.post("/api/race")
def api_race(body: RaceBody):
    """Race clock, persisted so a browser refresh keeps a running race.

    A START with no startTime is stamped with the SERVER's clock. Elapsed time
    is computed against time.time() here, so a start stamped by a phone whose
    clock is minutes out would make the race clock wrong for everyone.

    An EXPLICIT startTime is for the case the pit actually hits: the race began
    at 12:00 and nobody reached the laptop until 12:20. Without it, pressing
    Start says the race began at 12:20 — and the error is not cosmetic, because
    lapDelta is (laps done - laps expected) and expected comes from elapsed
    time. Twenty missing minutes make the car read about 5.7 laps better than
    it is, which is the number the strategist acts on. The strategy matrix
    inherits the same error. Resume passes the stored value back, which is the
    same mechanism.

    A start time in the FUTURE is refused: it cannot be a real race start, and
    it would make elapsed negative and the countdown longer than the race.
    """
    start = body.startTime
    now = time.time()
    if body.isRacing and start is not None:
        # 60 s of slack for ordinary clock skew between the browser and here.
        if start > now + 60:
            raise HTTPException(
                400, "start time is in the future (%.0f s ahead); a race "
                     "cannot start later than now" % (start - now))
    if body.isRacing and start is None:
        start = now
    with closing(rw_conn()) as conn:
        before = db.load_race_state(conn)
        db.save_race_state(conn, body.isRacing, start)
        # The stint clock runs with the race clock and holds when it stops, so
        # a stoppage does not eat into a driver's two hours.
        if body.isRacing and start is not None:
            existing = load_app_state(conn, DRIVER_STINT_KEY) or {}
            old_start = before.get("race_start_time")
            # A stopped race started again at a DIFFERENT time is a new race,
            # not a resume (Resume passes the stored start back unchanged).
            # The old stint belongs to the old race: carrying its banked time
            # over is how a fresh race opens hundreds of hours overdue.
            new_race = not before.get("is_racing") and (
                old_start is None or abs(float(start) - float(old_start)) > 1.0)
            if not existing.get("started_at") or new_race:
                # Green flag with nobody logged: driver one is in the car, and
                # has been since the START — so a backdated race backdates the
                # stint with it. That errs toward the two-hour change reading
                # as DUE rather than hiding it, which is the safe direction:
                # over-counting a stint prompts a correction the crew can make
                # with "Driver changed", under-counting one loses a mandatory
                # change. If a swap already happened in the missed window, that
                # same button fixes it.
                # A name typed before any stint existed belongs to driver one;
                # a name left over from a previous race does not.
                save_app_state(conn, DRIVER_STINT_KEY, {
                    "started_at": start, "stint": 1,
                    "driver": (None if existing.get("started_at")
                               else existing.get("driver") or None),
                    "accumulated_s": 0.0, "running_since": start,
                })
            elif _stint_follows_race(existing, old_start):
                # CORRECTING the start of a race already running. The stint was
                # auto-started with the race and nothing has happened to it
                # since, so it began when the race did and has to move with it.
                # Leaving it behind is how a corrected race ends up with a
                # countdown that says an hour is left when the driver has
                # already done two.
                #
                # _stint_follows_race() is deliberately narrow: once a change
                # has been logged, the stint began at that change and a
                # correction to the race start must NOT touch it.
                save_app_state(conn, DRIVER_STINT_KEY, {
                    **existing, "started_at": start,
                    "accumulated_s": 0.0, "running_since": start,
                })
            elif float(existing["started_at"]) < start - 1.0:
                # A correction moved the race start past the moment this driver
                # got in. Nobody drives before the race begins, so the stint
                # began no earlier than the new start. Keep the stint number.
                save_app_state(conn, DRIVER_STINT_KEY, {
                    **existing, "started_at": start,
                    "accumulated_s": 0.0, "running_since": start,
                })
            else:
                # Resume. Idempotent, so a mid-race restart changes nothing.
                _set_stint_running(conn, True)
        elif not body.isRacing:
            _set_stint_running(conn, False)
        _kick_public_driver()
        return {**db.load_race_state(conn), "driverStint": driver_stint(conn)}


@app.post("/api/race/reset")
def api_race_reset():
    """Clear the race clock completely — for a race started by accident.

    "Stop race" deliberately KEEPS the start time so Resume works, which means
    there is otherwise no way back to "never started". This is that way back.

    It also clears the driver stint, because starting a race auto-starts stint
    one: leaving it behind would strand a phantom stint counting against a race
    that no longer exists. Both are saved first, so the whole thing is
    reversible for RACE_UNDO_S — a start time cannot be reconstructed by hand,
    so a mis-click here has to be takeable-back.
    """
    now = time.time()
    with closing(rw_conn()) as conn:
        race = db.load_race_state(conn)
        stint = load_app_state(conn, DRIVER_STINT_KEY) or {}
        elapsed_min = 0.0
        if race.get("race_start_time"):
            elapsed_min = max(0.0, (now - race["race_start_time"]) / 60.0)

        save_app_state(conn, RACE_UNDO_KEY,
                       {"at": now, "race": race, "stint": stint})
        db.save_race_state(conn, False, None)
        save_app_state(conn, DRIVER_STINT_KEY, {})
        _kick_public_driver()
        return {
            "ok": True,
            # What was thrown away, so the toast can say it and a mistake is
            # obvious immediately ("cleared a race that had run 3 h 12 m").
            "clearedElapsedMin": elapsed_min,
            "clearedWasRacing": bool(race.get("is_racing")),
            "clearedStint": int(stint.get("stint", 0) or 0),
            "race": db.load_race_state(conn),
            "driverStint": driver_stint(conn, now),
        }


@app.post("/api/race/reset/undo")
def api_race_reset_undo():
    """Put the race clock and the driver stint back as they were."""
    now = time.time()
    with closing(rw_conn()) as conn:
        u = race_undo_available(conn, now)
        if u is None:
            raise HTTPException(400, "nothing to undo")
        race = u.get("race") or {}
        db.save_race_state(conn, race.get("is_racing", False),
                           race.get("race_start_time"))
        save_app_state(conn, DRIVER_STINT_KEY, u.get("stint") or {})
        # One undo only: a second click must not resurrect a stale snapshot.
        save_app_state(conn, RACE_UNDO_KEY, {})
        # The stint clock has to match whatever the race clock now says.
        _set_stint_running(conn, bool(race.get("is_racing")), now)
        _kick_public_driver()
        return {"ok": True, "race": db.load_race_state(conn),
                "driverStint": driver_stint(conn, now)}


class StintBody(BaseModel):
    """Optional name of the driver getting IN. Empty is fine — the stint
    number alone is enough to keep count."""
    driver: str | None = None


@app.post("/api/driver_stint")
def api_driver_stint(body: StintBody):
    """Log a driver change and restart the countdown.

    The SERVER stamps the time, for the same reason the race clock does: a
    phone with a wrong clock must not skew the countdown for everyone.

    One click, no confirmation — this is pressed during a pit stop with people
    shouting. The cost of a mis-click is covered by the undo below, and the
    response carries the length of the stint just ended so a mistake is
    obvious immediately ("previous stint: 3 min").
    """
    now = time.time()
    with closing(rw_conn()) as conn:
        st = load_app_state(conn, DRIVER_STINT_KEY) or {}
        prev_started = st.get("started_at")
        # The new stint starts counting only if the race is actually running —
        # a swap during a red flag banks nothing until the green flag returns.
        racing = bool(db.load_race_state(conn).get("is_racing"))
        save_app_state(conn, DRIVER_STINT_KEY, {
            "started_at": now,
            "stint": int(st.get("stint", 0)) + 1,
            "driver": _clean_driver(body.driver),
            "accumulated_s": 0.0,
            "running_since": now if racing else None,
            # Everything needed to put it back exactly as it was.
            "previous_started_at": prev_started,
            "previous_stint": st.get("stint"),
            "previous_driver": st.get("driver"),
            "previous_accumulated_s": st.get("accumulated_s"),
            "previous_running_since": st.get("running_since"),
            # The RACE time the finished stint ran, which is what the toast
            # reports — wall time would overstate it across a stoppage.
            "previous_stint_s": _stint_elapsed(st, now) if prev_started else None,
        })
        _kick_public_driver()
        return driver_stint(conn, now)


def _clean_driver(name):
    return (name or "").strip()[:PUBLIC_DRIVER_MAX_LEN] or None


@app.post("/api/driver_stint/name")
def api_driver_stint_name(body: StintBody):
    """Name (or un-name, with an empty name) the driver in the car NOW.

    Touches nothing but the name: the countdown keeps running. Without this the
    only way to name driver one, who is started automatically by the green
    flag, would be "Driver changed", which restarts their two hours. Allowed
    before the race too; the name is then carried into stint one.
    """
    now = time.time()
    with closing(rw_conn()) as conn:
        st = load_app_state(conn, DRIVER_STINT_KEY) or {}
        st["driver"] = _clean_driver(body.driver)
        save_app_state(conn, DRIVER_STINT_KEY, st)
        _kick_public_driver()
        return driver_stint(conn, now)


@app.post("/api/driver_stint/undo")
def api_driver_stint_undo():
    """Put the previous stint back — for the mis-click during a pit stop.

    Restores the whole previous record, so an accidental change costs nothing.
    Refuses once there is nothing to restore rather than inventing a time.
    """
    now = time.time()
    with closing(rw_conn()) as conn:
        st = load_app_state(conn, DRIVER_STINT_KEY) or {}
        prev = st.get("previous_started_at")
        if not prev:
            raise HTTPException(400, "nothing to undo")
        restored = {
            "started_at": prev,
            "stint": st.get("previous_stint") or max(1, int(st.get("stint", 1)) - 1),
            "driver": st.get("previous_driver"),
        }
        # Put the banked/running split back as it was, then re-sync it to the
        # race clock — the race may have been started or stopped in between.
        if st.get("previous_accumulated_s") is not None:
            restored["accumulated_s"] = st["previous_accumulated_s"]
            restored["running_since"] = st.get("previous_running_since")
        save_app_state(conn, DRIVER_STINT_KEY, restored)
        _set_stint_running(conn, bool(db.load_race_state(conn).get("is_racing")), now)
        _kick_public_driver()
        return driver_stint(conn, now)


class MessageBody(BaseModel):
    category: str = ""
    value: str | float | int = ""


@app.post("/api/driver_message")
def api_driver_message(body: MessageBody):
    """The pit wall's ONLY Firebase write. Everything else is SQLite."""
    import driver_message
    try:
        driver_message.send_driver_command(body.category, body.value)
    except Exception as e:
        raise HTTPException(502, "send failed: %s" % e)
    return {"ok": True, "shown": ("%s: %s" % (body.category, body.value)
                                  if body.category else str(body.value))}


@app.delete("/api/driver_message")
def api_clear_message():
    import driver_message
    try:
        driver_message.clear_driver_command()
    except Exception as e:
        raise HTTPException(502, "clear failed: %s" % e)
    return {"ok": True}


@app.post("/api/cut_lap")
def api_cut_lap():
    """Ask the CAR to close its lap (snapshots lap energy + time). Does not
    change the manual lap override."""
    import driver_message
    try:
        driver_message.send_lap_cut()
    except Exception as e:
        raise HTTPException(502, "cut lap failed: %s" % e)
    return {"ok": True, "sentAt": time.strftime("%H:%M:%S")}


@app.get("/api/cut_lap/ack")
def api_cut_lap_ack():
    """The car's acknowledgement. "Sent" and "the car is running it" are not
    the same thing, so the pit sees which one it has."""
    import driver_message
    try:
        return {"ack": driver_message.read_lap_ack()}
    except Exception as e:
        return {"ack": None, "error": str(e)}


class StrategyBody(BaseModel):
    key: str


@app.post("/api/strategy/select")
def api_strategy_select(body: StrategyBody):
    """Only the strategy NAME goes over the link: the car already holds all
    five generated profiles, so this is a few bytes rather than a 400-row
    table, and the profile it flies is the one committed to git."""
    if not any(s["key"] == body.key for s in C.STRATEGIES):
        raise HTTPException(400, "unknown strategy %r" % body.key)
    import driver_message
    try:
        driver_message.send_strategy(body.key)
    except Exception as e:
        raise HTTPException(502, "send failed: %s" % e)
    return {"ok": True, "key": body.key}


@app.get("/api/strategy/ack")
def api_strategy_ack():
    import driver_message
    try:
        return {"ack": driver_message.read_strategy_ack()}
    except Exception as e:
        return {"ack": None, "error": str(e)}


class ClearBody(BaseModel):
    # A typed confirmation phrase, not a boolean. clear_history is irreversible
    # and sat behind a popover-plus-confirm in the Streamlit dashboard precisely
    # so it could not be hit mid-race; a stray POST from a phone in someone's
    # pocket must not be able to wipe the store. The UI makes the user type this.
    confirm: str


CLEAR_PHRASE = "DELETE HISTORY"


@app.post("/api/clear_history")
def api_clear_history(body: ClearBody):
    if body.confirm != CLEAR_PHRASE:
        raise HTTPException(
            400, "clear_history requires confirm=%r" % CLEAR_PHRASE)
    with closing(rw_conn()) as conn:
        return {"ok": True, "deleted": db.clear_history(conn)}


# --------------------------------------------------------------------------- #
# WebSocket — the 2 s fast tier
# --------------------------------------------------------------------------- #
FAST_TICK_S = float(os.environ.get("SOLARRACE_FAST_TICK", "2.0"))

# Never push the fast tier more often than this, however fast a client asks.
# FAST_TICK_S above is the normal cadence; a real change to the manual lap
# override is allowed to jump the queue, and this is the floor that bounds
# that exception -- including a client that changes it on every message.
LIVE_MIN_PUSH_S = float(os.environ.get("SOLARRACE_LIVE_MIN_PUSH", "0.25"))


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    """Pushes the whole fast tier every FAST_TICK_S: tiles, faults, sectors, map.

    2 s is the cadence the earlier Streamlit app arrived at after the page kept
    stalling, and it is what the car reports at. Faster buys nothing.

    THE CADENCE IS THE SERVER'S, and no client can talk it out of it. This used
    to send, then wait UP TO FAST_TICK_S for a client message before sending
    again -- and the browser answered every push with its parameters, so the
    wait always ended at once and the two ran flat out. Measured in a real
    browser that was 30 payloads a second on Driver Telemetry and 134 a second
    on History, ~10 KB each, up to 1.4 MB/s per open device: enough to saturate
    the browser's main thread and skew the race clock's reading of server time.

    The client no longer echoes, but fixing only the client would not be enough
    on race day -- a phone still holding an older cached bundle would keep the
    storm going. So reading and sending are separate. The reader drains
    whatever arrives and only disturbs the cadence when the override ACTUALLY
    changes; an echo carrying the same value changes nothing.

    A real change pushes at once, so the override is never stuck behind a
    two-second wait, but never faster than LIVE_MIN_PUSH_S.

    tools/check_live_cadence.py attacks this with exactly those clients.
    """
    await ws.accept()
    manual_lap = -1
    changed = asyncio.Event()
    gone = False

    async def reader():
        """Drain client messages; wake the pusher only on a REAL change."""
        nonlocal manual_lap, gone
        try:
            while True:
                msg = await ws.receive_json()
                try:
                    want = int(msg.get("manualLap", manual_lap))
                except (ValueError, TypeError, AttributeError):
                    continue          # junk from a client is not worth dying over
                if want != manual_lap:
                    manual_lap = want
                    changed.set()
        except (WebSocketDisconnect, RuntimeError, ValueError):
            gone = True
            changed.set()             # do not let the pusher wait out a whole tick

    reader_task = asyncio.create_task(reader())
    try:
        while not gone:
            # Cleared BEFORE the read, so a change arriving while this builds or
            # sends is not lost: it simply skips the wait below.
            changed.clear()

            def read():
                with closing(ro_conn()) as conn:
                    return build_live(conn, manual_lap)

            # SQLite reads block; keep them off the event loop so one slow read
            # cannot stall every other socket.
            await ws.send_json(await asyncio.to_thread(read))
            sent_at = time.monotonic()
            try:
                await asyncio.wait_for(changed.wait(), timeout=FAST_TICK_S)
            except asyncio.TimeoutError:
                continue              # the ordinary tick
            if gone:
                break
            rest = LIVE_MIN_PUSH_S - (time.monotonic() - sent_at)
            if rest > 0:
                await asyncio.sleep(rest)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        reader_task.cancel()


@app.websocket("/ws/history")
async def ws_history(ws: WebSocket):
    """Incremental history appends — never the whole series.

    That is the entire performance argument for the rewrite: the client pushes
    new points into the existing chart with extendTraces, so a zoom survives.
    """
    await ws.accept()
    try:
        hello = await ws.receive_json()
    except (WebSocketDisconnect, ValueError):
        return
    keys = hello.get("metrics") or ["Speed"]
    chosen = [m for m in HISTORY_CHARTS if m.key in keys]
    cursor = hello.get("cursor")
    try:
        while True:
            await asyncio.sleep(HISTORY_TICK_S)

            def read(since):
                with closing(ro_conn()) as conn:
                    if since is None:
                        # No cursor at all: resume from the newest sample, never
                        # from the start of the store.
                        _, since = db.time_bounds(conn)
                        if since is None:
                            return []
                    # +epsilon so the cursor row is not resent; db.py filters >=.
                    rows = db.fetch_samples(conn, start_ts=since + 1e-6)
                # NOT fetch_samples(limit=N): that helper returns the most RECENT
                # N rows, which in replay would skip to the end of the store
                # instead of walking forward. Slice the oldest N in Python.
                if REPLAY:
                    return rows[:REPLAY_BATCH]
                # A catch-up burst (the collector paging in thousands of rows
                # after a gap) must not arrive as one enormous append — the
                # chart was thinned to ~4k points on load, and a raw 5,000-row
                # burst would triple its density in one tick. Thin big bursts
                # to the same order as a normal tick; the next /api/history
                # load re-thins the whole window evenly anyway.
                if len(rows) > APPEND_BURST_MAX:
                    stride = len(rows) // APPEND_BURST_MAX + 1
                    rows = rows[::stride]
                return rows
            rows = await asyncio.to_thread(read, cursor)
            if not rows:
                continue
            cursor = rows[-1]["device_ts"]
            await ws.send_json({
                "type": "append",
                "t": [_iso(r["device_ts"]) for r in rows],
                "series": {m.key: [value_from_row(r, m) for r in rows]
                           for m in chosen},
                "cursor": cursor,
            })
    except (WebSocketDisconnect, RuntimeError):
        return


HISTORY_TICK_S = float(os.environ.get("SOLARRACE_HISTORY_TICK", "10.0"))
APPEND_BURST_MAX = int(os.environ.get("SOLARRACE_APPEND_BURST_MAX", "600"))


# --------------------------------------------------------------------------- #
# Static: FastAPI serves the production React build, so the pit machine needs
# Python only — no Node, no dev server, no npm install at the track.
# --------------------------------------------------------------------------- #
_DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "frontend", "dist")
if os.path.isdir(_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(_DIST, "assets")),
              name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        """Everything not under /api or /ws is the single-page app."""
        candidate = os.path.join(_DIST, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(_DIST, "index.html"))
