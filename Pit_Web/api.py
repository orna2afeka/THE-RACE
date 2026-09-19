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
# THE lap clock, shared with the pit wall so the TV and this dashboard cannot
# show two different lap times. _lap_clock() below is the thin reader that
# hands it what the web backend has already read.
from lap_clock import (                                # noqa: E402
    LAP_DATUM_KEY, LAP_HOLD_KEY, lap_clock)
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
import energy_model                                    # noqa: E402
from strategy_engine import (                          # noqa: E402
    calculate_all_strategies, load_velocity_profile,
    get_live_track_status, profile_to_df, SECTIONS_INFO,
)
import speed_profile                                   # noqa: E402
import cell_extremes                                   # noqa: E402
from memo import memo                                  # noqa: E402

DB_PATH = os.environ.get("SOLARRACE_DB_PATH") or SQLITE_PATH

# Is this backend pointed somewhere OTHER than the pit's real store? That is
# the demo dashboard (Start Demo Dashboard.bat sets SOLARRACE_DB_PATH to
# demo_telemetry.db), or a backend opened on an archived copy.
#
# Two things hang off it, and both are the same rule: nothing made up may be
# mistaken for the car. The public driver name is never published from here,
# and the Strategy screen's typed-in inputs (SoC, time remaining) exist ONLY
# here — on the real dashboard the plan is always the car's own numbers.
DEMO_STORE = os.path.abspath(DB_PATH) != os.path.abspath(SQLITE_PATH)

# What a demo dashboard is told when it tries to command the car.
CAR_LINK_REFUSED = (
    "This dashboard is running on a demo store, so it is not connected to the "
    "car. The command was NOT sent. Use the real pit dashboard (run_web.bat) "
    "to command the car."
)


def car_link():
    """The car's Firebase channel — or a refusal, off the pit's real store.

    THE SANDBOX BOUNDARY, and the reason it is a function rather than a check
    repeated at a dozen endpoints. Every `import driver_message` in a request
    handler goes through here.

    The demo dashboard reads a DIFFERENT SQLite file, but driver_message talks
    to Firebase, which has only one of everything: one /lap_command, one
    /driver_command, one /strategy_command. Pointing the backend at
    demo_telemetry.db does nothing to that, so before this existed every send
    button on the demo reached the REAL CAR. The green flag was the worst of
    them: "Start race" sends new_race, which zeroes the car's laps, distance
    and energy and rewrites its checkpoint file.

    That also reached the PUBLIC PAGE. docs/index.html on GitHub Pages plots
    /public/live, which the car itself writes — so a demo that resets the car
    resets what every spectator is watching. Nothing on a demo dashboard is
    allowed near it.

    Raises 409 rather than returning None so a handler cannot forget to check:
    the request fails, loudly, before anything local is written.
    """
    if DEMO_STORE:
        raise HTTPException(409, CAR_LINK_REFUSED)
    import driver_message
    return driver_message


def car_link_ro():
    """Same boundary for the ACK POLLS, which read rather than command.

    None on a demo store, and the caller serves a null ack. Not a 409: these
    are polled every few seconds by every open tab, and a demo that painted an
    error toast twice a minute would be read as a broken demo. There is also
    nothing to report — a demo sends no commands, so it is owed no acks, and
    showing the real car's answer to somebody else's command would be worse
    than showing none.
    """
    if DEMO_STORE:
        return None
    import driver_message
    return driver_message

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
    # 1/0/None: is a charger on the car (charge_detector.py). Carried forward
    # like any other reading, which is right here because the car sends it on
    # EVERY sample -- so a carried value only ever appears for rows from a
    # build that predates the field, and reads None, not a confident 0.
    "is_charging": "is_charging",
    "last_lap_time_s": "last_lap_time_s", "lap_distance_m": "lap_distance_m",
    # The car's own lap tags (gate-based tracker). last_lap_kind and
    # last_lap_stopped_s describe a FINISHED lap, which stays true however old
    # the row is, so they carry forward. current_lap too.
    "last_lap_kind": "last_lap_kind", "last_lap_flags": "last_lap_flags",
    "last_lap_stopped_s": "last_lap_stopped_s", "current_lap": "current_lap",
    # Wall clock the lap being driven began, from the car. Carries forward for
    # the same reason current_lap does: it stays true for the whole lap, and a
    # lap clock that blanked between samples would be unreadable.
    "lap_started_ts": "lap_started_ts",
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
#   zone and track_pos_m answer "where is the car NOW"; a carried-forward
#   "track" while the car sits in its box is a wrong answer, not an old one.
_NO_CARRY_FORWARD = {"lap_source", "zone", "track_pos_m"}


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
        "zone": None, "track_pos_m": None,
        # lat/lon fall back to the Zolder paddock so the map has somewhere to
        # centre. has_gps says whether the pin is REAL: 0,0 is a real place in
        # the Atlantic, and a placeholder must never be mistakable for a fix.
        "lat": 50.9895, "lon": 5.2568, "has_gps": False,
        # No row at all: no position, no age, no last-fix instant. Nothing here
        # may default to a number -- "0 s ago" would read as a live fix.
        "has_gps_point": False, "gps_age_s": None, "gps_fix_ts": None,
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
    state["zone"] = _val(row, "zone", None)
    state["track_pos_m"] = _val(row, "track_pos_m", None)

    # Prefer the zone the CAR classified — what the driver's bar actually
    # showed. Fall back to classifying here only for rows written before the
    # column existed; efficiency.zone() is the same function the car ran.
    zone = cf("throttle_zone", "mms_throttle_zone")
    state["throttle_zone"] = zone or efficiency.zone(state["throttle_pct"])

    # Regeneration, the other half of the one-pedal control. DERIVED HERE from
    # the raw millivolts rather than stored: the car publishes it, but adding a
    # column would leave every row recorded before today blank, and
    # efficiency.regen_percent() is the same function the car ran on the same
    # number. None stays None — a pedal that never reported has no regen
    # reading either, and must not render as 0 %.
    state["regen_pct"] = efficiency.regen_percent(state["throttle_mv"])[0]

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
    # POSITION, AND WHETHER IT IS LIVE.
    #
    # has_gps used to mean "the row has a lat", which is not the same question.
    # The car keeps serving its last known fix after the receiver loses lock
    # (gps_reader: a frozen dot beats an empty map on the driver's screen), so
    # every row carries a well-formed position for as long as the car stays
    # lost. On 2026-09-18 that was 57 minutes, with the car driving.
    #
    # gps_age_s is read with _val, never carried forward: like the health
    # columns it answers "what is true right now", and a carried-forward age
    # would be doubly stale.
    gps_age = _val(row, "gps_age_s", None)
    have_point = (_val(row, "lat", None) is not None
                  and _val(row, "lon", None) is not None)
    # A car whose build predates gps_age_s sends no age at all. Treat that as
    # live-if-present, exactly as before, rather than blanking the map of every
    # team member still running last week's image -- the same courtesy
    # car_health() extends to a car that predates the heartbeat.
    state["gps_age_s"] = gps_age
    state["has_gps"] = have_point and (gps_age is None
                                       or gps_age <= limits.GPS_LIVE_MAX_AGE_S)
    # Wall-clock instant of the last usable fix, in the CAR's clock -- the same
    # clock device_ts is in, so the subtraction is exact and no skew between the
    # car and the pit can creep in. None when we cannot say.
    device_ts_raw = row["device_ts"]
    state["gps_fix_ts"] = (device_ts_raw - gps_age
                           if (device_ts_raw and gps_age is not None) else None)
    # The point itself is still handed over when it is old: the map draws it
    # dimmed as "last seen here", which is more use to the crew than an empty
    # map. has_gps is what says whether to believe it.
    state["lat"] = _val(row, "lat", 50.9895)
    state["lon"] = _val(row, "lon", 5.2568)
    state["has_gps_point"] = have_point
    state["_field_ages"] = field_ages

    device_ts = row["device_ts"]
    return state, ((now - device_ts) if device_ts else None)


def _race_clock(conn):
    r = db.load_race_state(conn)
    elapsed_min = 0.0
    if r["is_racing"] and r["race_start_time"]:
        elapsed_min = (time.time() - r["race_start_time"]) / 60.0
    return r, elapsed_min, max(0.0, strategy_engine.RACE_DURATION_MIN
                               - elapsed_min)


# --------------------------------------------------------------------------- #
# The charging clock
#
# Two limits sit on a charge, and until now both lived in somebody's head: the
# crew's own ceiling of an hour on the charger (strategy_engine
# MAX_STOP_DURATION_MIN -- the planner already refuses to plan past it), and the
# REGULATION that a car charging more than MAX_STOPS times is classified behind
# every car that did not. This counts down the first and counts the second.
#
# THE PIT OWNS THIS CLOCK, NOT THE CAR, for one reason: the charge detector
# reads CAN current and RPM, so a car switched OFF on the charger reports
# nothing at all. A clock driven by the car alone would miss exactly the stops
# where someone saved power by turning the car off. So:
#
#   STARTS   when the car reports is_charging, or when the pit presses Start.
#   RUNS     through any silence. A car that stops talking mid-charge has not
#            stopped charging -- this is the whole reason the pit owns it.
#   STOPS    when the car is heard MOVING again, or when the pit presses Stop.
#            Not on is_charging going to 0: a car switched on at the end of a
#            charge reports 0 for the detector's confirm window before it
#            reports anything else, and that must not end the clock early.
#
# The count belongs to the RACE: it is keyed on the race start like the sector
# bests are, so a new race opens at zero without this needing to know that the
# reset endpoint exists. "Discard" is there because the count is regulation,
# and a press by mistake must not cost one of three.
# --------------------------------------------------------------------------- #
CHARGE_CLOCK_KEY = "charge_clock"
CHARGE_LIMIT_S = strategy_engine.MAX_STOP_DURATION_MIN * 60.0
CHARGE_WARN_LEFT_S = 10 * 60.0          # amber from here down
CHARGE_MOVING_KMH = 5.0                 # the car has left the box
CHARGE_WATCH_S = 2.0


def _charge_record(conn):
    """The stored record, reset to empty when it belongs to another race."""
    rec = load_app_state(conn, CHARGE_CLOCK_KEY) or {}
    race_start = db.load_race_state(conn).get("race_start_time")
    if rec.get("race_start") != race_start:
        rec = {"race_start": race_start, "count": 0}
    return rec


def charge_clock(conn):
    """The charging clock as the header and the sidebar show it."""
    rec = _charge_record(conn)
    return {
        "active": rec.get("started_at") is not None,
        "startedAt": rec.get("started_at"),
        "startedBy": rec.get("started_by"),
        "limitS": CHARGE_LIMIT_S,
        "warnLeftS": CHARGE_WARN_LEFT_S,
        "count": int(rec.get("count") or 0),
        "maxStops": strategy_engine.MAX_STOPS,
        "lastDurationS": rec.get("last_duration_s"),
    }


