"""
db.py — SQLite schema and helpers for the pit-side telemetry store
==================================================================
This local SQLite file is the pit's SOURCE OF TRUTH. The collector writes to it;
the dashboard and export read from it. Firebase is only the live feed.

Design choices
--------------
* PRIMARY KEY is the RTDB push key (`rtdb_key`). It is unique and chronological,
  so `INSERT OR IGNORE` makes ingest idempotent: replayed events after a
  reconnect (the boundary key always re-arrives because RTDB `startAt` is
  inclusive) silently no-op instead of duplicating.
* We keep the full record as `raw_json` (nothing is lost — every BMS cell, every
  flag), PLUS a handful of "hot" flattened columns for fast charting/filtering.
  Telemetry is wide (many metrics per sample), so a wide row beats an EAV
  (metric/value) table here: one sample = one row, no fan-out on read.
* `device_ts` is the CAR's timestamp, so history stays chronologically correct
  regardless of the order samples actually arrive in.
"""

import json
import sqlite3

from constants import (CONTROLLER_SPEED_DIVISOR, CONTROLLER_SPEED_DIVISOR_LEGACY,
                       RPM_REPORT_SCALE)
from pit_config import SQLITE_PATH, DEVICE_ID

# Hot columns extracted from each record for charting/filtering. The dashboard
# can name any of these as an exportable/plottable "metric". Anything not listed
# is still recoverable from raw_json.
METRIC_COLUMNS = [
    "bms_soc_percent",
    "bms_voltage_V",
    "bms_current_A",
    "battery_temp_C",
    "mms_rpm",
    "mms_power_W",
    "mms_temperature_C",
    # The motor controller's OWN measurements, from the LYNX frames. These were
    # published by the car all along but had no column, so they were dropped on
    # arrival and only recoverable by hand out of raw_json.
    #
    # `mms_measured_voltage_V` is the controller's pack voltage and is the one to
    # trust: bench-confirmed against the cell count, whereas `bms_voltage_V` above
    # reads ~2.25x high (112 V for a ~50 V pack) and its JBD decode is a known
    # open bug. Both are stored so the disagreement stays visible and diagnosable.
    "mms_measured_voltage_V",
    "mms_current_A",
    # THE road-speed source for the HUD, the pit tiles, the history charts and
    # the Excel export. Stored already decoded: the raw CAN field is 0.1 km/h
    # and is not gear-corrected, so mms_parser.decode_vehicle_speed_kmh() has
    # applied both corrections before it reaches here.
    #
    # ⚠️ Rows written before that decode fix hold the RAW value, roughly 50x
    # too high. They are wrong, not just old — back-fill before reading
    # historical speed (tools/backfill_columns.py).
    "mms_vehicle_speed_kmh",
    # Distance counter from the controller (0x620). A counter, not an integral,
    # so it does not accumulate error across dropped frames.
    "mms_trip_m",
    # The controller's own SoC estimate — independent of the BMS's, so a
    # disagreement between them is itself information.
    "mms_estimated_soc_percent",
    # Regen energy recovered, Wh. Sits beside total_race_energy, which is NET of
    # this, so having both is what lets you see gross consumption.
    "regen_energy",
    # What the car was TOLD to do at each moment, from the active speed profile.
    # Storing it makes "did the driver hold the target" answerable after the race
    # instead of only watchable live.
    "target_speed_kmh",
    # Motor PT1000 sensor. Both halves are stored: the converted °C is what the
    # pit reads during a race, and the raw Ω is what lets you re-derive it (or
    # spot a dead probe) afterwards without trusting the car's conversion.
    "mms_motor_ohms",
    "mms_motor_temp_C",
    # Active power map as the raw controller value. Numeric so it can be charted
    # as a step trace across a race; the human name is stored beside it below.
    "mms_motor_map_raw",
    # Per-lap analytics, all computed ON THE CAR (see lap_tracker.py). The Pi
    # holds each lap's figures for the whole of the FOLLOWING lap, so the pit
    # only has to receive one sample anywhere in a lap to record that lap
    # exactly — which is what makes the per-lap history survive a dropped link.
    # Energy is in Wh and is NET of regen, so it can legitimately decrease.
    "total_race_energy",
    "last_lap_energy",
    "last_lap_time_s",
    "last_lap_distance_m",
    "lap_distance_m",
    "odometer_m",
    "calculated_lap",
    "lat",
    "lon",
]