def _charge_start(conn, by, now=None):
    rec = _charge_record(conn)
    if rec.get("started_at") is not None:
        return False                      # already running: never restart it
    now = time.time() if now is None else now
    rec.update({"started_at": now, "started_by": by,
                "count": int(rec.get("count") or 0) + 1})
    save_app_state(conn, CHARGE_CLOCK_KEY, rec)
    return True


def _charge_stop(conn, now=None, discard=False):
    rec = _charge_record(conn)
    if rec.get("started_at") is None:
        return False
    now = time.time() if now is None else now
    if discard:
        # Not a charge: give the count back and leave no duration behind.
        rec["count"] = max(0, int(rec.get("count") or 0) - 1)
    else:
        rec["last_duration_s"] = max(0.0, now - float(rec["started_at"]))
    rec["started_at"] = None
    rec["started_by"] = None
    save_app_state(conn, CHARGE_CLOCK_KEY, rec)
    return True


# A gap this long inside a run of is_charging samples ends the run: the car was
# off or out of contact, and what came before may be a different plug-in.
CHARGE_SPELL_GAP_S = 120.0


def _charging_since(conn):
    """The instant the car's CURRENT unbroken run of is_charging=1 began.

    Walks back from the newest sample and stops at the first row that is not
    charging, or at a silence longer than CHARGE_SPELL_GAP_S. Bounded to twice
    the limit: a charge older than that is over the hour whatever it reads.
    """
    floor = time.time() - 2 * CHARGE_LIMIT_S
    since = prev = None
    for ts, ch in conn.execute(
            "SELECT device_ts, is_charging FROM telemetry "
            "WHERE device_id = ? AND device_ts >= ? ORDER BY device_ts DESC",
            (db.DEVICE_ID, floor)):
        if ch != 1 or (prev is not None and prev - ts > CHARGE_SPELL_GAP_S):
            break
        since = prev = ts
    return since if since is not None else time.time()


def charge_watch_once():
    """One look at the car: start on is_charging, stop on movement.

    Only a FRESH sample may do either. An hour-old backlog row that says the
    car was charging is history arriving late, not a plug going in now.
    """
    with closing(ro_conn()) as conn:
        state, age = read_live_state(conn)
        active = _charge_record(conn).get("started_at") is not None
    if age is None or age > C.DATA_STALE_AFTER_S:
        return None
    speed = state.get("speed_kmh")
    if not active and state.get("is_charging") == 1:
        with closing(rw_conn()) as conn:
            # FROM WHEN THE CAR SAYS THE CHARGE BEGAN, not from when this loop
            # noticed. They differ whenever Pit Web was restarted mid-charge or
            # the samples arrived late, and "now" would under-read the one
            # number this clock exists to get right.
            changed = _charge_start(conn, "car", now=_charging_since(conn))
    elif active and speed is not None and speed > CHARGE_MOVING_KMH:
        with closing(rw_conn()) as conn:
            changed = _charge_stop(conn)
    else:
        return False
    if changed:
        nudge_live()
    return changed


def _charge_watch_loop():
    while True:
        try:
            charge_watch_once()
        except Exception as e:                               # noqa: BLE001
            print("[charge clock] watch failed, will retry: %s" % e, flush=True)
        time.sleep(CHARGE_WATCH_S)


def race_lap_floor(conn):
    """The instant the pit's lap views start from, or None for the whole store.

    THE GREEN FLAG. Every list of LAPS is bounded by this -- /api/laps, and the
    per-lap workbook through export.py -- because a race start zeroes the car's
    lap counter, so laps from before it repeat the numbers of laps after it.

    The race START, not "is a race running": a race that has been stopped still
    happened, and its laps are still the ones worth looking at afterwards. It
    is cleared only by a race reset, which is the pit saying the race never
    began.

    Samples are NOT bounded by this. The History charts, the time-ranged
    workbook and the store itself keep everything the car ever sent; this is
    about which laps are THIS RACE's, not about what is worth keeping.
    """
    return db.load_race_state(conn).get("race_start_time")


DRIVER_STINT_KEY = "driver_stint"
RACE_UNDO_KEY = "race_undo"
STRATEGY_CHOICE_KEY = "strategy_choice"
# LAP_HOLD_KEY and LAP_DATUM_KEY are imported from lap_clock.py, where the rule
# that reads them lives. This file still owns the WRITES -- both are stamped by
# the endpoints at the bottom -- but the pit wall reads them too, and a key
# name spelled in two places is a screen that silently never sees a press.


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
    base["publicSynced"] = public_driver_synced(_public_driver_state(st))
    # When the pit said the swap began, or None. NOT auto-expired anywhere:
    # it stays until somebody clears it, by choice — see the endpoint below.
    base["changeStartedAt"] = st.get("change_started_at") or None
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


# --- The stint log: who drove, and between which two instants --------------- #
# The countdown only ever needed the CURRENT driver, so that is all the record
# held — and a lap finished an hour ago had no way back to a name. `log` is the
# list of stints already FINISHED, appended to at each driver change; the stint
# in progress is the record itself and is deliberately NOT in the list, so
# "Name current driver" has one place to write and nothing to keep in sync.
#
# It rides inside the same app_state record on purpose. Everything that already
# takes the record as a whole — the race-reset snapshot, its undo, the
# new-race branch that starts from a bare dict — then carries the log with it,
# correctly, without knowing it exists. A separate table would have had to be
# taught each of those cases one at a time.
#
# db.load_driver_stints() reads it back for /api/laps and for the workbook.
def _stint_log(st, ended_at):
    """`st`'s log with the stint it describes closed at `ended_at`.

    A record with no started_at is a race that never began — nothing to close,
    so the log passes through unchanged.
    """
    log = list(st.get("log") or [])
    if st.get("started_at") is not None:
        entry = {"stint": st.get("stint"), "driver": st.get("driver") or None,
                 "started_at": st["started_at"], "ended_at": ended_at}
        # Every name the stint carried and from when -- see api_driver_stint_name
        # and db._stint_intervals. Absent on a stint that only ever had one.
        if st.get("names"):
            entry["names"] = list(st["names"])
        log.append(entry)
    return log


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
PUBLIC_DRIVER_ENABLED = not DEMO_STORE
PUBLIC_DRIVER_RESYNC_S = 15
PUBLIC_DRIVER_MAX_LEN = 40
_NOT_SENT = object()
_public_driver_sent = _NOT_SENT
_public_driver_lock = threading.Lock()
_public_driver_wake = threading.Event()


# THE SPECTATOR ESTIMATE. While the car is out of contact the pit can ask the
# public page to show where it SHOULD be at race pace: {"startedAt", "lap",
# "distM"} in app_state, mirrored onto /public/driver by the loop below. The
# page walks the marker round the 4:40 profile from that instant and marks
# every figure it produces as estimated (tools/build_zolder_animation.py).
#
# Started by a press and by nothing else, and ENDED BY THE CAR: the first fresh
# sample clears it (sync_public_driver), so an anchor typed at 14:00 cannot
# come back to life at the next dropout an hour later and put the marker, and
# the lap count, somewhere nobody chose.
PUBLIC_ESTIMATE_KEY = "public_estimate"
_public_estimate_sent = None


def _public_estimate(conn):
    """The estimate the page should be showing, or None."""
    e = load_app_state(conn, PUBLIC_ESTIMATE_KEY) or {}
    if e.get("startedAt") is None or e.get("lap") is None or e.get("distM") is None:
        return None
    return {"startedAt": float(e["startedAt"]), "lap": int(e["lap"]),
            "distM": float(e["distM"])}


# THE PIT'S OWN LINE ON THE PUBLIC PAGE. One short sentence the crew types --
# "Changing tyres" -- shown to everyone with the URL until they take it down.
#
# WHY IT IS NOT A LIST OF CANNED REASONS. The page can already say the two
# things the system KNOWS: a driver change, and the charger. This is for
# everything else, and everything else is not a list anyone can write in
# advance -- a puncture, a controller swap, scrutineering, a red flag. The pit
# knows what is happening and can type it in five words.
#
# IT SAYS WHAT THE PIT TYPED, and nothing else: no inference, no expiry. Like
# the driver-change flag, it stays until it is cleared, and the page prints how
# long it has been up so a note left on after the car has gone back out is
# visible rather than quietly wrong.
#
# EVERYTHING HERE IS PUBLIC. The cap and the one-line rule are applied at the
# door, in _clean_note, so nothing downstream has to wonder: the page renders
# it with textContent, and this never sees markup worth escaping.
PUBLIC_NOTE_KEY = "public_note"
PUBLIC_NOTE_MAX_LEN = 48
_public_note_sent = None


def _clean_note(text):
    """One line, no runs of whitespace, no longer than a phone can show."""
    return " ".join((text or "").split())[:PUBLIC_NOTE_MAX_LEN].strip()


def _public_note(conn):
    """{"text", "since"} the page should be showing, or None."""
    n = load_app_state(conn, PUBLIC_NOTE_KEY) or {}
    text = _clean_note(n.get("text"))
    if not text:
        return None
    since = n.get("since")
    return {"text": text, "since": float(since) if since else None}


def _public_driver_state(st):
    """What /public/driver should say for stint record `st`: (name, since).

    `since` is when the pit said a driver change began, or None. The pair is
    what gets compared and published, so a change that starts while the name
    is unchanged is still a difference worth a write.
    """
    return (st.get("driver") or None, st.get("change_started_at") or None)


def public_driver_synced(state):
    """True when the public page shows `state` — the (name, since) pair above.
    False while a write is pending or failing, None when publishing is off."""
    if not PUBLIC_DRIVER_ENABLED:
        return None
    return _public_driver_sent is not _NOT_SENT and _public_driver_sent == state


def sync_public_driver():
    """Make /public/driver match the current stint. Returns True when in sync."""
    global _public_driver_sent, _public_estimate_sent, _public_note_sent
    if not PUBLIC_DRIVER_ENABLED:
        return None
    with _public_driver_lock:
        try:
            with closing(ro_conn()) as conn:
                st = load_app_state(conn, DRIVER_STINT_KEY) or {}
                est = _public_estimate(conn)
                note = _public_note(conn)
                _state, age = read_live_state(conn) if est else (None, None)
            # THE CAR IS BACK, so the estimate is over. `age` is measured on the
            # car's own sample time, so a collector paging through an hour-old
            # backlog does not count as the car being heard -- only a sample
            # that is current does.
            if est and age is not None and age <= C.DATA_STALE_AFTER_S:
                with closing(rw_conn()) as conn:
                    save_app_state(conn, PUBLIC_ESTIMATE_KEY, {})
                est = None
            state = _public_driver_state(st)
            if (_public_driver_sent is not _NOT_SENT
                    and _public_driver_sent == state
                    and _public_estimate_sent == est
                    and _public_note_sent == note):
                return True
            import driver_message
            driver_message.publish_driver_name(state[0], changing_since=state[1],
                                               estimate=est, note=note)
            _public_driver_sent = state
            _public_estimate_sent = est
            _public_note_sent = note
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