# Fault / error columns — surfaced and exported separately from the numeric
# metrics above. `bms_protections` is the comma-joined list of active JBD
# protection labels (e.g. "Cell Overvoltage, Discharge Overcurrent"); the
# *_error_code columns hold the raw bitmask words; the *_has_error flags are
# 0/1 (NULL when the device didn't report).
ERROR_COLUMNS = [
    "bms_has_error",
    "bms_error_code",
    "bms_protections",
    "mms_has_error",
    "mms_error_code",
    "mms_alerts",
]

# Textual state columns — not numbers to chart, not faults. The map NAME is
# stored as the car computed it (rather than re-deriving it here from the raw
# value) so the pit always reads exactly what the driver's badge reads, even if
# the two ever run different builds.
STATE_COLUMNS = [
    "mms_motor_map",
    # How the last lap was triggered: gps | odometer | manual | gps_no_can.
    # "odometer" means the GPS trigger MISSED and the distance backstop fired —
    # a visible signal that finish-line detection needs looking at.
    "lap_source",
]

# Every data column the dashboard/exporter can name, in a stable order.
EXPORT_COLUMNS = METRIC_COLUMNS + ERROR_COLUMNS + STATE_COLUMNS

# Column -> SQLite declared type. Numeric metrics are REAL; flags/codes are
# INTEGER; the protections summary is TEXT.
_COL_TYPES = {
    **{c: "REAL" for c in METRIC_COLUMNS},
    "bms_has_error": "INTEGER",
    "bms_error_code": "INTEGER",
    "bms_protections": "TEXT",
    "mms_has_error": "INTEGER",
    "mms_error_code": "INTEGER",
    "mms_alerts": "TEXT",
    "mms_motor_map": "TEXT",
    "lap_source": "TEXT",
}

_DATA_COL_DEFS = ",\n    ".join(f"{c} {_COL_TYPES[c]}" for c in EXPORT_COLUMNS)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS telemetry (
    rtdb_key          TEXT PRIMARY KEY,   -- RTDB push id (unique, chronological)
    device_id         TEXT NOT NULL,
    device_ts         REAL,               -- car timestamp (unix seconds)
    ingested_ts       REAL,               -- when the pit stored it
    {_DATA_COL_DEFS},
    raw_json          TEXT                -- full car_data, nothing dropped
);
CREATE INDEX IF NOT EXISTS idx_telemetry_device_ts ON telemetry (device_ts);
CREATE INDEX IF NOT EXISTS idx_telemetry_dev_ts    ON telemetry (device_id, device_ts);

-- Small key/value store for dashboard state that must survive a page refresh
-- (e.g. the race clock), so the pit engineer never has to re-enter it.
CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_conn(path: str = SQLITE_PATH) -> sqlite3.Connection:
    """Open a connection in WAL mode so the dashboard/export can read while the
    collector writes concurrently from another process."""
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    # Migrate older DBs in place: add any data columns the table is missing
    # (e.g. the fault columns added later). CREATE TABLE IF NOT EXISTS won't
    # alter an existing table, so we do it explicitly. Existing rows get NULL.
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(telemetry)")}
    for col in EXPORT_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE telemetry ADD COLUMN {col} {_COL_TYPES[col]}")
    # Partial index over only the fault rows — makes the errors-history query
    # cheap even on a huge table. Created here (not in _SCHEMA) so the fault
    # columns are guaranteed to exist first (after the migration above).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_telemetry_faults ON telemetry (device_ts) "
        "WHERE bms_has_error = 1 OR mms_has_error = 1"
    )
    # The per-lap charts GROUP BY calculated_lap on every history refresh.
    # Without this the 10s fragment full-scans the whole race every tick.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_telemetry_lap "
        "ON telemetry (device_id, calculated_lap)"
    )
    # One-time correction of historical rows: motor power is signed (negative on
    # regen) but was stored as a raw uint16, so regen samples read as ~65000 W.
    # Fold any such rows back to their true signed value. Idempotent — after the
    # first pass nothing exceeds the int16 range, and new rows are stored signed.
    conn.execute("UPDATE telemetry SET mms_power_W = mms_power_W - 65536 "
                 "WHERE mms_power_W > 32767")
    conn.commit()