# How often the OAuth token used to send commands to the car is topped up.
# Under google-auth's own 300 s refresh threshold, so a press never finds an
# expired one. See _token_warm_loop.
TOKEN_WARM_S = 120.0


def _token_warm_loop():
    """Keep the pit's OAuth token fresh, off the button's thread.

    driver_message._token() refreshes INLINE, on whichever thread is serving
    the press. So about once an hour one unlucky press also paid for a full
    OAuth round trip to oauth2.googleapis.com before its command went anywhere
    -- and that call gets ten seconds before it gives up.

    That is one answer to "sometimes the lap clock takes five seconds": nothing
    to do with the car, the command or the link to it, just a token that
    expired under an engineer's finger. Refreshing it here means every press
    finds a valid one and goes straight out.

    Failures are ignored on purpose. There is no service account on a laptop
    running the demo, and a warm-up that cannot run is not a reason to log a
    line every two minutes -- a real send still raises where the engineer can
    see it.
    """
    driver_message = car_link_ro()
    if driver_message is None:
        return          # a demo store sends nothing, so there is no token to warm
    while True:
        try:
            driver_message.warm_token()
        except Exception:
            pass
        time.sleep(TOKEN_WARM_S)


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

# A GPS FIX OLDER THAN THIS IS A PROBLEM, and gps_fix alone cannot tell you.
#
# `gps_fix` is 1 if vehicle_state["gps"] is non-empty (main._health_snapshot),
# and _refresh_gps() only ever OVERWRITES that dict -- it never clears it. The
# reader's get_coordinates() likewise serves the last fix forever with a
# growing fix_age_s. So gps_fix means "this car has had a fix at some point",
# not "it has one now", and it stayed 1 through a two-hour outage on
# 2026-09-18 while the dashboard showed a clean LIVE badge. The one honest
# signal the car sends is the AGE.
#
# 60 s, not the 15 s limits.GPS_LIVE_MAX_AGE_S the map dims its dot at. The map
# is drawing a position and must stop trusting it quickly; this decides whether
# to put words on the status pill, and a pill that flickers every time the car
# passes under a bridge is one the crew learns to ignore by Saturday -- which
# is exactly what this file says about the CAN badge three comments up.
GPS_STALE_AFTER_S = 60.0


def _age_words(seconds):
    """47.3 -> '47s', 2810 -> '47m'. Short enough to sit inside a status pill."""
    seconds = float(seconds)
    if seconds < 120:
        return "%.0fs" % seconds
    if seconds < 7200:
        return "%.0fm" % (seconds / 60.0)
    return "%.1fh" % (seconds / 3600.0)


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

    gps_age = state.get("gps_age_s")
    gps_detail = (state.get("gps_detail") or "")
    if "no receiver" in gps_detail:
        # THE ONE WORTH NAMING SEPARATELY. gpsd holds no device at all -- the
        # modem re-enumerated (a cable change or an LTE reset does it) and
        # gpsd's udev hot-add rule does not match SimTech vendor IDs, so
        # nothing ever gives the port back. See deploy/gps_up.sh. "No GPS fix"
        # would send someone to look at the sky for a problem that is fixed
        # with one systemctl command.
        problems.append("gpsd has no GPS device")
    elif state.get("gps_fix") == 0:
        problems.append("no GPS fix")
    elif gps_age is not None and gps_age > GPS_STALE_AFTER_S:
        # The car is still serving its last position and still says it has a
        # fix. Only the age says the position is from another part of the race.
        problems.append("GPS fix %s old" % _age_words(gps_age))

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
        "gpsAgeS": state.get("gps_age_s"),
        "gpsFixTs": state.get("gps_fix_ts"),
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


# --------------------------------------------------------------------------- #
# Energy used so far this lap
# --------------------------------------------------------------------------- #
# The car publishes last_lap_energy and total_race_energy, never "this lap so
# far", so the pit subtracts: live total minus the total at the lap's first
# sample. Net of regen on both sides, which is what makes the Current-lap tile
# comparable with the Last-lap tile next to it.
#
# CACHED PER LAP, and that is not a micro-optimisation. db.lap_start_energy()
# costs what the lap is big -- 0.2 ms for a normal lap, 200 ms for one whose
# counter stalled and swallowed hours of samples -- and build_live() runs every
# 2 s for every viewer on the pit LAN. Uncached, one stalled lap counter would
# put the dashboard into exactly the read storm fetch_lap_track was rewritten to
# escape.
#
# The TTL is what makes a late baseline self-correct. A lap's first sample is
# normally the real start of it, but if the link was down at the trigger the
# earliest sample the pit holds is further in, and the collector's catch-up
# backfills the missing ones minutes later. Re-reading every 15 s picks the real
# start up when it arrives instead of holding the understated figure all lap.
_LAP_BASELINE_TTL_S = 15.0
# No lap can end this far BELOW where it started. Scaled off what a lap
# actually costs (the strategy matrix spends 72-88 Wh on one), so it stays
# right if the profiles are rebuilt: net regen over a lap is a fraction of the
# spend, never a multiple of it. Only an energy reset can clear this bar.
LAP_ENERGY_IMPLAUSIBLE_WH = max(
    [m.get("energy_wh") or 0.0 for m in C.PROFILE_MATRIX.values()] or [100.0])
_lap_baseline = {}                       # lap -> (read_at, energy_wh, at_m)
_lap_baseline_lock = threading.Lock()


def _lap_energy_baseline(conn, lap):
    """(energy_wh, metres_into_the_lap) for the start of `lap`, or (None, None).

    The second value is how far into the lap the pit's earliest sample sits.
    Near 0 it is the real start; a large number means the beginning of the lap
    was never received and the subtraction understates it, which the tile says
    out loud rather than quietly reporting a low number.
    """
    now = time.time()
    with _lap_baseline_lock:
        hit = _lap_baseline.get(lap)
        if hit is not None and (now - hit[0]) < _LAP_BASELINE_TTL_S:
            return hit[1], hit[2]
    row = db.lap_start_energy(conn, lap)
    energy = at_m = None
    if row is not None:
        energy, at_m = row["total_race_energy"], row["lap_distance_m"]
    with _lap_baseline_lock:
        _lap_baseline.clear()            # only the current lap is ever asked
        _lap_baseline[lap] = (now, energy, at_m)
    return energy, at_m

def _lap_clock(conn, state, active_lap, age):
    """The header's lap clock, from the shared rule in lap_clock.py.

    THE RULE ITSELF IS NOT HERE, deliberately: the garage TV shows the same
    clock and now works it out with the same function, so the two screens
    cannot disagree -- see that file's header for what it cost when they
    could. All this does is read what the rule needs from this request's
    connection and hand it over.

    Kept as a wrapper rather than inlined at the one call site because
    tools/check_lap_clock.py drives it with a stubbed load_app_state, which is
    how the handover and hold rules are checked with no database at all.
    """
    return lap_clock(
        state,
        # The newest sample's instant, measured the way read_live_state
        # measured the age every tile is labelled with, so the clock and those
        # ages cannot drift apart.
        (time.time() - age) if age is not None else None,
        hold_at=(load_app_state(conn, LAP_HOLD_KEY) or {}).get("heldAt"),
        datum_at=(load_app_state(conn, LAP_DATUM_KEY) or {}).get("atS"),
        # Only consulted when the car has published no datum of its own, and
        # not run at all on the path that does not need it.
        estimate=lambda: db.lap_started_estimate(conn, active_lap),
    )



def build_live(conn, manual_lap=-1):
    """The whole fast tier in one payload: tiles, sidebar, sectors, map.

    One SQLite read feeds all of it.
    """
    state, age = read_live_state(conn)
    race, elapsed_min, left_min = _race_clock(conn)
    fresh = age is not None and age <= C.DATA_STALE_AFTER_S

    active_lap = manual_lap if manual_lap >= 0 else state["auto_lap"]
    expected = elapsed_min / C.TARGET_LAP_TIME_MIN if C.TARGET_LAP_TIME_MIN else 0

    # Energy used so far this lap. Keyed on the CAR's lap, never on active_lap:
    # a manual lap number typed in the pit is a correction to the COUNT, and
    # using it here would look up stored rows that belong to a different lap.
    lap_energy = lap_energy_from_m = None
    car_lap, total_energy = state["auto_lap"], state["total_race_energy"]
    if car_lap is not None and total_energy is not None:
        base, lap_energy_from_m = _lap_energy_baseline(conn, car_lap)
        if base is not None:
            lap_energy = float(total_energy) - float(base)
            # A baseline from BEFORE an energy reset. The pit can send
            # reset_energy (lap_command.py), which zeroes total_race_energy on
            # the car mid-lap while the stored first sample of that lap still
            # holds the pre-reset total — subtracting gives a large negative
            # that would sit on the tile for the rest of the lap. A mildly
            # negative lap is REAL (energy is net of regen and may legitimately
            # decrease; see LapTracker.update_energy), so only a difference
            # bigger than any lap could physically regen is treated as the
            # reset it is, and reported as unknown rather than as a number.
            if lap_energy < -LAP_ENERGY_IMPLAUSIBLE_WH:
                lap_energy = lap_energy_from_m = None

    # Prefer the car's own "metres since the last lap trigger". Once laps are
    # cut at the GPS finish line, odometer % 4000 no longer lines up with the
    # real boundary and the sector display drifts further out of step each lap.
    #
    # Better still is the car's GPS lap position, which cannot be out of phase
    # with the track at all. And lap_distance_m is CLAMPED, not wrapped: it runs
    # past 4000 m whenever the car is waiting for a lap trigger (a virtual
    # crossing fires up to 400 m late), and "% 4000" turned those metres into
    # "Sector 1, Start - Turn 1" while the car was still in the last chicane.
    odo_km = state["odometer_km"]
    if state.get("track_pos_m") is not None:
        lap_dist = float(state["track_pos_m"]) % C.TRACK_LENGTH_METERS
    elif state["lap_distance_m"] is not None:
        lap_dist = min(max(float(state["lap_distance_m"]), 0.0),
                       C.TRACK_LENGTH_METERS - 1.0)
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
        # Wh used since this lap's trigger, net of regen — null until the car
        # has reported both a lap and an energy total. `FromM` is how far into
        # the lap the baseline sample sits, so the tile can flag a figure that
        # is missing the start of the lap.
        "currentLapEnergy": lap_energy,
        "currentLapEnergyFromM": lap_energy_from_m,
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
        # THE CLOCK THE DRIVER IS READING. One number: the wall-clock instant
        # the current lap began, so the browser counts up from it exactly as it
        # does for the race clock, and a lap clock does not freeze between the
        # car's 0.5 s pushes.
        #
        # `source` is "car" when it came from the car's own lap_started_ts --
        # the same datum the HUD stopwatch counts from, set in the same call
        # that re-datums the lap (lap_tracker._trigger_lap), so the two screens
        # agree to the millisecond. "store" means it was estimated from the
        # earliest sample the pit holds for this lap and can read short; the
        # caption says so rather than presenting a guess as a measurement.
        "lapClock": _lap_clock(conn, state, active_lap, age),
        "charge": charge_clock(conn),
    }