# The two regimes the speed field can arrive in, as a ratio to motor RPM:
#
#   RAW        speed = rpm x 1.0366     (0.1 km/h, gear ratio never applied)
#   CORRECTED  speed = rpm x 0.020355   (true km/h)
#
# They differ by a factor of 50.909, so telling them apart is not a close call.
# The boundary below is the geometric mean of the two, which sits ~7x away from
# either regime — far outside any plausible measurement noise.
# Raw sits at rpm x 0.5183; a decoded value sits at rpm x (0.5183 / (10*DIV)).
# The boundary is their geometric mean, derived from the constants so it cannot
# silently go stale when one of them is corrected.
#
# 0.5183, not the 1.0366 that was here before, because mms_rpm now arrives
# ALREADY CORRECTED for the controller's 2x under-report (see
# drivetrain.RPM_REPORT_SCALE). The raw speed field did not change, so its ratio
# to a doubled RPM is halved. Getting this wrong would not fail loudly - it
# would quietly misfile decoded speeds as raw and divide them a second time.
_SPEED_RATIO_RAW = 1.0366 * RPM_REPORT_SCALE                    # 0.5183, legacy
_SPEED_RATIO_RAW_RECONFIGURED = 2.6656 * RPM_REPORT_SCALE        # 1.3328, current

# THREE regimes, not two, and a row says which it belongs to by the ratio of its
# speed field to its RPM. Boundaries are the geometric means, so each sits a
# factor of ~3 from either regime - far outside measurement noise.
#
#   ratio ~ 0.0204  the value is already true km/h        -> pass through
#   ratio ~ 0.5183  raw field, controller pre-2026-08-20  -> / 2.5455
#   ratio ~ 1.3328  raw field, controller post-2026-08-20 -> / 6.5455
#
# The middle regime is why this is not one constant: the controller was
# reconfigured mid-history and the same raw number means different speeds on
# either side of it. Using one divisor for both made every pre-reconfiguration
# row come out 2.57x too low.
_SPEED_DECODED_RATIO = _SPEED_RATIO_RAW / (10.0 * CONTROLLER_SPEED_DIVISOR_LEGACY)
_SPEED_BOUNDARY_DECODED = (_SPEED_DECODED_RATIO * _SPEED_RATIO_RAW) ** 0.5
_SPEED_BOUNDARY_ERA = (_SPEED_RATIO_RAW * _SPEED_RATIO_RAW_RECONFIGURED) ** 0.5
# Kept under the old name: fix_vehicle_speed.py imports it.
_SPEED_RATIO_BOUNDARY = _SPEED_BOUNDARY_DECODED