# --------------------------------------------------------------------------- #
@asynccontextmanager
async def _lifespan(_app):
    global _live_loop
    # The loop the websocket pushers run on, so a button pressed on a threadpool
    # worker can wake them. See nudge_live().
    _live_loop = asyncio.get_running_loop()
    # Started here, not at import: the tools/check_*.py scripts import this
    # module and must not start writing to Firebase.
    if PUBLIC_DRIVER_ENABLED:
        threading.Thread(target=_public_driver_loop, name="public-driver",
                         daemon=True).start()
    threading.Thread(target=_token_warm_loop, name="token-warm",
                     daemon=True).start()
    # Not on a demo store: the demo's "car" is a replay, and a replayed charge
    # must not start a clock that counts against the real race's three.
    if not DEMO_STORE:
        threading.Thread(target=_charge_watch_loop, name="charge-watch",
                         daemon=True).start()
    yield
    _live_loop = None


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
        # The one-pedal control's three landmarks, straight from efficiency.py
        # so the browser never carries its own copy of the neutral point. The
        # pedal bar is drawn from these: regen below neutral, acceleration
        # above it, and the scale runs 0..full in raw millivolts.
        "pedal": {"idleMv": efficiency.THROTTLE_MV_IDLE,
                  "neutralMv": efficiency.THROTTLE_MV_NEUTRAL,
                  "fullMv": efficiency.THROTTLE_MV_FULL},
        "trackLengthM": C.TRACK_LENGTH_METERS,
        "dataStaleAfterS": C.DATA_STALE_AFTER_S,
        "targetLapTimeMin": C.TARGET_LAP_TIME_MIN,
        "driverStint": {"limitS": DRIVER_STINT_LIMIT_S,
                        "warnS": DRIVER_STINT_WARN_S,
                        "critS": DRIVER_STINT_CRIT_S},
        "exportGroups": list(export.METRIC_GROUPS.keys()),
        "drivers": list(C.DRIVERS),
        "liveMetricCount": live_metrics.LIVE_METRIC_COUNT,
        "liveMetricsPerRow": live_metrics.LIVE_METRICS_PER_ROW,
        # Zolder paddock — where the map centres before the car reports.
        "mapFallback": {"lat": 50.9895, "lon": 5.2568},
        # True when this backend is NOT on the pit's real store. The Strategy
        # screen shows its typed-input panel only then — see DEMO_STORE.
        "demoStore": DEMO_STORE,
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
    driver_message = car_link()
    try:
        sent = driver_message.send_trip_reset()
    except Exception as e:
        raise HTTPException(502, "trip reset failed: %s" % e)
    return {"ok": True, "id": sent["id"], "sentAt": time.strftime("%H:%M:%S")}


@app.get("/api/trip_reset/ack")
def api_trip_reset_ack():
    """The car's acknowledgement of a trip reset, and ONLY of a trip reset.

    /lap_command_ack is shared with Cut Lap. A Cut Lap ack landing in between
    would otherwise be mistaken for this command's own confirmation.
    """
    driver_message = car_link_ro()
    if driver_message is None:
        return {"ack": None}
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


def _chart_columns(conn, chosen):
    """device_ts plus the one column each chosen metric reads.

    A column this store does not have is left OUT rather than asked for: an
    older store predates solar current, throttle, is_charging and the rest,
    and naming a missing column is an OperationalError where today it is a
    dash. value_from_row() returns None for a column that is not in the row,
    which is exactly what the chart drew before.
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(telemetry)")}
    cols = ["device_ts"]
    for m in chosen:
        if m.source in have and m.source not in cols:
            cols.append(m.source)
    return cols


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
        # THINNED IN SQL, AND ONLY THE COLUMNS DRAWN. This used to read every
        # row in the window with SELECT * and thin it here, which meant
        # dragging the whole store -- 4.6 kB a row, 61 % of it raw_json the
        # chart never looks at -- into Python to draw a few thousand points
        # from it. The "All" window measured 35.8 s on the pit's own store for
        # two metrics; it is now 0.15 s for all fifteen (db.fetch_series, and
        # db.CHART_COLUMNS for the index that makes it index-only).
        #
        # Still an even stride, not a bucket average, so every point drawn is a
        # value the car really measured at a moment it really measured it -- and
        # the stride counts back from the NEWEST sample, so the live end of the
        # trace is exact and `cursor` below is the true last point served.
        rows, in_range, step = db.fetch_series(
            conn, _chart_columns(conn, chosen), start_ts=start_ts,
            end_ts=end_ts, limit=limit, stride_target=max_points)
        total = db.count_samples(conn)

    # What the window held before thinning, which is what the caption reports.
    # `limit` caps it the same way it caps the rows themselves.
    full = min(in_range, limit)

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
        "downsampled": step > 1,
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
    # Biggest first to pick which ones are worth eliding, then back into time
    # order to emit: Plotly walks the breaks in the order given, and a list
    # that jumps back and forth is at best harder to read in a payload and at
    # worst a question about undefined behaviour nobody should have to ask.
    gaps.sort(reverse=True)
    out = []
    for gap, a, b in sorted(gaps[:max_breaks], key=lambda g: g[1]):
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
        # ONE PASS IN SQL, over the index. This used to pull every row in the
        # window -- SELECT *, raw_json and all -- to take three numbers off
        # each metric, on a strip that repolls every 10 s: half a minute of
        # work per poll on the "All" window. Same figures, exactly, over every
        # sample in the window rather than the thinned set the chart draws.
        have = {c for c in _chart_columns(conn, chosen) if c != "device_ts"}
        stats, rows = db.series_stats(conn, sorted(have),
                                      start_ts=start_ts, end_ts=end)
    out = []
    for m in chosen:
        # A metric whose column this store does not have: no readings, and the
        # whole window counted as missing -- the dash it has always drawn.
        s = stats.get(m.source) or {"min": None, "avg": None, "max": None,
                                    "now": None, "samples": 0}
        d = m.divisor
        out.append({
            "key": m.key, "label": m.label, "unit": m.unit, "color": m.color,
            # The divisor is applied AFTER the aggregate, which is the same
            # arithmetic value_from_row did per row: min/avg/max all commute
            # with dividing by a positive constant.
            "min": _scaled(s["min"], d),
            "avg": _scaled(s["avg"], d),
            "max": _scaled(s["max"], d),
            "now": _scaled(s["now"], d),
            "samples": s["samples"], "missing": rows - s["samples"],
        })
    return {"stats": out}


def _scaled(value, divisor):
    """`value` in the unit the chart shows, or None. Never 0 for absent."""
    return None if value is None else (value / divisor if divisor else value)


@app.get("/api/samples")
def api_samples(limit: int = Query(50, ge=1, le=500)):
    """Most recent raw samples, for the History tab's table."""
    with closing(ro_conn()) as conn:
        # The table shows the chart metrics and nothing else, so it asks for
        # those columns. SELECT * here was 60 rows x 4.6 kB of raw_json read
        # off disk and thrown away, every 10 s, for fifteen numbers a row.
        rows, _, _ = db.fetch_series(
            conn, _chart_columns(conn, HISTORY_CHARTS), limit=limit)
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
        # FROM THE GREEN FLAG, when there is one. The car is zeroed at the
        # start of a race (api_race -> send_new_race), so its lap numbers begin
        # again at 1 -- and without this bound the warm-up's lap 1 and the
        # race's lap 1 would sit in the same list under the same number, drag
        # each other through best/average, and land on two tabs of the per-lap
        # workbook fighting over one name.
        #
        # Nothing is deleted or hidden from the store: every warm-up sample and
        # lap is still there, and the time-ranged workbook still exports them.
        # With no race ever started the floor is None and this is the whole
        # store, exactly as before.
        rows = db.fetch_laps(conn, since_ts=race_lap_floor(conn))
        # Who was in the car when each lap finished. From the stints the PIT
        # logged — the car reports no driver — so a lap driven before anyone
        # pressed "Driver changed", or by a crew that never typed a name, has
        # driver null. Null, never "unknown": a missing reading is missing.
        db.attach_lap_drivers(rows, db.load_driver_stints(conn),
                              db.load_lap_driver_overrides(conn))
    # `kind` is the car's own verdict: flying | in | out | in_out | start |
    # suspect, or None from a car that predates it. Every lap is LISTED; only
    # flying laps feed best / average, because an in-lap's time holds a pit
    # stop and an out-lap starts from the pit lane.
    laps = [{"lap": r["lap"], "driver": r["driver"], "key": r["key"],
             "driverEdited": r["driver_edited"], "energyWh": r["energy_wh"],
             "lapTimeS": r["lap_time_s"], "distanceM": r["distance_m"],
             "kind": r["kind"], "flags": r["flags"], "source": r["lap_source"],
             "stoppedS": r["stopped_s"], "finishedTs": r["finished_ts"],
             # WHEN THE LAP BEGAN: the finish minus the car's own measured lap
             # time. Not "the previous lap's finish" -- those differ exactly
             # where it matters, across a Pi restart or a stop, and the lap
             # time is the car's measurement while the gap is only an absence.
             # Formatted HERE because the zone rule (export_local) lives here;
             # the browser never turns an epoch into a wall clock on its own.
             # Null when either half is missing, never a guess.
             "started": (_clock(r["finished_ts"] - r["lap_time_s"])
                         if r["finished_ts"] and r["lap_time_s"] else None)}
            for r in rows]
    flying = db.flying_laps(rows)
    times = [r["lap_time_s"] for r in flying if r["lap_time_s"]]
    energy = [r["energy_wh"] for r in flying if r["energy_wh"] is not None]
    return {
        "laps": laps,
        "summary": {
            "count": len(laps),
            "flyingCount": len(flying),
            "bestS": min(times) if times else None,
            "avgS": (sum(times) / len(times)) if times else None,
            "avgWh": (sum(energy) / len(energy)) if energy else None,
        },
    }