def _vehicle_speed(raw_speed, rpm):
    """Normalise the controller's speed field to true km/h.

    WHY THIS IS HERE AND NOT ONLY ON THE CAR
    mms_parser.decode_vehicle_speed_kmh() is the real fix, but it only takes
    effect once SolarRace_OS is deployed to the Pi. A car already on track keeps
    publishing the raw value, and those samples are useless if the pit stores
    them as km/h. This normalises at ingest so the pit is correct immediately,
    whichever firmware the car happens to be running.

    It stays correct AFTER the car is updated too, which is the point of using a
    ratio rather than a date or a version flag: an already-decoded value sits at
    0.0204 x rpm and is passed through untouched. No cutover to get right, and
    no window where the two ends disagree.

    With no RPM to compare against, it falls back to plausibility: above
    200 km/h the value is certainly still raw, and below that it is passed
    through unchanged. That ambiguous band is narrow in practice — RPM and
    speed ride in the same 0x610 frame, so one is rarely present without the
    other.
    """
    speed = _num(raw_speed)
    if speed is None:
        return None
    speed = abs(speed)
    if speed == 0.0:
        return 0.0                      # identical under either interpretation

    r = _num(rpm)
    if r is not None and abs(r) > 0:
        ratio = speed / abs(r)
        if ratio >= _SPEED_BOUNDARY_ERA:
            # Raw, from the reconfigured controller.
            return speed * 0.1 / CONTROLLER_SPEED_DIVISOR
        if ratio > _SPEED_BOUNDARY_DECODED:
            # Raw, from the controller as it was configured before 2026-08-20.
            return speed * 0.1 / CONTROLLER_SPEED_DIVISOR_LEGACY
        return speed                                        # already true km/h
    # No usable RPM. Fall back to plausibility: this car does not exceed 200
    # km/h, so anything above that is certainly still raw.
    if speed > 200.0:
        # No RPM, so the era is unknowable. Assume the current controller: it is
        # what a live car is running, and this branch only fires on a row that
        # arrived without RPM in the same frame, which is rare.
        return speed * 0.1 / CONTROLLER_SPEED_DIVISOR
    return speed


def _num(value):
    """Coerce to float when possible, else None — RTDB values arrive as
    int/float/str/bool depending on the parser."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _flag(value):
    """Boolean fault flag -> 1/0, or None when the device didn't report it."""
    if value is None:
        return None
    return 1 if value else 0


def _int(value):
    """Coerce to int (error-code bitmask) when possible, else None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _signed16(value):
    """Reinterpret a raw uint16 as a signed int16.

    Motor power is a SIGNED value (negative during regen/coasting), but the LYNX
    frame was historically decoded unsigned on the car, so small negatives arrive
    as ~65000. Fold them back to the true signed value. No-op once the car sends
    signed values (or for anything already in the int16 range) — a 2-byte field
    can't legitimately exceed 32767 W here anyway."""
    v = _num(value)
    if v is None:
        return None
    return v - 65536 if v > 32767 else v