class LapDriverBody(BaseModel):
    # One lap, or a run of them -- a whole stint credited to the wrong name is
    # eighteen laps, and nobody should fix that with eighteen dropdowns.
    keys: list[str]
    # A name from constants.DRIVERS, or "" to drop the edit and go back to
    # whatever the stint log says.
    driver: str = ""


@app.post("/api/laps/driver")
def api_laps_driver(body: LapDriverBody):
    """Set, by hand, who drove these laps. The pit's word over the stint log.

    Filed by lap_seq (db.lap_key), scoped to this race, and read back by
    db.attach_lap_drivers -- the one function the lap table and both workbooks
    share, so an edit here is the name in Excel too. Nothing about the STINT
    changes: the countdown, the stint number and the public page are untouched.

    Only names from the team's list are accepted. A free-typed name is how
    "ido" and "Ido" become two drivers.
    """
    name = body.driver.strip()
    if name and name not in C.DRIVERS:
        raise HTTPException(400, "driver must be one of: %s" % ", ".join(C.DRIVERS))
    if not body.keys or len(body.keys) > 1000:
        raise HTTPException(400, "give between 1 and 1000 laps")
    with closing(rw_conn()) as conn:
        race_start = db.load_race_state(conn).get("race_start_time")
        rec = load_app_state(conn, db.LAP_DRIVERS_KEY) or {}
        if rec.get("race_start") != race_start:
            rec = {"race_start": race_start, "by_key": {}}
        by = dict(rec.get("by_key") or {})
        for key in body.keys:
            if name:
                by[str(key)] = name
            else:
                by.pop(str(key), None)
        rec["by_key"] = by
        save_app_state(conn, db.LAP_DRIVERS_KEY, rec)
    return {"ok": True, "edited": len(body.keys), "driver": name or None}


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
# The profile the PIT selected
# --------------------------------------------------------------------------- #
# The pit's target speed is the profile chosen in the Strategy section. Full
# stop. The strategist picks a profile, sends it to the car, and every target
# readout on this dashboard is then the curve they picked -- there is no second
# source that can quietly move it underneath them.
#
# WHAT THIS REPLACED, and why it had to go. The selection used to be ignored on
# purpose: the target came from the car's `active_strategy` column, falling back
# to the Firebase acknowledgement, on the reasoning that "the message left the
# pit" is not "the car changed profile". Sound in theory. In practice the ack
# has no expiry, so with the car off and the store empty the dashboard served
# whatever profile was last acknowledged -- a fast_189s ack from three weeks
# earlier was still driving the target speed. A number nobody in the pit chose
# and nobody could see the age of is worse than an assumption they made
# themselves.
#
# The car's own report is NOT consulted here. That is deliberate and it is the
# trade: if the car rejects a profile or has not applied it yet, this dashboard
# shows what the pit asked for, not what the car is flying. The honest reading
# of the car's answer lives in the Strategy section, which polls
# /api/strategy/ack and says "Car confirmed it is running X" in as many words.
# That is where a disagreement surfaces.
#
# strategy_engine.profile_to_df() loads through speed_profile.load_csv -- the
# CAR's own loader -- so the pit and the car cannot read the same file
# differently.


@memo(ttl=30)
def _profile_frame(key, path, mtime):
    """One profile as the frame get_live_track_status() expects.

    `mtime` is in the key because profile_builder.py rewrites these CSVs while
    the dashboard is running; without it the pit would serve a stale curve for
    as long as the process lived.
    """
    return profile_to_df(path)


# The selection, cached in process. build_live() runs every 2 s per socket and
# app_state is one tiny row, but this is also what makes a new selection appear
# on the NEXT live frame rather than after a cache expiry: /api/strategy/select
# updates this global in the same breath as it writes the row.
_CHOICE_UNLOADED = object()
_pit_choice = _CHOICE_UNLOADED


def pit_strategy_choice():
    """The profile key the pit last selected, or None if nobody has yet.

    A key that is no longer on disk (the profile was renamed or deleted in the
    Profile Builder) counts as no selection, so the caller falls back to the
    default rather than to a curve that cannot be loaded.
    """
    global _pit_choice
    if _pit_choice is _CHOICE_UNLOADED:
        try:
            with closing(ro_conn()) as conn:
                stored = load_app_state(conn, STRATEGY_CHOICE_KEY)
        except Exception:
            return None                  # never CACHE a failed read; retry next
        _pit_choice = stored if isinstance(stored, str) else None
    return _pit_choice


def set_pit_strategy_choice(key):
    """Persist the pit's selection and make it live immediately."""
    global _pit_choice
    with closing(rw_conn()) as conn:
        save_app_state(conn, STRATEGY_CHOICE_KEY, key)
    _pit_choice = key


def _active_profile(state):
    """(frame, {key, source}) for the curve the PIT selected.

      "pit"     -- chosen in the Strategy section and sent to the car.
      "default" -- nobody has chosen this race yet, so the target speed is an
                   assumption. Flagged so the UI can SAY so: a target from an
                   assumed profile must never look like one from a chosen
                   profile, the same rule as has_gps versus the paddock
                   fallback.

    `state` is accepted and unused. It is the live telemetry row, which carries
    the car's own `active_strategy`; see the section comment for why that is
    deliberately not read here.
    """
    try:
        available = speed_profile.available_profiles()
    except Exception:
        available = {}

    key, source = pit_strategy_choice(), "pit"
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
        # Bounded to the race, like the purple fold below it. The car's lap
        # counter goes back to 0 at the green flag, so without this the
        # warm-up holds the highest tags in the store and the grid shows two
        # laps from before the race, for the whole race.
        laps = db.recent_laps(conn, 4, since_ts=start)
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

# Energy per lap: THE MATRIX IS THE PLAN. constants.PROFILE_MATRIX holds a lap
# time and a Wh/lap for each profile, both put there by the crew, and both are
# served exactly as written. Nothing here recomputes them.
#
# The car's own laps are served BESIDE that column, never over it. They used to
# replace it -- a profile with enough laps got their median while the rest kept
# their stored figure -- and that mixed two different claims in one column: the
# crew read "Base 130.6" and "Fast 88" as a comparison of two profiles when it
# was really a comparison of a measurement with a guess. A matrix is a decision
# about how to drive. It should change when the crew changes it, not quietly
# when a stint happens to be logged.
#
# So the table says 145 Wh at 285 s because that is what the matrix says, and
# the caption says what the car actually paid, and the difference between them
# is the crew's to act on.
MIN_LAPS_FOR_MEASURED = C.MIN_LAPS_FOR_MEASURED
# What the matrix editor will accept. Wide enough for a car being nursed home
# on one motor, narrow enough that a slipped decimal point is refused rather
# than planned around.
MATRIX_MIN_LAP_S, MATRIX_MAX_LAP_S = 60.0, 1800.0
MATRIX_MIN_WH, MATRIX_MAX_WH = 1.0, 2000.0
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
        # Stationary time, and the two things it is made of. Served split so
        # the page can show the arithmetic instead of restating the 5 min a
        # driver change costs -- a constant the browser must not hold a copy of.
        "pitMin": plan["pit_min"],
        "chargeStopMin": plan["charge_stop_min"],
        "swapMin": plan["swap_min"],
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
    # Never round a real charge down to nothing. _plan_one_strategy() reads a
    # start of 0 Wh as "unknown" and plans a FULL pack, so a car limping in on
    # 0.2% -- under half of one rounding step -- would be planned as if it were
    # brimmed, which is the most dangerous direction this can be wrong in.
    if battery_wh > 0 and rounded_wh <= 0:
        rounded_wh = STRATEGY_WH_ROUND
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
        # The crew's 95% rule, on the wire beside the floor and for the same
        # reason: the page states the limits the plan was made under instead
        # of holding numbers of its own. capacityWh is the pack at 100% SoC
        # (9000 Wh); no charge in any plan here goes above ceilingWh.
        "ceilingWh": strategy_engine.BATTERY_CEILING_WH,
        "maxChargeSocPct": strategy_engine.MAX_CHARGE_SOC_PCT,
        "minSocPct": strategy_engine.MIN_SOC_PCT,
        "minStopMin": strategy_engine.MIN_STOP_DURATION_MIN,
        # The hour ceiling, served for the same reason as the floor: the page
        # states the rule the plan was made under, and reads it off the engine
        # rather than printing a number of its own.
        "maxStopMin": strategy_engine.MAX_STOP_DURATION_MIN,
        "maxStops": strategy_engine.MAX_STOPS,
        # Carried across from the engine so the screen shows its warning: the
        # curve's shape is right, its numbers
        # have never been checked against this charger or pack.
        "chargingCurveIsMeasured": strategy_engine.CHARGING_CURVE_IS_MEASURED,
        "measured": measured_note or {},
        "minLapsForMeasured": MIN_LAPS_FOR_MEASURED,
        "timeLeftMin": time_left_min,
        # The race duration, which is also the cap on the demo screen's typed
        # "time remaining". Served so the browser holds no copy of it.
        "maxTimeLeftMin": strategy_engine.RACE_DURATION_MIN,
    }