def _join(value):
    """List of protection labels -> comma-joined text, or None if absent."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return ", ".join(str(x) for x in value)
    return str(value)


def flatten_record(rtdb_key: str, record: dict, device_id: str = DEVICE_ID) -> dict:
    """Turn one pushed record `{timestamp, car_data:{battery,motor,...}}` into a
    flat column dict ready for upsert."""
    record = record or {}
    car = record.get("car_data") or {}
    battery = car.get("battery") or {}
    motor = car.get("motor") or {}
    temp = car.get("temp_controller") or {}
    gps = car.get("gps") or {}

    return {
        "rtdb_key": rtdb_key,
        "device_id": device_id,
        "device_ts": _num(record.get("timestamp")),
        # ingested_ts is filled at write time (caller passes time.time())
        "bms_soc_percent": _num(battery.get("bms_soc_percent")),
        "bms_voltage_V": _num(battery.get("bms_voltage_V")),
        "bms_current_A": _num(battery.get("bms_current_A")),
        "battery_temp_C": _num(temp.get("battery_temp_C")),
        "mms_rpm": _num(motor.get("mms_rpm")),
        "mms_power_W": _signed16(motor.get("mms_power_W")),
        "mms_temperature_C": _num(motor.get("mms_temperature_C")),
        # Controller-side measurements — see the METRIC_COLUMNS notes. All live in
        # the "motor" block the car publishes.
        "mms_measured_voltage_V": _num(motor.get("mms_measured_voltage_V")),
        "mms_current_A": _num(motor.get("mms_current_A")),
        # Normalised, not stored verbatim: a car running pre-fix firmware sends
        # this in 0.1 km/h without the gear reduction. See _vehicle_speed().
        "mms_vehicle_speed_kmh": _vehicle_speed(
            motor.get("mms_vehicle_speed_kmh"), motor.get("mms_rpm")),
        "mms_trip_m": _num(motor.get("mms_trip_m")),
        "mms_estimated_soc_percent": _num(motor.get("mms_estimated_soc_percent")),
        "regen_energy": _num(motor.get("regen_energy")),
        "target_speed_kmh": _num(motor.get("target_speed_kmh")),
        "mms_motor_ohms": _num(motor.get("mms_motor_ohms")),
        "mms_motor_temp_C": _num(motor.get("mms_motor_temp_C")),
        "mms_motor_map_raw": _num(motor.get("mms_motor_map_raw")),
        "mms_motor_map": _join(motor.get("mms_motor_map")),
        "total_race_energy": _num(motor.get("total_race_energy")),
        "last_lap_energy": _num(motor.get("last_lap_energy")),
        "last_lap_time_s": _num(motor.get("last_lap_time_s")),
        "last_lap_distance_m": _num(motor.get("last_lap_distance_m")),
        "lap_distance_m": _num(motor.get("lap_distance_m")),
        "lap_source": _join(motor.get("lap_source")),
        "odometer_m": _num(motor.get("odometer_m")),
        "calculated_lap": _num(motor.get("calculated_lap")),
        "lat": _num(gps.get("lat")),
        "lon": _num(gps.get("lon")),
        # Faults
        "bms_has_error": _flag(battery.get("bms_has_error")),
        "bms_error_code": _int(battery.get("bms_error_code")),
        "bms_protections": _join(battery.get("bms_protections")),
        "mms_has_error": _flag(motor.get("mms_has_error")),
        "mms_error_code": _int(motor.get("mms_error_code")),
        "mms_alerts": _join(motor.get("mms_alerts")),
        "raw_json": json.dumps(car, separators=(",", ":")),
    }


_COLUMNS = ["rtdb_key", "device_id", "device_ts", "ingested_ts", *EXPORT_COLUMNS, "raw_json"]
_INSERT_SQL = (
    f"INSERT OR IGNORE INTO telemetry ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in _COLUMNS)})"
)


def upsert_many(conn: sqlite3.Connection, items, ingested_ts: float,
                device_id: str = DEVICE_ID) -> int:
    """Idempotently store a batch of (rtdb_key, record) pairs.

    Returns the number of NEW rows actually inserted (duplicates are ignored).
    `items` may be a dict {key: record} or an iterable of (key, record) pairs.
    """
    if isinstance(items, dict):
        items = items.items()

    rows = []
    for key, record in items:
        if not key or not isinstance(record, dict):
            continue
        row = flatten_record(key, record, device_id)
        row["ingested_ts"] = ingested_ts
        rows.append(row)

    if not rows:
        return 0

    before = conn.total_changes
    conn.executemany(_INSERT_SQL, rows)
    conn.commit()
    return conn.total_changes - before


def get_last_key(conn: sqlite3.Connection):
    """Stream cursor: the highest RTDB key stored so far, or None if empty.
    Push keys sort lexicographically in chronological order, so MAX() is the
    newest sample — the point to resume streaming from after a restart/dropout.
    Intentionally NOT filtered by device_id: the cursor tracks the whole node."""
    row = conn.execute("SELECT MAX(rtdb_key) AS k FROM telemetry").fetchone()
    return row["k"] if row else None


def save_cursor(conn: sqlite3.Connection, key) -> None:
    """Persist the last RTDB key seen, independent of the telemetry rows.
    Survives a history reset so the collector resumes from the tail instead of
    re-backfilling everything that was just cleared. No-op for a falsy key."""
    if not key:
        return
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES ('stream_cursor', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key,),
    )
    conn.commit()


def get_cursor(conn: sqlite3.Connection):
    """The persisted stream cursor, or None. Used as a fallback resume point
    when the telemetry table is empty (e.g. right after a history reset)."""
    row = conn.execute(
        "SELECT value FROM app_state WHERE key = 'stream_cursor'"
    ).fetchone()
    return row["value"] if row and row["value"] else None


def clear_history(conn: sqlite3.Connection, device_id: str = DEVICE_ID) -> int:
    """Delete all stored telemetry for a device. Returns the row count removed.

    Records the current stream position first so the collector picks up from the
    live tail afterwards rather than re-downloading the whole RTDB history. The
    race-clock state in app_state is left untouched."""
    save_cursor(conn, get_last_key(conn))
    cur = conn.execute("DELETE FROM telemetry WHERE device_id = ?", (device_id,))
    conn.commit()
    return cur.rowcount


def fetch_lap_summary(conn: sqlite3.Connection, device_id: str = DEVICE_ID):
    """One row per completed lap: (lap, energy_wh, lap_time_s, distance_m).

    Cheap on purpose. The CAR already did the integration and the timing, and
    holds each lap's figures constant for the whole of the following lap, so
    every sample within a lap carries identical values and MAX() just picks
    them up — no re-integration of power over time happens on the pit at all.

    Rows before a lap has completed carry NULL and are excluded, as are rows
    from before this feature existed.

    `calculated_lap` is stored REAL (everything numeric goes through _num), so
    it needs an explicit CAST to group cleanly.
    """
    return conn.execute(
        "SELECT CAST(calculated_lap AS INTEGER) AS lap, "
        "       MAX(last_lap_energy)  AS energy_wh, "
        "       MAX(last_lap_time_s)  AS lap_time_s, "
        "       MAX(last_lap_distance_m) AS distance_m "
        "FROM telemetry "
        "WHERE device_id = ? AND calculated_lap IS NOT NULL "
        "  AND (last_lap_energy IS NOT NULL OR last_lap_time_s IS NOT NULL) "
        "GROUP BY lap ORDER BY lap",
        (device_id,),
    ).fetchall()


def fetch_lap_track(conn: sqlite3.Connection, lap: int, device_id: str = DEVICE_ID):
    """(device_ts, lap_distance_m) for one lap, ascending — for sector timing.

    Only the two columns sector splits need, so a lap's worth of samples is a
    cheap read even at 1 Hz over a long race.
    """
    return conn.execute(
        "SELECT device_ts, lap_distance_m FROM telemetry "
        "WHERE device_id = ? AND CAST(calculated_lap AS INTEGER) = ? "
        "  AND device_ts IS NOT NULL AND lap_distance_m IS NOT NULL "
        "ORDER BY device_ts ASC",
        (device_id, int(lap)),
    ).fetchall()


def recent_laps(conn: sqlite3.Connection, count: int = 2,
                device_id: str = DEVICE_ID):
    """The newest `count` lap numbers that have samples, newest first."""
    rows = conn.execute(
        "SELECT DISTINCT CAST(calculated_lap AS INTEGER) AS lap FROM telemetry "
        "WHERE device_id = ? AND calculated_lap IS NOT NULL "
        "ORDER BY lap DESC LIMIT ?",
        (device_id, int(count)),
    ).fetchall()
    return [r["lap"] for r in rows]


def latest_sample(conn: sqlite3.Connection, device_id: str = DEVICE_ID):
    """Most recent sample by car timestamp — the dashboard's 'live' value."""
    return conn.execute(
        "SELECT * FROM telemetry WHERE device_id = ? "
        "ORDER BY device_ts DESC LIMIT 1",
        (device_id,),
    ).fetchone()