@app.get("/api/strategy")
def api_strategy(manual_lap: int = Query(-1),
                 soc_pct: float | None = Query(None, gt=0, le=100),
                 time_left_min: float | None = Query(
                     None, ge=0, le=strategy_engine.RACE_DURATION_MIN)):
    """The strategy screen. `soc_pct` and `time_left_min` are DEMO-ONLY.

    On the real dashboard the plan is always made from what the car reported
    and from the race clock in the store; both overrides are ignored there, so
    a stray query string cannot put a typed number in front of the crew as if
    the car had sent it. On the demo backend they replace the two inputs the
    search actually takes, which is how the strategy can still be read when the
    Pi has been silent for hours and the stored SoC is long out of date.

    Nothing is written either way: this endpoint has always been a pure
    read + search, and an override only changes the arguments it is given.
    """
    with closing(ro_conn()) as conn:
        state, _ = read_live_state(conn)
        _, _, clock_left_min = _race_clock(conn)
    if not DEMO_STORE:
        soc_pct = time_left_min = None
    soc = state["soc"] if soc_pct is None else soc_pct
    left_min = clock_left_min if time_left_min is None else time_left_min
    active_lap = manual_lap if manual_lap >= 0 else state["auto_lap"]
    # `not soc` covers both a missing reading and a reported 0: neither is a
    # usable capacity, so the matrix assumes a full pack rather than telling
    # the strategist the car is empty. FULL means 100% SoC -- the car rolls out
    # at 100% and only the charges DURING the race are capped at 95%.
    #
    # An override is always believed: it cannot be 0 (the query rejects it,
    # because the engine reads an empty pack as an unknown one and plans a full
    # one instead), so a typed 4% is planned as 4% and not as a placeholder.
    assumed_full = soc_pct is None and not soc
    battery_wh = (strategy_engine.BATTERY_FULL_WH if assumed_full
                  else (soc / 100.0) * strategy_engine.BATTERY_FULL_WH)

    # The matrix, verbatim. A row with no stored Wh/lap (a profile the Builder
    # wrote and nobody has costed) is left out rather than planned at zero.
    table = [{"label": s["label"], "lap_time_min": s["lap_time_min"],
              "energy_wh": s["energy_wh"]}
             for s in C.STRATEGIES if s.get("energy_wh") is not None]

    # What the car paid, for the caption and the per-row tooltip. Never
    # substituted into the table above.
    measured = _measured_energy_wh()
    measured_note, stored = {}, {s["key"]: s for s in C.STRATEGIES}
    for key, (wh, n) in measured.items():
        s = stored.get(key)
        if s is None:
            continue
        measured_note[s["label"]] = {"laps": n, "wh": round(wh, 1),
                                     "storedWh": s["energy_wh"]}

    out = _strategy_payload(left_min, battery_wh, active_lap, table, measured_note)
    out.update({
        "assumedFullPack": assumed_full,
        # Whether the typed-input panel may be shown at all, and what of it the
        # server actually honoured. Served rather than inferred in the browser:
        # a page that decides for itself that an override took effect will
        # label a car-derived plan as typed the moment the two disagree.
        "demoStore": DEMO_STORE,
        "overrides": {"socPct": soc_pct, "timeLeftMin": time_left_min},
        # What the car and the race clock say, so the panel can show what is
        # being overridden and offer the way back to it.
        "carSocPct": state["soc"],
        "clockTimeLeftMin": clock_left_min,
        # The profile list as it stands RIGHT NOW, so the selector beside this
        # table tracks a matrix edit on the next poll. /api/config carries the
        # same list, but the browser fetched that once when the page loaded --
        # before the crew changed a lap time from the editor ten feet away.
        "matrix": _matrix_rows(),
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


@app.get("/api/export/laps.xlsx")
def api_export_laps_xlsx(firstLap: int | None = Query(None),
                         lastLap: int | None = Query(None),
                         groups: str = Query("")):
    """The per-lap workbook: the Laps sheet as an index, then a tab per lap.

    A SECOND EXPORT, BESIDE /api/export/telemetry.xlsx AND NOT INSTEAD OF IT.
    That one is cut by time and is what the crew downloads at the end of a
    session; this one is cut by lap number and answers "show me lap 87". Same
    column groups, same openpyxl module, different question.

    ValueError from export.py is the caller's mistake -- an empty lap range, or
    more laps than MAX_LAP_SHEETS -- so it comes back as a 400 carrying the
    sentence the panel shows, not as a 500.
    """
    names = [g for g in groups.split(",") if g] or list(export.METRIC_GROUPS)
    cols = export.metrics_for_groups(names)
    buf = io.BytesIO()
    # Same reason write_xlsx is handed a connection: with SOLARRACE_DB_PATH set,
    # a function opening its own would export a different store from the one
    # the sidebar counted laps in.
    with closing(ro_conn()) as conn:
        try:
            laps, rows = export.write_laps_xlsx(
                buf, first_lap=firstLap, last_lap=lastLap, metrics=cols,
                conn=conn)
        except ValueError as e:
            raise HTTPException(400, str(e))
    data = buf.getvalue()
    span = ("laps_%s-%s" % (firstLap, lastLap) if firstLap is not None
            and lastLap is not None else "laps")
    name = "telemetry_%s_%s.xlsx" % (span, datetime.now().strftime("%Y%m%d-%H%M"))
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="%s"' % name,
                 "X-Row-Count": str(rows), "X-Lap-Count": str(laps)})


@app.get("/api/export/lap_bounds")
def api_export_lap_bounds():
    """Which lap numbers the store holds, for the per-lap export's fields.

    NOT folded into /api/export/bounds, which the panel polls every 60 s:
    counting laps means db.fetch_laps() grouping the whole telemetry table,
    and making the sidebar pay that every minute to fill in two fields that
    are read once would be a poor trade. This is fetched when the per-lap
    section is opened.
    """
    with closing(ro_conn()) as conn:
        lo, hi, n = export.lap_bounds(conn=conn)
    return {"firstLap": lo, "lastLap": hi, "laps": n,
            "maxSheets": export.MAX_LAP_SHEETS}


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


class ChargeBody(BaseModel):
    # start | stop | discard | set_count. No default: the count behind this is
    # regulation.
    action: str
    # start: the plug went in this many minutes ago (the press came late).
    minutesAgo: float | None = None
    # set_count: how many charges this race has REALLY used. The pit's word
    # over the clock's -- a charge made with the car switched off and nobody
    # pressing Start is a charge the store never saw.
    count: int | None = None


@app.post("/api/charge")
def api_charge(body: ChargeBody):
    """The pit's hand on the charging clock. See "The charging clock" above.

    `start` for a car that cannot say so itself (switched off on the charger),
    `stop` when the plug comes out, `discard` for a press that was not a
    charge -- it stops the clock AND gives the count back.
    """
    if body.action not in ("start", "stop", "discard", "set_count"):
        raise HTTPException(400, "action must be start, stop, discard or set_count")
    with closing(rw_conn()) as conn:
        if body.action == "set_count":
            if body.count is None or not (0 <= body.count <= 20):
                raise HTTPException(400, "count must be a number from 0 up")
            rec = _charge_record(conn)
            changed = int(rec.get("count") or 0) != body.count
            rec["count"] = int(body.count)
            save_app_state(conn, CHARGE_CLOCK_KEY, rec)
        elif body.action == "start":
            ago = float(body.minutesAgo or 0.0)
            if not (0 <= ago <= 2 * strategy_engine.MAX_STOP_DURATION_MIN):
                raise HTTPException(400, "minutesAgo must be between 0 and %d"
                                    % (2 * strategy_engine.MAX_STOP_DURATION_MIN))
            changed = _charge_start(conn, "pit", now=time.time() - ago * 60.0)
        else:
            changed = _charge_stop(conn, discard=body.action == "discard")
        out = charge_clock(conn)
    if changed:
        nudge_live()
    return {"ok": True, "changed": changed, "charge": out}


class EstimateBody(BaseModel):
    # Both REQUIRED, no defaults: this puts a moving car on a public page, and
    # "lap 0 at the start line because a field was left out" is a worse thing
    # to publish than nothing.
    lap: int
    distM: float


@app.get("/api/public/estimate")
def api_public_estimate_get():
    """What the spectator page has been asked to estimate, if anything.

    `prefill` is the car's last known lap and place, for the two fields -- a
    starting point for the person typing, who may well know better (that is
    why they are typing).
    """
    with closing(ro_conn()) as conn:
        est = _public_estimate(conn)
        state, age = read_live_state(conn)
    lap = state.get("auto_lap")
    dist = state.get("lap_distance_m")
    return {
        "enabled": PUBLIC_DRIVER_ENABLED,
        "active": est is not None,
        "estimate": est,
        "synced": (_public_estimate_sent == est) if PUBLIC_DRIVER_ENABLED else None,
        "carAgeS": age,
        "carFresh": age is not None and age <= C.DATA_STALE_AFTER_S,
        "prefill": {"lap": None if lap is None else int(lap) + 1,
                    "distM": None if dist is None
                    else float(dist) % C.TRACK_LENGTH_METERS},
        "trackLengthM": C.TRACK_LENGTH_METERS,
    }


@app.post("/api/public/estimate")
def api_public_estimate_start(body: EstimateBody):
    """Start showing the public page where the car SHOULD be.

    Refused while the car is being heard: the page only shows an estimate when
    the car is silent, the sync loop would clear it within seconds anyway, and
    a press that appears to do nothing is worse than one that says why.
    """
    if not PUBLIC_DRIVER_ENABLED:
        raise HTTPException(409, "this dashboard is not on the pit's store, so "
                                 "it does not publish to the spectator page")
    if body.lap < 0:
        raise HTTPException(400, "lap must be 0 or more")
    if not (0 <= body.distM < C.TRACK_LENGTH_METERS):
        raise HTTPException(400, "position must be between 0 and %d m"
                                 % C.TRACK_LENGTH_METERS)
    with closing(rw_conn()) as conn:
        _state, age = read_live_state(conn)
        if age is not None and age <= C.DATA_STALE_AFTER_S:
            raise HTTPException(409, "the car is live (%.0f s ago) - the page "
                                     "is showing its real position" % age)
        est = {"startedAt": time.time(), "lap": int(body.lap),
               "distM": float(body.distM)}
        save_app_state(conn, PUBLIC_ESTIMATE_KEY, est)
    _kick_public_driver()
    return {"ok": True, "estimate": est}


@app.post("/api/public/estimate/stop")
def api_public_estimate_stop():
    """Stop estimating. The page goes back to "where it was last seen"."""
    with closing(rw_conn()) as conn:
        save_app_state(conn, PUBLIC_ESTIMATE_KEY, {})
    _kick_public_driver()
    return {"ok": True}


class NoteBody(BaseModel):
    """What to show the public. Empty text takes the note down."""
    text: str = ""


@app.get("/api/public/note")
def api_public_note_get():
    """The line the public page is showing, if any. See PUBLIC_NOTE_KEY."""
    with closing(ro_conn()) as conn:
        note = _public_note(conn)
    return {
        "enabled": PUBLIC_DRIVER_ENABLED,
        "note": note,
        # False while the write to Firebase is pending or failing, so the pit
        # can see that what it typed has not reached anybody yet.
        "synced": (_public_note_sent == note) if PUBLIC_DRIVER_ENABLED else None,
        "maxLen": PUBLIC_NOTE_MAX_LEN,
    }


@app.post("/api/public/note")
def api_public_note_set(body: NoteBody):
    """Put a line on the public page, or take it down with empty text.

    THE CLOCK BELONGS TO THE SITUATION, NOT TO THE PRESS. Re-sending the same
    words -- the sync retrying, a second crew member pressing the same button
    -- keeps the instant the note first went up, because what the page reports
    is how long the car has been in this state. Different words are a
    different thing happening, and start their own count.
    """
    if not PUBLIC_DRIVER_ENABLED:
        raise HTTPException(409, "this dashboard is not on the pit's store, so "
                                 "it does not publish to the spectator page")
    text = _clean_note(body.text)
    with closing(rw_conn()) as conn:
        old = load_app_state(conn, PUBLIC_NOTE_KEY) or {}
        if not text:
            save_app_state(conn, PUBLIC_NOTE_KEY, {})
        else:
            same = _clean_note(old.get("text")) == text
            since = old.get("since") if same else None
            save_app_state(conn, PUBLIC_NOTE_KEY,
                           {"text": text, "since": float(since or time.time())})
        note = _public_note(conn)
    # The write to Firebase is the background thread's job -- the crew is in
    # the middle of a pit stop and must not wait on the internet.
    _kick_public_driver()
    return {"ok": True, "note": note}


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
    new_race = False
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
            #
            # THE CAR IS ZEROED ON THIS SAME TEST, below. One rule decides both
            # "the stint starts over" and "the warm-up is discarded", because
            # they are the same question -- and because a second, slightly
            # different definition of "a new race" is how a resume would come
            # to wipe a lap count mid-race.
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
        payload = {**db.load_race_state(conn), "driverStint": driver_stint(conn)}

    # THE GREEN FLAG REACHES THE CAR. A new race zeroes the car's lap count,
    # lap sequence, distance, energy and finished-lap figures, and the car
    # rewrites its checkpoint -- so the warm-up laps are not the race's, and
    # nobody has to delete lap_checkpoint.json on the Pi between the
    # installation laps and the start. See driver_message.send_new_race.
    #
    # SENT AFTER THE STORE IS CLOSED, not inside the block above: this is a
    # network write that gets up to five seconds, and holding the pit's only
    # write connection across it would block every other writer for as long as
    # Firebase felt like taking. (lap_command.py's docstring documents the same
    # trap on the car.)
    #
    # A CAR THAT CANNOT BE REACHED DOES NOT FAIL THE START, exactly as with the
    # lap-clock hold: a race that will not start because the link is down is
    # worse than a car still counting its warm-up, and the caller is told which
    # it got. The command carries its own timestamp and lap_command.py refuses
    # one older than MAX_COMMAND_AGE_S, so a car that comes up long afterwards
    # adopts it without executing it -- it will not wipe a race an hour in.
    #
    # ON A DEMO STORE THE CLOCK STARTS AND THE CAR IS LEFT ALONE. Starting a
    # race on the demo is a legitimate thing to do -- it is the demo's own
    # clock, in its own SQLite file -- but new_race zeroes the REAL car's laps,
    # distance, energy and checkpoint, and the car then republishes those zeros
    # to /public/live, which is what the spectator page on GitHub Pages plots.
    # A practice run on the demo must not reset the race the public is watching.
    car_error = None
    if new_race:
        if DEMO_STORE:
            car_error = CAR_LINK_REFUSED
        else:
            import driver_message
            try:
                driver_message.send_new_race()
            except Exception as e:                           # noqa: BLE001
                car_error = str(e)
    return {**payload, "newRace": new_race, "carError": car_error,
            # True when carError is the demo boundary rather than a link
            # failure: the page must not offer "try again" for a deliberate
            # refusal, and must not call it an error.
            "carLinkDisabled": DEMO_STORE}


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
            # The stint that just ended, closed at this instant. See _stint_log.
            "log": _stint_log(st, now),
            # Everything needed to put it back exactly as it was.
            "previous_started_at": prev_started,
            "previous_stint": st.get("stint"),
            "previous_driver": st.get("driver"),
            "previous_names": st.get("names"),
            "previous_accumulated_s": st.get("accumulated_s"),
            "previous_running_since": st.get("running_since"),
            # A change in progress ENDS here: this press is the new driver
            # being in the car, which is the end of the swap the pit flagged.
            # The new record simply does not carry change_started_at forward;
            # the previous_ copy is only so the undo can put it back.
            "previous_change_started_at": st.get("change_started_at"),
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

    A NAME COUNTS FROM NOW, NEVER BACKWARDS. It used to overwrite the stint's
    one name, so typing the incoming driver here a moment before pressing
    "Driver changed" relabelled the OUTGOING driver's whole stint: three hours
    and eighteen laps of Ido's became Amit's on 2026-09-19. Now:

      * a stint nobody has named yet takes the name for the whole of it -- that
        is driver one, started by the green flag, being named for the first
        time, and there is nobody else those laps could belong to;
      * a stint that already HAS a name keeps it for every lap driven so far,
        and the new name applies to laps that finish from this instant on.

    The per-lap dropdown in the lap table is the tool for correcting the past;
    this button is not.
    """
    now = time.time()
    with closing(rw_conn()) as conn:
        st = load_app_state(conn, DRIVER_STINT_KEY) or {}
        new = _clean_driver(body.driver)
        old = st.get("driver") or None
        if old and new != old and st.get("started_at") is not None:
            names = list(st.get("names") or [])
            if not names:
                names.append({"driver": old, "from": st["started_at"]})
            names.append({"driver": new, "from": now})
            st["names"] = names
        st["driver"] = new
        save_app_state(conn, DRIVER_STINT_KEY, st)
        _kick_public_driver()
        return driver_stint(conn, now)


class ChangingBody(BaseModel):
    """True when a driver change has just STARTED, False when it has not."""
    on: bool = True


@app.post("/api/driver_stint/changing")
def api_driver_stint_changing(body: ChangingBody):
    """Say that a driver change is under way — the car is in the box, swapping.

    This exists because the system knew the END of a swap and never the start.
    "Driver changed - reset timer" is pressed when the new driver is in and
    their clock starts; until then the public page showed the old driver and a
    car sitting still with no explanation, which reads as a broken car to the
    families the page is for. Same argument as the charging badge.

    The SERVER stamps the time, like every other clock here, so a tablet with
    a wrong clock cannot make a two-minute swap look like twenty.

    NOTHING EXPIRES THIS. By the team's decision it stays up until somebody
    takes it down — either this endpoint with on=false, or the "Driver
    changed" press, which is the swap ending. So that a flag left on is
    visible rather than silent, every screen shows how long it has been up.
    """
    now = time.time()
    with closing(rw_conn()) as conn:
        st = load_app_state(conn, DRIVER_STINT_KEY) or {}
        if body.on:
            # Pressing it twice must not restart the count: the interesting
            # number is how long this swap has run, not how long since the
            # last press.
            st["change_started_at"] = st.get("change_started_at") or now
        else:
            st.pop("change_started_at", None)
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
            # Drop the entry the change just appended: that stint is the one
            # being put back into the car, so it is current again, not history.
            "log": list(st.get("log") or [])[:-1],
            # A mis-click during a swap: the swap was still running, so the
            # badge on the public page and the wall comes back with it.
            "change_started_at": st.get("previous_change_started_at"),
        }
        # The names the restored stint had carried, so an undo does not
        # flatten it back to one name.
        if st.get("previous_names"):
            restored["names"] = st["previous_names"]
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
    driver_message = car_link()
    try:
        driver_message.send_driver_command(body.category, body.value)
    except Exception as e:
        raise HTTPException(502, "send failed: %s" % e)
    return {"ok": True, "shown": ("%s: %s" % (body.category, body.value)
                                  if body.category else str(body.value))}


@app.delete("/api/driver_message")
def api_clear_message():
    driver_message = car_link()
    try:
        driver_message.clear_driver_command()
    except Exception as e:
        raise HTTPException(502, "clear failed: %s" % e)
    return {"ok": True}


def _note_lap_datum(at=None):
    """Start the wall's lap clock from NOW, without waiting for the car.

    The two buttons that re-datum the lap -- Cut lap and Restart lap -- call
    this after the command is away. It is the wall's copy of what the car is
    about to do, and it lives only until the car's own answer comes back inside
    a telemetry sample; _lap_clock() does that handover.

    Display only, like the hold beside it: no lap is cut here, no count moves,
    nothing is recorded. The car cuts the lap; this moves a number on a screen
    in the pit a few seconds earlier than the round trip allows.
    """
    with closing(rw_conn()) as conn:
        save_app_state(conn, LAP_DATUM_KEY,
                       {"atS": time.time() if at is None else at})
    nudge_live()


@app.post("/api/cut_lap")
def api_cut_lap():
    """Ask the CAR to close its lap (snapshots lap energy + time). Does not
    change the manual lap override."""
    driver_message = car_link()
    try:
        sent = driver_message.send_lap_cut()
    except Exception as e:
        raise HTTPException(502, "cut lap failed: %s" % e)
    _note_lap_datum()
    return {"ok": True, "id": sent["id"], "sentAt": time.strftime("%H:%M:%S")}


class LapSetBody(BaseModel):
    # REQUIRED, and no default. The car reads this as `cmd.get("value") or 0`,
    # so anything falsy zeroes the race lap count. A pit-exit button that can
    # wipe the lap count by omitting a field is not one to have on a race
    # dashboard, so the number has to be stated.
    lap: int


@app.post("/api/lap/set")
def api_lap_set(body: LapSetBody):
    """Correct the car's lap NUMBER, e.g. to match the officials' count.

    `lap` is what the count should READ afterwards, not an increment. With the
    gate-based tracker this changes the number and nothing else; starting a
    fresh lap is /api/lap/restart.
    """
    if body.lap < 0:
        raise HTTPException(400, "lap must be 0 or more")
    driver_message = car_link()
    try:
        sent = driver_message.send_lap_set(body.lap)
    except Exception as e:
        raise HTTPException(502, "set lap failed: %s" % e)
    return {"ok": True, "id": sent["id"], "lap": body.lap,
            "sentAt": time.strftime("%H:%M:%S")}


@app.post("/api/lap/restart")
def api_lap_restart():
    """Start a FRESH lap on the car without counting one.

    For a lap that has to be thrown away. NOT a pit-stop button: the car closes
    the in-lap itself when it passes the line in the pit lane. The lap number
    is not touched -- /api/lap/set corrects that, and nothing else.
    """
    driver_message = car_link()
    try:
        sent = driver_message.send_lap_restart()
    except Exception as e:
        raise HTTPException(502, "restart lap failed: %s" % e)
    _note_lap_datum()
    return {"ok": True, "id": sent["id"], "sentAt": time.strftime("%H:%M:%S")}


class StopwatchBody(BaseModel):
    # "reset" starts the driver's clock from now, "clear" blanks it. No default:
    # the two do different things on the driver's instrument panel and the
    # caller should say which one it means.
    action: str


@app.post("/api/lap/stopwatch")
def api_lap_stopwatch(body: StopwatchBody):
    """Move the DRIVER's stopwatch from the pit. Display only.

    Nothing the car records changes -- not the lap count, not a lap time, not
    the energy totals, not the odometer. It is the pit's copy of the button
    beside the clock on the HUD, and the car's next real lap cut takes the
    clock back over. See driver_message.send_stopwatch_reset().
    """
    driver_message = car_link()
    if body.action not in ("reset", "clear"):
        raise HTTPException(400, "action must be 'reset' or 'clear'")
    try:
        sent = (driver_message.send_stopwatch_reset() if body.action == "reset"
                else driver_message.send_stopwatch_clear())
    except Exception as e:
        raise HTTPException(502, "stopwatch %s failed: %s" % (body.action, e))
    return {"ok": True, "id": sent["id"], "action": body.action,
            "sentAt": time.strftime("%H:%M:%S")}


class LapHoldBody(BaseModel):
    # True parks the clock where it stands, False lets it run again. No
    # default: a toggle that acts on a missing field is how a clock gets
    # stopped by a request that meant to start it.
    hold: bool


@app.post("/api/lap/hold")
def api_lap_hold(body: LapHoldBody):
    """Stop the stopwatch, or let it run again -- on the wall AND in the car.

    ONE clock, two buttons. The wall is parked here immediately so the press
    feels like a press, and the same instruction goes to the car; the driver's
    button beside the HUD clock does the same job from the other end, and
    whichever was pressed last is the one that stands.

    Display only on both sides: no lap is cut, the lap count does not move, and
    the energy totals and the odometer are untouched -- exactly the contract the
    stopwatch reset and clear already have.

    A car that cannot be reached does NOT fail the press. The wall still stops,
    because a pit unable to stop its own clock while the link is down is worse
    than one whose clock disagrees with a car that is not running; the caller
    gets `carError` and can say so.

    Self-clearing at both ends: here the stored instant is compared against the
    current lap's datum in _lap_clock(), and on the car the next crossing of the
    line releases it (driver_dash_v2._on_lap_timer).
    """
    driver_message = car_link()
    with closing(rw_conn()) as conn:
        save_app_state(conn, LAP_HOLD_KEY,
                       {"heldAt": time.time()} if body.hold else {})
    try:
        sent = (driver_message.send_stopwatch_stop() if body.hold
                else driver_message.send_stopwatch_resume())
    except Exception as e:
        nudge_live()
        return {"ok": True, "hold": body.hold, "id": None, "sentAt": None,
                "carError": str(e)}
    nudge_live()
    return {"ok": True, "hold": body.hold, "id": sent["id"],
            "sentAt": time.strftime("%H:%M:%S")}


@app.get("/api/cut_lap/ack")
def api_cut_lap_ack():
    """The car's acknowledgement. "Sent" and "the car is running it" are not
    the same thing, so the pit sees which one it has.

    THE ACK NODE IS RETAINED, and it is the LAST ack the car ever wrote -- not
    the ack to whatever was just pressed. With the car off it can be hours old:
    reading it as a confirmation of the press you just made is how a dashboard
    tells the crew a command landed on a car that is not even powered. So every
    send returns the command's `id` and the caller must match it against
    `ack.id` before it says the word "confirmed". Action alone is not enough --
    the same button pressed yesterday has the same action."""
    driver_message = car_link_ro()
    if driver_message is None:
        return {"ack": None}
    try:
        return {"ack": driver_message.read_lap_ack()}
    except Exception as e:
        return {"ack": None, "error": str(e)}


class StrategyBody(BaseModel):
    key: str


# --------------------------------------------------------------------------- #
# Editing the matrix, mid-race
# --------------------------------------------------------------------------- #
# THE STORE IS STILL constants.PROFILE_MATRIX, and the writer is still
# profile_manage.write_saved_matrix() -- the same one the Speed Profile Builder
# uses. It re-parses the whole file before replacing it and copies the old one
# into profiles/_backup/, so a matrix typed in at 3 a.m. cannot leave a
# constants.py that will not import. One store, one validator, one backup
# trail, whichever screen the edit came from.
#
# What is new here is that the change lands WITHOUT A RESTART: the Builder's
# own Save tells you to restart the Pit Web window, which is fine between
# sessions and useless with the car on track. constants.set_profile_matrix()
# adopts the new numbers in this process, and the plan cache is keyed on the
# table itself, so the next 10 s poll is already planning on them.
#
# Labels are NOT editable here. They are what /api/config handed the browser
# when the page loaded and what the strategy dropdown is built from; changing
# one mid-race would leave two names for the same profile on one screen. Rename
# in the Builder, where a reload comes with the territory.
class MatrixRow(BaseModel):
    key: str
    target_s: float
    energy_wh: float


class MatrixBody(BaseModel):
    rows: list[MatrixRow]


class MatrixFillBody(BaseModel):
    """One row the crew typed, and the ladder to rebuild around it."""
    key: str
    target_s: float
    energy_wh: float
    # The one assumption a single row cannot avoid; see energy_model.
    aero_share: float | None = None


def _matrix_rows():
    """The matrix as the editor shows it, in the order the table uses."""
    return [{"key": s["key"], "label": s["label"],
             "target_s": round(s["lap_time_min"] * 60.0, 2),
             "energy_wh": s["energy_wh"]}
            for s in C.STRATEGIES]


@app.get("/api/strategy/matrix")
def api_strategy_matrix():
    return {"rows": _matrix_rows(),
            "aeroShare": energy_model.DEFAULT_AERO_SHARE}


@app.post("/api/strategy/matrix/fill")
def api_strategy_matrix_fill(body: MatrixFillBody):
    """The other rows, derived from this one. Writes nothing."""
    try:
        rows = energy_model.ladder_from_anchor(
            _matrix_rows(), body.key, body.target_s, body.energy_wh,
            body.aero_share if body.aero_share is not None
            else energy_model.DEFAULT_AERO_SHARE)
    except ValueError as e:
        raise HTTPException(400, str(e))
    by_key = {r["key"]: r for r in rows}
    return {"rows": [dict(r, **by_key.get(r["key"], {})) for r in _matrix_rows()]}


@app.post("/api/strategy/matrix")
def api_strategy_matrix_save(body: MatrixBody):
    """Write the edited matrix and adopt it here. Labels come from the store."""
    import profile_manage as pm
    current = {s["key"]: s for s in C.STRATEGIES}
    draft = {}
    for r in body.rows:
        s = current.get(r.key)
        if s is None:
            raise HTTPException(400, "unknown profile %r" % r.key)
        # A lap time or a consumption of zero is not a slow car, it is a typo,
        # and the engine would plan an infinite number of free laps on it.
        if not (MATRIX_MIN_LAP_S <= r.target_s <= MATRIX_MAX_LAP_S):
            raise HTTPException(400, "%s: a lap time of %.1f s is outside %.0f-%.0f s"
                                % (s["label"], r.target_s, MATRIX_MIN_LAP_S,
                                   MATRIX_MAX_LAP_S))
        if not (MATRIX_MIN_WH <= r.energy_wh <= MATRIX_MAX_WH):
            raise HTTPException(400, "%s: %.1f Wh a lap is outside %.0f-%.0f Wh"
                                % (s["label"], r.energy_wh, MATRIX_MIN_WH,
                                   MATRIX_MAX_WH))
        # The STORED name ("Base"), never the displayed one ("Base (+0%)"):
        # constants.display_label() adds the percentage on the way out, and
        # writing the decorated string back would bake one edit's spacing into
        # the store and then decorate it again on the next read.
        draft[r.key] = {"label": (C.PROFILE_MATRIX.get(r.key) or {}).get("label")
                                 or s.get("name") or s["label"],
                        "target_s": r.target_s, "energy_wh": r.energy_wh}
    # Rows the caller did not send keep what they have: an editor that dropped
    # a profile because the browser was showing a stale list would be a silent
    # deletion, and PROFILE_MATRIX is also what the car is sent by key.
    for key, meta in C.PROFILE_MATRIX.items():
        draft.setdefault(key, dict(meta))
    try:
        pm.write_saved_matrix(draft)
    except Exception as e:                                  # noqa: BLE001
        raise HTTPException(500, "constants.py was not changed: %s" % e)
    C.set_profile_matrix(draft)
    return {"ok": True, "rows": _matrix_rows()}


@app.post("/api/strategy/select")
def api_strategy_select(body: StrategyBody):
    """Only the strategy NAME goes over the link: the car already holds all
    five generated profiles, so this is a few bytes rather than a 400-row
    table, and the profile it flies is the one committed to git.

    This is also the moment the PIT's own target speed changes -- see "The
    profile the PIT selected". The choice is stored BEFORE the radio send and
    stays stored if that send fails: a dead link does not unmake the pit's
    decision, and the caller is told the send failed either way. Storing it
    after would mean a radio glitch silently left every target readout on the
    previous profile.

    Moving the dropdown alone changes nothing; pressing Send does. A target
    speed that followed a stray scroll wheel would be a different kind of bug.
    """
    if not any(s["key"] == body.key for s in C.STRATEGIES):
        raise HTTPException(400, "unknown strategy %r" % body.key)
    set_pit_strategy_choice(body.key)
    driver_message = car_link()
    try:
        sent = driver_message.send_strategy(body.key)
    except Exception as e:
        raise HTTPException(502, "send failed: %s" % e)
    return {"ok": True, "key": body.key, "id": sent["id"]}


@app.get("/api/strategy/ack")
def api_strategy_ack():
    driver_message = car_link_ro()
    if driver_message is None:
        return {"ack": None}
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


# Every open /ws/live pusher's wake event, and the loop they all run on.
#
# A PRESS IN THE PIT CHANGES WHAT THE WALL SHOULD SHOW BEFORE THE CAR CAN
# ANSWER IT -- the lap clock is re-datumed, or parked, in SQLite by the button's
# own request. Without this the next push is up to FAST_TICK_S away, so a change
# the pit made itself took two seconds to appear on the screen of the person who
# made it, on top of whatever the car's round trip costs. Waking the pushers
# sends it within LIVE_MIN_PUSH_S instead.
#
# It reaches EVERY open device, not just the one that pressed: the engineer on
# the timing stand and the one on the pit wall are looking at the same clock.
#
# Cheap and self-limiting. A press is a human action, and the pushers' existing
# LIVE_MIN_PUSH_S floor already caps how close together two payloads can be, so
# this cannot become the message storm the cadence comment above describes.
_live_wakers = set()
_live_loop = None


def nudge_live():
    """Push the live payload to every open screen now. Safe from any thread.

    The endpoints run in FastAPI's threadpool, not on the event loop, so the
    events are set through the loop rather than touched directly. A no-op
    before the loop exists (an import by tools/check_*.py) and a no-op if it
    has gone -- a failed nudge only means the ordinary tick shows the change.
    """
    loop = _live_loop
    if loop is None:
        return

    def wake():
        for ev in _live_wakers:
            ev.set()

    try:
        loop.call_soon_threadsafe(wake)
    except RuntimeError:
        pass


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
    # Also woken by a press in the pit -- see nudge_live().
    _live_wakers.add(changed)
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
        _live_wakers.discard(changed)
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
                    # Only the drawn columns: a tick that appends twenty points
                    # has no use for twenty copies of raw_json.
                    rows, _, _ = db.fetch_series(
                        conn, _chart_columns(conn, chosen),
                        start_ts=since + 1e-6)
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