def count_samples(conn: sqlite3.Connection, device_id: str = DEVICE_ID) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM telemetry WHERE device_id = ?", (device_id,)
    ).fetchone()
    return int(row["n"]) if row else 0


def count_samples_since(conn: sqlite3.Connection, start_ts: float,
                        device_id: str = DEVICE_ID) -> int:
    """How many samples arrived at/after `start_ts`. Used by the History tab to
    say how much new data piled up while a frozen chart was being examined —
    a COUNT, so it never re-reads the rows it is counting."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM telemetry WHERE device_id = ? AND device_ts >= ?",
        (device_id, start_ts),
    ).fetchone()
    return int(row["n"]) if row else 0


def time_bounds(conn: sqlite3.Connection, device_id: str = DEVICE_ID):
    """(min_device_ts, max_device_ts) over stored samples, or (None, None)."""
    row = conn.execute(
        "SELECT MIN(device_ts) AS lo, MAX(device_ts) AS hi FROM telemetry "
        "WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    if not row:
        return None, None
    return row["lo"], row["hi"]


def fetch_samples(conn: sqlite3.Connection, start_ts: float = None,
                  end_ts: float = None, limit: int = None,
                  device_id: str = DEVICE_ID):
    """Rows ordered by car timestamp, optionally filtered by [start_ts, end_ts].
    When `limit` is set, returns the most recent `limit` rows (still ascending)."""
    clauses = ["device_id = ?"]
    params = [device_id]
    if start_ts is not None:
        clauses.append("device_ts >= ?")
        params.append(start_ts)
    if end_ts is not None:
        clauses.append("device_ts <= ?")
        params.append(end_ts)
    where = " AND ".join(clauses)

    if limit is not None:
        # newest `limit`, then re-sort ascending for charting
        sql = (f"SELECT * FROM (SELECT * FROM telemetry WHERE {where} "
               f"ORDER BY device_ts DESC LIMIT ?) ORDER BY device_ts ASC")
        params.append(limit)
    else:
        sql = f"SELECT * FROM telemetry WHERE {where} ORDER BY device_ts ASC"

    return conn.execute(sql, params).fetchall()


def fetch_faults(conn: sqlite3.Connection, limit: int = 2000,
                 device_id: str = DEVICE_ID):
    """Rows with a BMS or MMS fault flag set, ascending by car time.
    With `limit`, returns the most recent `limit` fault rows (still ascending).
    Backed by the partial fault index, so it stays cheap on a large table."""
    where = "device_id = ? AND (bms_has_error = 1 OR mms_has_error = 1)"
    if limit is not None:
        sql = (f"SELECT * FROM (SELECT * FROM telemetry WHERE {where} "
               f"ORDER BY device_ts DESC LIMIT ?) ORDER BY device_ts ASC")
        return conn.execute(sql, (device_id, limit)).fetchall()
    sql = f"SELECT * FROM telemetry WHERE {where} ORDER BY device_ts ASC"
    return conn.execute(sql, (device_id,)).fetchall()


# --------------------------------------------------------------------------- #
# Race state (persisted so a browser refresh keeps the running race)
# --------------------------------------------------------------------------- #
def save_race_state(conn: sqlite3.Connection, is_racing, race_start_time) -> None:
    """Persist the race clock. `race_start_time` is a unix epoch (or None)."""
    payload = json.dumps({
        "is_racing": bool(is_racing),
        "race_start_time": race_start_time,
    })
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES ('race', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (payload,),
    )
    conn.commit()


def load_race_state(conn: sqlite3.Connection) -> dict:
    """Return {'is_racing': bool, 'race_start_time': float|None}; defaults if unset."""
    row = conn.execute("SELECT value FROM app_state WHERE key = 'race'").fetchone()
    if row and row["value"]:
        try:
            data = json.loads(row["value"])
            return {
                "is_racing": bool(data.get("is_racing")),
                "race_start_time": data.get("race_start_time"),
            }
        except (ValueError, TypeError):
            pass
    return {"is_racing": False, "race_start_time": None}
