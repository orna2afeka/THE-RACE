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
import pathlib
import sqlite3

from constants import (CONTROLLER_SPEED_DIVISOR, CONTROLLER_SPEED_DIVISOR_LEGACY,
                       RPM_REPORT_SCALE)
from pit_config import SQLITE_PATH, DEVICE_ID
# Repo-root modules, importable because constants (above) put the root on
# sys.path. Shared with the car so the pit's rule 3.5.6 report applies the same
# gates as the HUD's.
import cell_extremes                                  # noqa: E402
import limits                                         # noqa: E402

# Hot columns extracted from each record for charting/filtering. The dashboard
# can name any of these as an exportable/plottable "metric". Anything not listed
# is still recoverable from raw_json.
# How many individual cell-voltage columns to carry. 30, not the currently-
# wired count, because it is the true UPPER BOUND of what the BMS protocol can
# address at all: cell voltages arrive 3-per-frame over 10 CAN IDs (0x107..
# 0x110 — see bms_parser.py), so 30 is the most this wiring could ever report
# without a protocol change, regardless of how many taps are physically
# connected today.
#
# Deliberately NOT the live "how many are actually wired" count — that number
# is not even constant across this project's own history (bms_string_count has
# been seen as both 13 and 28 in stored samples, presumably as the pack was
# built out), so hard-coding today's figure here would need a second schema
# migration the next time a cell gets added. bms_string_count is stored as its
# own column instead (below) and is what a display must gate on, per-row, to
# tell a real 0.000 V-if-that-ever-happens from "this tap isn't wired yet."
BMS_CELL_COLUMN_COUNT = 30

# How many per-thermistor temperature COLUMNS to carry. Sized to the wiring,
# not to DS003's 30: the Orion module on this car reports 26 thermistors
# enabled across an id range that runs past 30 (ids 1-13 and 21-onwards, with
# 14-20 never loaded), so 30 columns silently truncated the last few real
# sensors into raw_json only.
#
# 40 covers that range with headroom while staying far below the module's own
# 80-thermistor ceiling (temp_controller_parser.THERMISTOR_MAX, which is what
# the DECODER bounds against). A column that is never populated costs nothing
# — 38 of this table's columns have never held a value — whereas a reading
# with nowhere to land is gone from every query and every export.
#
# Not imported from the car-side module: that lives under SolarRace_OS/modules,
# off this app's sys.path. Kept in sync by hand, the same way
# DS004_MODULE_COUNT in Pit_Web/api.py is its own independent constant.
THERMISTOR_CELL_COLUMN_COUNT = 40

METRIC_COLUMNS = [
    "bms_soc_percent",
    "bms_voltage_V",
    "bms_current_A",
    # The same three from BMS B (can1), as main._remap_bms_frame names them.
    # The car has published them all along; before these columns existed they
    # were only in raw_json, and the pit showed battery A's SoC as if it were
    # the whole car's.
    "bms2_soc_percent",
    "bms2_voltage_V",
    "bms2_current_A",
    # How many cell taps the BMS itself reports as configured (ID 0x104). The
    # authoritative "is this cell real" signal for the bms_cell_NN_V columns
    # below — see BMS_CELL_COLUMN_COUNT for why that fixed 30 is a wiring limit,
    # not a live count.
    "bms_string_count",
    # Individual cell voltages, 1-indexed to match the BMS's own numbering.
    # Absent (None) for any cell beyond what was polled/wired for a given
    # sample, exactly like every other "car never reported this" field here —
    # never coalesced to 0, so an unwired tap cannot be mistaken for a shorted
    # cell. See bms_parser.py's cell-voltage decode for the source.
    *[f"bms_cell_{i:02d}_V" for i in range(1, BMS_CELL_COLUMN_COUNT + 1)],
    # Battery temp = the hottest plausible Orion cell (limits.
    # battery_temp_from_cells), derived at ingest from the cell readings in the
    # record. Rows stored before 2026-09-16 hold the Orion module's AVERAGE
    # here instead; they were left as they are.
    "battery_temp_C",
    # The two JBD BMS units' own NTC probes (frame 0x105, three per BMS), shown
    # live on the Cell Voltages tab and the HUD's R3.5.6 screen. bms_ = BMS A
    # (can0), bms2_ = BMS B (can1), as main._remap_bms_frame names them. Before
    # these columns existed the readings were only in raw_json.
    *[f"bms_temp_{i}_C" for i in (1, 2, 3)],
    *[f"bms2_temp_{i}_C" for i in (1, 2, 3)],
    # DS003 — individual cell temperatures from the Orion Thermistor
    # Expansion Module's per-sensor round-robin broadcast (0x1838F3xx), NOT
    # from the BMS's 3 onboard NTC probes.
    # Absent (None) for any cell not yet loaded/enabled on the module via
    # Orion's own utility software — see temp_controller_parser.py's
    # docstring for why there is no wire signal that means "not configured",
    # only the absence of a value ever arriving. Never coalesced to 0, same
    # reasoning as bms_cell_NN_V above.
    *[f"bms_cell_temp_{i:02d}_C" for i in range(1, THERMISTOR_CELL_COLUMN_COUNT + 1)],
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
    # too high. They are wrong, not just old. The one-off repair scripts that
    # rewrote them have been removed now that every live database has had them
    # applied; anything read out of a pre-fix .bak still needs correcting by
    # hand, using _SPEED_RATIO_BOUNDARY below.
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
    # Throttle pedal position, 0-100 %, from the ESC's GPIO0 reading. The
    # pit-wall coaching signal: a trace full of spikes is a driver pumping the
    # pedal, a flat one is the steady input that wins an endurance race.
    "mms_throttle_percent",
    # The RAW millivolts the same reading came from. Stored for two reasons, both
    # of which the PT1000 pair above already demonstrate the value of:
    #   * It is what the team reads off the pit wall to replace the placeholder
    #     pedal calibration in efficiency.py (the released/floored voltages).
    #   * Until that calibration is measured, every percentage above is only
    #     approximately right — so keeping the raw value means the whole race
    #     can be RE-DERIVED afterwards once the real span is known, instead of
    #     being permanently stored at whatever the placeholder implied.
    "mms_throttle_mv",
    # Per-lap analytics, all computed ON THE CAR (see lap_tracker.py). The Pi
    # holds each lap's figures for the whole of the FOLLOWING lap, so the pit
    # only has to receive one sample anywhere in a lap to record that lap
    # exactly — which is what makes the per-lap history survive a dropped link.
    # Energy is in Wh and is NET of regen, so it can legitimately decrease.
    "total_race_energy",
    "last_lap_energy",
    # Regen counterpart of the two above. "last_lap_regen_energy" needs no
    # lap-boundary special case of its own here — it is held for the whole of
    # the following lap by the SAME mechanism as last_lap_energy (see
    # lap_tracker.py's snapshot() docstring).
    "last_lap_regen_energy",
    # Since the last detected charging stop (charge_detector.py on the car),
    # NOT since the last lap. Reads the same as total_race_energy/regen_energy
    # until the first charging stop this race — see LapTracker.mark_stint_start.
    "stint_energy",
    "stint_regen_energy",
    "last_lap_time_s",
    "last_lap_distance_m",
    "lap_distance_m",
    "odometer_m",
    "calculated_lap",
    "lat",
    "lon",
    # How old the position on this row is, in seconds, straight from the car's
    # GPSReader. Without it lat/lon cannot be read at all: the car keeps serving
    # the LAST KNOWN fix once the receiver loses lock, on purpose (a frozen dot
    # beats an empty map), so a row can carry a perfectly well-formed position
    # from twenty minutes ago while the car is a kilometre down the track. Any
    # consumer that draws the position must gate on this.
    "gps_age_s",

    # --- Laps, as the car's gate-based tracker tags them ------------------ #
    # (SolarRace_OS/modules/lap_tracker.py). All NULL on rows from a car that
    # predates them; every consumer must cope with that.
    #
    # calculated_lap is laps COMPLETED and restarts whenever the car's counter
    # does, so neither it nor calculated_lap + 1 can name a lap safely:
    #   current_lap      the lap being driven
    #   last_lap_number  the lap the last_lap_* figures on this row belong to
    #   lap_seq          laps ever counted by this tracker; the pit cannot set
    #                    it, so it does not repeat when someone corrects the
    #                    lap number. fetch_laps() keys on it.
    "current_lap",
    "last_lap_number",
    "lap_seq",
    # Seconds the car stood still during the last lap. A pit stop lives here.
    "last_lap_stopped_s",
    # WALL CLOCK (time.time() on the car) of the moment the lap being driven
    # began -- lap_tracker sets it at the same instant as the HUD's own
    # stopwatch datum, and its comment says it "exists to be compared against
    # the pit's clock". The car has sent it all along and the pit dropped it on
    # the floor, so the dashboard had no way to show the clock the driver is
    # reading. Wall clock, not monotonic, so it IS comparable with device_ts.
    "lap_started_ts",
    # Lap distance from GPS, NULL in the pit lane or without a fresh fix.
    # Unlike lap_distance_m it cannot be out of phase with the track.
    "track_pos_m",
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
    # Which efficiency zone the DRIVER was actually shown — "eco" | "normal" |
    # "power" — as the car classified it. Stored rather than re-derived here for
    # the same reason mms_motor_map is: after the race, "we radioed them because
    # they were in the red" has to be answerable from what the HUD displayed,
    # not from re-running today's thresholds over yesterday's percentages. That
    # distinction matters precisely because efficiency.py's boundaries are
    # placeholders and WILL change.
    "mms_throttle_zone",
    # How the last lap was triggered: gps | odometer | manual | gps_no_can.
    # "odometer" means the GPS trigger MISSED and the distance backstop fired —
    # a visible signal that finish-line detection needs looking at.
    "lap_source",
    # What kind of lap the last one was: flying | in | out | in_out | start |
    # suspect. ONLY "flying" laps are fit to build energy and strategy figures
    # from. last_lap_flags says why a lap is not flying, comma-separated
    # (ended_in_pit, virtual_end, distance_suspect, interrupted, stopped, ...).
    "last_lap_kind",
    "last_lap_flags",
    # Where the car is: track | pit_lane | box. NULL until GPS has said.
    "zone",
    # Which speed profile the car was following, as the CAR reports it. The car
    # has always published this and the pit used to drop it on the floor, which
    # meant a stored lap could not be attributed to the profile it was driven
    # under — so "what does 189s pace actually cost per lap" was unanswerable
    # from the record, and the strategy matrix had to keep using numbers
    # somebody estimated before the car ever turned a wheel.
    "active_strategy",

    # --- Pi health ------------------------------------------------------- #
    # What the car can say about ITSELF, independent of anything on the CAN
    # bus. These arrive on a heartbeat that fires even with no CAN traffic and
    # no GPS fix, which is the one combination that used to publish nothing at
    # all -- leaving "CAN unplugged in the garage" looking exactly like "the Pi
    # is dead". See HEARTBEAT_INTERVAL_S in SolarRace_OS/main.py.
    "pi_uptime_s",
    "can_state",        # starting | live | silent | disconnected
    "can_silent_s",     # since the last frame on ANY bus; None if none open
    "can_detail",       # names only the QUIET channels, e.g. "can1 silent 47s"
    "can_frames",
    "gps_fix",          # 1 / 0
    "gps_detail",

    # --- the shared stopwatch -------------------------------------------- #
    # The ONE clock the driver's HUD, the pit wall and the public page all
    # show, moved from either end. Elapsed seconds on the display, and whether
    # it is still moving.
    #
    # Elapsed rather than a start timestamp on purpose: the car measures it
    # with time.monotonic(), which nothing can step, and the Pi has no RTC --
    # NTP shifts its wall clock minutes at a time after boot, which is exactly
    # what made gps fix ages read 3444 s on 2026-09-18. A reader that wants it
    # live adds the age of the row it came in.
    "stopwatch_s",
    "stopwatch_stopped",

    # --- is a charger on the car right now -------------------------------- #
    # 1/0, from charge_detector.py on the car: stationary AND a sustained
    # current into the pack. Inferred, not reported -- nothing on this car has
    # a "charger connected" signal -- so read it as the car's best evidence,
    # not as a contact closure.
    #
    # NULL on every row from a car that predates this column, which is every
    # row recorded before 2026-09-19. That is NOT "not charging": treat NULL as
    # unknown and show nothing, or the history charts will grow a confident
    # flat "never charged" line across the whole of practice.
    "is_charging",
]

# Every data column the dashboard/exporter can name, in a stable order.
EXPORT_COLUMNS = METRIC_COLUMNS + ERROR_COLUMNS + STATE_COLUMNS

# THE COLUMNS THE HISTORY CHARTS DRAW, and the reason idx_telemetry_chart
# exists. Every Metric.source in Pit_Dashboard/metrics.py must appear here or
# its chart falls off the fast path -- tools/check_history.py fails if one
# does. Ordered as metrics.py lists them, so the two read alike.
#
# WHY AN INDEX OVER FIFTEEN COLUMNS IS WORTH IT. A telemetry row is ~4.6 kB,
# almost all of it raw_json, and SQLite stores rows whole: reading one number
# out of every row still drags the entire 170 MB store through the page cache.
# Measured on the pit's own store (36,724 rows): the History tab's widest
# window took 2.8 s just to scan, and 35.8 s the way it was being asked. With
# these columns carried IN the index the same query is index-only and never
# touches the table -- 0.15 s for all fifteen metrics at once.
#
# It costs a few MB and one more index to maintain per insert, against a
# collector writing about two rows a second. That is not a trade, it is a gift.
CHART_COLUMNS = (
    "mms_vehicle_speed_kmh", "mms_throttle_percent", "mms_throttle_mv",
    "mms_power_W", "mms_rpm", "bms_soc_percent", "mms_measured_voltage_V",
    "bms_current_A", "battery_temp_C", "mms_motor_temp_C",
    "mms_temperature_C", "mms_motor_ohms", "odometer_m", "calculated_lap",
    "total_race_energy",
)
_not_stored = [c for c in CHART_COLUMNS if c not in EXPORT_COLUMNS]
if _not_stored:
    raise RuntimeError("CHART_COLUMNS names columns the store has no room "
                       "for: %s" % _not_stored)

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
    "mms_throttle_zone": "TEXT",
    "lap_source": "TEXT",
    "last_lap_kind": "TEXT",
    "last_lap_flags": "TEXT",
    "zone": "TEXT",
    "active_strategy": "TEXT",
    "pi_uptime_s": "REAL",
    "can_silent_s": "REAL",
    "can_state": "TEXT",
    "can_detail": "TEXT",
    "gps_detail": "TEXT",
    "gps_fix": "INTEGER",
    "can_frames": "INTEGER",
    "stopwatch_s": "REAL",
    "stopwatch_stopped": "INTEGER",
    "is_charging": "INTEGER",
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

-- The last known-non-null value of every metric, per device. Separate from
-- telemetry on purpose: telemetry keeps real NULLs/gaps for History, Export
-- and fault-episode detection, while the live view falls back to this table
-- so a tile never blanks out just because the newest row happens to be NULL
-- for that one field. Narrow/long (not a wide table mirroring METRIC_COLUMNS)
-- so a newly-added metric needs no schema migration -- it just starts getting
-- rows here the first time it's non-null.
CREATE TABLE IF NOT EXISTS last_known (
    device_id   TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value_num   REAL,
    value_text  TEXT,
    device_ts   REAL NOT NULL,
    PRIMARY KEY (device_id, metric)
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
    # Cap the write-ahead log at 32 MB when it is next reset. A no-op on its
    # own -- it takes effect only when a checkpoint actually succeeds, which is
    # what collector._maybe_checkpoint is for. Without both halves the WAL grows
    # for the life of the file: this store had reached 151 MB beside a 290 MB
    # database, and every page lookup in every query paid to search it.
    conn.execute("PRAGMA journal_size_limit=33554432;")
    return conn


def get_conn_ro(path: str = SQLITE_PATH):
    """A connection that CANNOT write, for tools that must not disturb the race.

    Returns (conn, mode) where mode is "ro" or "query_only", so a caller can say
    on screen which protection it actually got.

    The profile builder runs beside a live collector and a live pit wall against
    the same file. `mode=ro` is the strong form -- SQLite refuses writes at the
    VFS layer -- but it needs to create the -shm file to read a WAL database, and
    a read-only directory (or a stale -shm) makes the open fail outright. The
    fallback is a normal handle with `query_only=ON`, which refuses writes at the
    SQL layer instead: weaker (a PRAGMA could turn it off) but identical in
    practice for code that never tries.

    NOTE: nothing that uses this may call init_db(). That function runs DDL --
    ALTER TABLE, CREATE INDEX, and a full-table UPDATE -- and would fail here,
    correctly, but only after the caller had already assumed a schema.
    """
    uri = "file:" + pathlib.Path(path).as_posix().replace("?", "%3f") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("SELECT 1 FROM telemetry LIMIT 1")   # prove it really opened
        return conn, "ro"
    except sqlite3.Error:
        conn = sqlite3.connect(path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA query_only=ON;")
        return conn, "query_only"


def fetch_lap_profile_samples(conn: sqlite3.Connection, lap: int,
                              device_id: str = DEVICE_ID):
    """(device_ts, lap_distance_m, mms_vehicle_speed_kmh, lap_source) for one lap.

    ⚠️ SUPERSEDED, AND UNUSED. Use fetch_trace_samples(). This returns every row
    that ever carried this lap number, across every drive that used it -- and
    the counter restarts, so in this project's own store that means three
    separate evenings welded into one "lap 1". Nothing calls this any more;
    it is left only because the paragraph below is the clearest statement of
    the off-by-one anywhere in the codebase. Calling it reintroduces the bug.

    A SIBLING of fetch_lap_track, not a widening of it. That one is on the 4s
    cached path of the tab the dashboard opens on, and its two-column shape is
    deliberate; this adds two more columns for a tool that runs a handful of
    times by hand. Same half-open range on the raw column for the same reason --
    see fetch_lap_track's docstring for why a CAST here costs 141 ms instead of
    0.08 ms.

    ⚠️ THE OFF-BY-ONE. `calculated_lap` is LapTracker.lap_count: the number of
    laps COMPLETED. In the same snapshot `lap_distance_m` is the lap being driven
    and `last_lap_time_s` is the one just finished. So these samples are the
    trace of lap `lap` + 1, and that lap's time/energy/distance live on the rows
    tagged `lap` + 1 (fetch_lap_summary's row `lap` + 1).

    Confirmed on the real store, not just read off lap_tracker.py: for the trace
    tagged 0, MAX(lap_distance_m) is 3990 m while last_lap_distance_m at 0 is
    NULL and at 1 is 4020 m. Join these two the naive way and every profile is
    filed under the wrong lap's time -- a wrong answer that looks completely
    plausible, which is why profile_build.check_lap_alignment() re-proves it at
    runtime instead of trusting this comment.
    """
    return conn.execute(
        "SELECT device_ts, lap_distance_m, mms_vehicle_speed_kmh, lap_source "
        "FROM telemetry "
        "WHERE device_id = ? AND calculated_lap >= ? AND calculated_lap < ? "
        "  AND device_ts IS NOT NULL AND lap_distance_m IS NOT NULL "
        "ORDER BY device_ts ASC",
        (device_id, float(int(lap)), float(int(lap)) + 1.0),
    ).fetchall()


def has_lap_tags(conn: sqlite3.Connection) -> bool:
    """Does this store have the gate-based tracker's columns yet?

    init_db() adds them, and only the COLLECTOR runs init_db(): the web backend
    opens the store read-only. So between pulling this code and restarting the
    collector, the backend is reading a store without them, and a query that
    names one raises "no such column" - which took /api/laps down. Every lap
    query asks here first and reads the old shape until the columns exist.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(telemetry)")}
    return {"lap_seq", "last_lap_kind", "last_lap_number"} <= cols


def _kind_expr(conn: sqlite3.Connection) -> str:
    return "MAX(last_lap_kind)" if has_lap_tags(conn) else "NULL"


def lap_energy_by_strategy(conn: sqlite3.Connection, recent_laps: int = 60,
                           device_id: str = DEVICE_ID):
    """Measured energy per lap, grouped by the profile the lap was driven under.

    Returns {strategy_key: [wh, wh, ...]} — the raw per-lap figures, recent laps
    only, for the caller to take a median of. Energy is the car's own
    integration (signed motor power, trapezoidal, regen subtracting), so nothing
    is re-derived here: this reads a number the car already computed and held
    for the whole of the following lap.

    MINDS THE OFF-BY-ONE, and it matters more here than anywhere else.
    `calculated_lap` counts COMPLETED laps, so rows tagged N carry lap N's
    energy in last_lap_energy while their active_strategy is the profile being
    followed during lap N+1. Pairing those two off the same row attributes every
    lap's cost to the NEXT lap's profile — invisible while nobody changes
    strategy, and wrong exactly when someone does, which is the moment this
    number is being looked at. So the strategy for lap N comes from the rows
    tagged N-1, the trace of that lap.

    Bounded to the most recent `recent_laps` laps: a median over the whole race
    would average this morning's conditions into tonight's answer, and the query
    stays the same size at lap 400 as at lap 40.
    """
    top = conn.execute(
        "SELECT MAX(calculated_lap) AS m FROM telemetry WHERE device_id = ?",
        (device_id,)).fetchone()
    if not top or top["m"] is None:
        return {}
    floor = max(0.0, float(top["m"]) - float(recent_laps))

    rows = conn.execute(
        "SELECT CAST(calculated_lap AS INTEGER) AS lap, "
        "       MAX(last_lap_energy) AS energy_wh, "
        "       " + _kind_expr(conn) + " AS kind, "
        "       active_strategy AS strat, COUNT(*) AS n "
        "FROM telemetry "
        "WHERE device_id = ? AND calculated_lap >= ? "
        "GROUP BY lap, strat",
        (device_id, floor)).fetchall()

    energy, modal = {}, {}
    for r in rows:
        lap = r["lap"]
        # An in-lap, an out-lap or a lap the car marked suspect is not what a
        # lap of this profile costs. Untagged laps (an older car) stay in.
        if r["kind"] and r["kind"] != FLYING:
            energy[lap] = None
            continue
        if lap in energy and energy[lap] is None:
            continue
        if r["energy_wh"] is not None:
            energy[lap] = max(energy.get(lap, float("-inf")), float(r["energy_wh"]))
        if r["strat"]:
            best = modal.get(lap)
            if best is None or r["n"] > best[1]:
                modal[lap] = (r["strat"], r["n"])

    out = {}
    for lap, wh in energy.items():
        driven_under = modal.get(lap - 1)      # the trace of THIS lap
        if driven_under and wh is not None:
            out.setdefault(driven_under[0], []).append(wh)
    return out


def laps_measured(conn: sqlite3.Connection, recent_laps: int = 60,
                  device_id: str = DEVICE_ID):
    """Every recent lap as {lap, energy_wh, lap_time_s, distance_m, strategy}.

    lap_energy_by_strategy() answers "what did a lap on profile X cost", which
    is the right question ONLY when the lap really was flown at X's pace. This
    answers the more careful version — what a lap cost AND how fast and how far
    it actually was — so a caller can check that for itself.

    That check is not academic. A lap cut by the ODOMETER fallback rather than
    the GPS line is 4200 m, not 4000 (track.ODOMETER_FORCE_LAP_M), and carries
    whatever pace the car happened to be doing; `active_strategy` still names
    the profile the pit last SENT. Costing such a lap as though it were a lap
    of that profile is how a bench session at 50 km/h ends up setting the
    energy budget for a race at 68.

    MINDS THE SAME OFF-BY-ONE as lap_energy_by_strategy, for the same reason:
    rows tagged N carry lap N's figures in last_lap_*, while their
    active_strategy is the profile being followed during lap N+1. So the
    strategy for lap N comes from the rows tagged N-1.

    Bounded to the most recent `recent_laps` laps, as that function is.
    """
    top = conn.execute(
        "SELECT MAX(calculated_lap) AS m FROM telemetry WHERE device_id = ?",
        (device_id,)).fetchone()
    if not top or top["m"] is None:
        return []
    floor = max(0.0, float(top["m"]) - float(recent_laps))

    rows = conn.execute(
        "SELECT CAST(calculated_lap AS INTEGER) AS lap, "
        "       MAX(last_lap_energy)     AS energy_wh, "
        "       MAX(last_lap_time_s)     AS lap_time_s, "
        "       MAX(last_lap_distance_m) AS distance_m, "
        "       MAX(lap_source)          AS lap_source, "
        "       " + _kind_expr(conn) + " AS kind, "
        "       active_strategy          AS strat, COUNT(*) AS n "
        "FROM telemetry "
        "WHERE device_id = ? AND calculated_lap >= ? "
        "GROUP BY lap, strat",
        (device_id, floor)).fetchall()

    facts, modal = {}, {}
    for r in rows:
        lap = r["lap"]
        cur = facts.setdefault(lap, {"lap": lap, "energy_wh": None,
                                     "lap_time_s": None, "distance_m": None,
                                     "lap_source": None, "strategy": None,
                                     "kind": None})
        if r["kind"] and not cur["kind"]:
            cur["kind"] = r["kind"]
        for col in ("energy_wh", "lap_time_s", "distance_m"):
            if r[col] is not None:
                v = float(r[col])
                cur[col] = v if cur[col] is None else max(cur[col], v)
        if r["lap_source"] and not cur["lap_source"]:
            cur["lap_source"] = r["lap_source"]
        if r["strat"]:
            best = modal.get(lap)
            if best is None or r["n"] > best[1]:
                modal[lap] = (r["strat"], r["n"])

    out = []
    for lap in sorted(facts):
        f = facts[lap]
        driven_under = modal.get(lap - 1)          # the trace of THIS lap
        f["strategy"] = driven_under[0] if driven_under else None
        # Only flying laps describe what a lap costs. A lap with no kind comes
        # from a car that predates the tags and is kept, as it always was.
        if f["kind"] in (None, FLYING):
            out.append(f)
    return out


def lap_overview(conn: sqlite3.Connection, device_id: str = DEVICE_ID):
    """One grouped pass over every lap TRACE: cheap enough to run on a 300 MB
    store, and the only query the builder's lap table needs before a human has
    shortlisted anything.

    Per-lap detail (gaps, coverage) costs a full read of that lap's samples, so
    it is deliberately NOT here -- it is computed for the few laps that survive
    this table's filters.
    """
    return conn.execute(
        "SELECT CAST(calculated_lap AS INTEGER) AS trace_lap, "
        "       COUNT(*) AS n_samples, "
        "       SUM(mms_vehicle_speed_kmh IS NOT NULL) AS n_speed, "
        "       MAX(lap_distance_m) AS trace_end_m, "
        "       MIN(device_ts) AS t0, MAX(device_ts) AS t1, "
        "       MAX(ABS(mms_vehicle_speed_kmh)) AS v_max_kmh, "
        "       MAX(lap_source) AS lap_source "
        "FROM telemetry "
        "WHERE device_id = ? AND calculated_lap IS NOT NULL "
        "GROUP BY trace_lap ORDER BY trace_lap",
        (device_id,),
    ).fetchall()


# --------------------------------------------------------------------------- #
# Lap TRACES — one row per drive, not one per lap number
# --------------------------------------------------------------------------- #
# `calculated_lap` IS NOT UNIQUE. It restarts whenever the car's lap counter is
# reset -- a fresh image, a cleared checkpoint, a new session -- so the same
# number comes back days later. lap_overview() above groups by it alone, which
# welds those drives into one "lap": in this project's own store lap 1 is three
# separate evenings, and fetch_lap_summary() then hands it the MAX lap time and
# MAX energy across all of them.
#
# Nothing had been built from that yet, but it is not a bench-only problem.
# Reset the counter between practice and the race at Zolder and every number
# repeats, with ~4000 m traces that pass every check the builder applies.
#
# So a TRACE is (lap number, run): a stretch of one lap number's rows with no
# gap longer than TRACE_GAP_S. Splitting on a gap WITHIN a lap number -- rather
# than cutting the whole store into sessions -- is deliberate. A store-wide
# split has to decide what a session boundary is, and the obvious signal (the
# lap counter going down) fires 2025 times here for a completely different
# reason: three copies of the car code were once publishing at the same time
# under one device_id, so consecutive rows hop between three lap sequences.
# Per-lap-number splitting does not care, and interleaving is caught separately
# by profile_build.count_backward_jumps().
TRACE_GAP_S = 300.0

# ── Standstills, and why they matter more than gaps ───────────────────────── #
# A DRIVER CHANGE DOES NOT PRODUCE A GAP. The Pi stays powered through it and
# keeps publishing speed-0 samples, and calculated_lap only moves when the car
# crosses the finish line — so driver A's in-lap, the standstill and driver B's
# out-lap arrive as ONE lap number with no gap anywhere in it, and TRACE_GAP_S
# above never fires. Confirmed here: trace L0R53 of 26 Aug is a single 1423 s
# run holding a 730 s standstill at lap_distance_m = 3670.
#
# So the stop has to be found INSIDE a trace, which is what the islands CTE in
# lap_traces does. Mirrored in profile_build.STOP_MOVING_KMH and friends, which
# hold the same rule in Python for one focused lap (and let a self-test compare
# the two).
#
# Below this the car is not moving. Not zero on purpose: the controller's speed
# field jitters around standstill, and an "= 0" test shatters one 730 s stop
# into dozens of two-sample fragments that pass no threshold at all.
STOP_MOVING_KMH = 1.0
# The shortest standstill worth reporting. The UI's pit threshold is NOT applied
# here — profile_build.classify_traces() applies it — so dragging that slider
# costs nothing instead of invalidating this query's ~2 s cached result.
STOP_MIN_S = 2.0
# One lap_distance_m quantum: the store steps distance in 10 m, so a car that
# really was stationary can still show one step of movement.
STOP_DISTANCE_QUANTUM_M = 10.0

# How close the next lap's run must start to a trace ending for the two to be
# the same drive. The car crosses the line and increments in the same sample, so
# in practice this is well under a second; the slack covers a dropped sample or
# two at exactly the wrong moment.
TRACE_JOIN_SLACK_S = 30.0


def store_watermark(conn: sqlite3.Connection, device_id: str = DEVICE_ID):
    """(newest device_ts, highest calculated_lap). Sub-millisecond, always.

    The change probe the profile builder polls so a finished lap shows up
    without re-running the ~2 s lap_traces query on a timer. The lap counter
    moving means a lap ENDED, which is exactly when the cache must be dropped.

    TWO STATEMENTS ON PURPOSE — DO NOT TIDY THESE INTO ONE SELECT. SQLite's
    index-max optimisation applies only to a query whose result is a LONE
    aggregate, so each of these is an index seek (idx_telemetry_dev_ts,
    idx_telemetry_lap) and returns in 0.0 ms. Ask for both MAXes in a single
    SELECT and the optimisation is lost and it full-scans: measured 650 ms on a
    130k-row store, against 0.0 ms for the pair.
    """
    ts = conn.execute(
        "SELECT MAX(device_ts) FROM telemetry WHERE device_id = ?",
        (device_id,)).fetchone()[0]
    lap = conn.execute(
        "SELECT MAX(calculated_lap) FROM telemetry WHERE device_id = ?",
        (device_id,)).fetchone()[0]
    return (float(ts) if ts is not None else 0.0,
            int(lap) if lap is not None else -1)


def lap_traces(conn: sqlite3.Connection, device_id: str = DEVICE_ID,
               gap_s: float = TRACE_GAP_S,
               move_kmh: float = STOP_MOVING_KMH,
               stop_min_s: float = STOP_MIN_S,
               quantum_m: float = STOP_DISTANCE_QUANTUM_M):
    """One row per (lap number, run). The session-aware lap_overview.

    Replaces BOTH lap_overview() and fetch_lap_summary() for the builder, which
    is why it costs about what the two of them did together (measured: 1364 ms
    against 676 + 552 on a 118k-row store).

    THE `carried_*` COLUMNS ARE NOT THIS TRACE'S FIGURES. `last_lap_*` describes
    the lap just FINISHED, so the values carried on a trace's own rows belong to
    the lap BEFORE it. They are returned under `carried_` names so nothing can
    read them as this lap's time by accident; profile_build.pair_traces() joins
    each trace to the run that actually follows it and that run's carried values
    are this trace's real time and energy.

    lap_overview() and fetch_lap_summary() are left exactly as they were: the
    pit dashboard and the pit wall read them on hot paths and neither cares
    about drives.

    ALSO RETURNS, per drive: `v_start_kmh` / `v_end_kmh` (the first and last
    speed the car reported, so an out-lap and an in-lap can be told from a
    flying lap) and `stop_s` / `stop_at_m` / `stop_rows` / `stopped_s_total` /
    `n_stops` (the longest standstill INSIDE the drive, and where on the lap it
    was). All NULL when the drive has no stop. See STOP_MOVING_KMH above for
    why a standstill, not a gap, is what marks a driver change — and note the
    pit threshold itself is NOT applied here, so moving that slider does not
    invalidate this query's cached result.

    Measured cost on a 130k-row store: 1.7-3.1 s, against 1.1-1.8 s before the
    extra CTEs. The window sort over (calculated_lap, device_ts) is the expense
    and no index provides that order, which is why the stop and speed work
    shares this one sort instead of living in a second function.
    """
    return conn.execute(
        "WITH seq AS ("
        "  SELECT device_ts, calculated_lap AS lap, lap_distance_m,"
        "         mms_vehicle_speed_kmh AS v, mms_power_W AS p, lap_source,"
        "         last_lap_time_s, last_lap_energy, last_lap_regen_energy,"
        "         last_lap_distance_m,"
        "         LAG(device_ts) OVER (PARTITION BY calculated_lap"
        "                              ORDER BY device_ts) AS prev_ts"
        "  FROM telemetry"
        "  WHERE device_id = ? AND calculated_lap IS NOT NULL"
        "    AND device_ts IS NOT NULL"
        "), runs AS ("
        "  SELECT *, SUM(CASE WHEN prev_ts IS NULL OR device_ts - prev_ts > ?"
        "                     THEN 1 ELSE 0 END)"
        "            OVER (PARTITION BY lap ORDER BY device_ts"
        "                  ROWS UNBOUNDED PRECEDING) AS run"
        "  FROM seq"
        # The original per-trace aggregate, now a CTE so the extras can join to
        # it. Unchanged column for column.
        "), base AS ("
        "  SELECT CAST(lap AS INTEGER) AS trace_lap, run,"
        "         COUNT(*) AS n_samples,"
        "         SUM(v IS NOT NULL) AS n_speed,"
        "         SUM(p IS NOT NULL) AS n_power,"
        "         MAX(lap_distance_m) AS trace_end_m,"
        "         MIN(device_ts) AS t0, MAX(device_ts) AS t1,"
        "         MAX(ABS(v)) AS v_max_kmh,"
        "         MAX(lap_source) AS lap_source,"
        "         MAX(last_lap_time_s) AS carried_lap_time_s,"
        "         MAX(last_lap_energy) AS carried_energy_wh,"
        "         MAX(last_lap_regen_energy) AS carried_regen_wh,"
        "         MAX(last_lap_distance_m) AS carried_distance_m"
        "  FROM runs GROUP BY CAST(lap AS INTEGER), run"
        # First and last speed the car actually REPORTED in this drive: what
        # tells an out-lap from a standing start apart from a flying lap.
        # SQLite has no FIRST_VALUE(... IGNORE NULLS), so the NULLs are filtered
        # out first and the survivors numbered.
        "), spd AS ("
        "  SELECT CAST(lap AS INTEGER) AS trace_lap, run, ABS(v) AS av,"
        "         ROW_NUMBER() OVER (PARTITION BY lap, run"
        "                            ORDER BY device_ts) AS rn,"
        "         COUNT(*)     OVER (PARTITION BY lap, run) AS cnt"
        "  FROM runs WHERE v IS NOT NULL"
        "), ends AS ("
        "  SELECT trace_lap, run,"
        "         MAX(CASE WHEN rn = 1   THEN av END) AS v_start_kmh,"
        "         MAX(CASE WHEN rn = cnt THEN av END) AS v_end_kmh"
        "  FROM spd GROUP BY trace_lap, run"
        # Gaps and islands. `island` is the number of MOVING samples seen so far
        # in this drive, so every maximal run of not-moving rows shares one
        # value. A NULL speed is "not moving" here, which means it CONTINUES an
        # island rather than splitting it -- one dropped sample must not turn a
        # 730 s stop into two 365 s halves that slip under every threshold.
        "), marks AS ("
        "  SELECT CAST(lap AS INTEGER) AS trace_lap, run, device_ts,"
        "         lap_distance_m, v,"
        "         CASE WHEN v IS NOT NULL AND ABS(v) >= ? THEN 1 ELSE 0 END"
        "              AS moving,"
        "         SUM(CASE WHEN v IS NOT NULL AND ABS(v) >= ?"
        "                  THEN 1 ELSE 0 END)"
        "             OVER (PARTITION BY lap, run ORDER BY device_ts"
        "                   ROWS UNBOUNDED PRECEDING) AS island"
        "  FROM runs"
        "), islands AS ("
        "  SELECT trace_lap, run, island,"
        # WALL CLOCK, from the island's own first row to its last. Conservative
        # on purpose: it never counts the unknown time between the last moving
        # sample and the first stopped one, so a reported stop is a LOWER bound
        # and can never be invented.
        "         MAX(device_ts) - MIN(device_ts) AS stop_s,"
        "         MIN(lap_distance_m) AS stop_at_m,"
        "         COALESCE(MAX(lap_distance_m) - MIN(lap_distance_m), 0.0)"
        "             AS drift_m,"
        "         SUM(CASE WHEN v IS NOT NULL AND ABS(v) < ?"
        "                  THEN 1 ELSE 0 END) AS n_still,"
        "         COUNT(*) AS n_rows"
        "  FROM marks WHERE moving = 0"
        "  GROUP BY trace_lap, run, island"
        "), real_stops AS ("
        "  SELECT trace_lap, run, stop_s, stop_at_m, n_rows,"
        "         ROW_NUMBER() OVER (PARTITION BY trace_lap, run"
        "                            ORDER BY stop_s DESC) AS rk,"
        "         SUM(stop_s) OVER (PARTITION BY trace_lap, run)"
        "             AS stopped_s_total,"
        "         COUNT(*)    OVER (PARTITION BY trace_lap, run) AS n_stops"
        "  FROM islands"
        # A RUN OF NULL SPEEDS IS NOT A STANDSTILL. It needs at least one row
        # where the car actually reported a speed under move_kmh. This clause is
        # load-bearing: 74,341 of 125,771 lap-tagged rows here have no speed at
        # all, 40-odd whole traces have none, and the longest such run is
        # 9494 s -- which without this reads as a 2.6-hour pit stop.
        "  WHERE n_still >= 1 AND stop_s >= ?"
        # And the car cannot have covered more ground than move_kmh allows in
        # that time. THIS is what separates a real standstill from a telemetry
        # dropout AT SPEED, whose distance keeps climbing. Physical rather than
        # a flat metre budget: a flat 20 m rule lost a real stop here (trace
        # L0R56 read 306 s against 453 s), and this form self-scales. The last
        # term is one 10 m distance quantum of slack.
        "    AND drift_m <= ? / 3.6 * stop_s + ?"
        ") "
        "SELECT b.*, e.v_start_kmh, e.v_end_kmh, "
        "       s.stop_s, s.stop_at_m, s.n_rows AS stop_rows, "
        "       s.stopped_s_total, s.n_stops "
        "FROM base b "
        "LEFT JOIN ends e ON e.trace_lap = b.trace_lap AND e.run = b.run "
        "LEFT JOIN real_stops s ON s.trace_lap = b.trace_lap "
        "                      AND s.run = b.run AND s.rk = 1 "
        "ORDER BY b.trace_lap, b.run",
        (device_id, float(gap_s), float(move_kmh), float(move_kmh),
         float(move_kmh), float(stop_min_s), float(move_kmh),
         float(quantum_m)),
    ).fetchall()


def fetch_trace_samples(conn: sqlite3.Connection, lap: int, t0: float, t1: float,
                        device_id: str = DEVICE_ID):
    """One TRACE's samples: (device_ts, lap_distance_m, speed, source, power).

    A sibling of fetch_lap_profile_samples bounded by time as well as by lap
    number, so it returns one drive rather than every drive that ever carried
    this lap number. Carries mms_power_W as a fifth column for the energy
    breakdown; clean_samples() reads positions 1 and 2 only, so the extra column
    costs nothing to everything else that consumes these rows.
    """
    return conn.execute(
        "SELECT device_ts, lap_distance_m, mms_vehicle_speed_kmh, lap_source, "
        "       mms_power_W "
        "FROM telemetry "
        "WHERE device_id = ? AND calculated_lap >= ? AND calculated_lap < ? "
        "  AND device_ts BETWEEN ? AND ? "
        "  AND device_ts IS NOT NULL AND lap_distance_m IS NOT NULL "
        "ORDER BY device_ts ASC",
        (device_id, float(int(lap)), float(int(lap)) + 1.0, float(t0), float(t1)),
    ).fetchall()


def lap_started_estimate(conn: sqlite3.Connection, lap,
                         device_id: str = DEVICE_ID):
    """When the car crossed into `lap`, from the samples we happen to hold.

    THE FALLBACK, not the answer. The car reports lap_started_ts (a wall clock
    set at the moment of the cut), and that is what the dashboard shows. This
    covers the two cases where it is absent: rows recorded before the pit
    stored that column, and a car running older code.

    Accurate to one push interval (~0.5 s) at best, and it reads SHORT when the
    pit missed the first samples of the lap -- it can only see the earliest
    sample it has. Index-backed by idx_telemetry_lap, so the cost does not grow
    with the race.

    None when the lap is unknown or nothing is stored for it, which the caller
    shows as a dash rather than counting up from an invented datum.
    """
    if lap is None:
        return None
    try:
        row = conn.execute(
            "SELECT MIN(device_ts) FROM telemetry "
            "WHERE device_id = ? AND CAST(calculated_lap AS INTEGER) = ?",
            (device_id, int(lap)),
        ).fetchone()
    except Exception:                                    # noqa: BLE001
        return None
    return row[0] if row and row[0] else None


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
    # COVERING INDEX FOR THE HISTORY CHARTS. (device_id, device_ts) first so
    # it also answers the ordering and the window bounds, then every column a
    # chart can draw -- see CHART_COLUMNS for why carrying them is worth it.
    # Built here, after the migration, for the same reason the faults index is:
    # an older store may not have all of these columns until the ALTER TABLE
    # above has run. Takes about half a second on a 170 MB store, once.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_telemetry_chart ON telemetry "
        "(device_id, device_ts, %s)" % ", ".join(CHART_COLUMNS)
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
# Kept under the old name. Its importer (the one-off fix_vehicle_speed.py) has
# been removed, but the boundary is the documented dividing line between the two
# speed eras, so it stays as the reference for reading old rows.
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
    health = car.get("health") or {}

    return {
        "rtdb_key": rtdb_key,
        "device_id": device_id,
        "device_ts": _num(record.get("timestamp")),
        # ingested_ts is filled at write time (caller passes time.time())
        "bms_soc_percent": _num(battery.get("bms_soc_percent")),
        "bms_voltage_V": _num(battery.get("bms_voltage_V")),
        "bms_current_A": _num(battery.get("bms_current_A")),
        "bms2_soc_percent": _num(battery.get("bms2_soc_percent")),
        "bms2_voltage_V": _num(battery.get("bms2_voltage_V")),
        "bms2_current_A": _num(battery.get("bms2_current_A")),
        "bms_string_count": _num(battery.get("bms_string_count")),
        # One key per possible cell tap. .get() returns None for anything the
        # car never reported (fewer cells wired than BMS_CELL_COLUMN_COUNT, or
        # a build that predates this column existing) — never a fabricated 0.
        **{f"bms_cell_{i:02d}_V": _num(battery.get(f"bms_cell_{i:02d}_V"))
           for i in range(1, BMS_CELL_COLUMN_COUNT + 1)},
        # Derived here from the record's own cell readings rather than trusted
        # from the car, so a Pi still running an older build (which sent the
        # module's average under this name) is stored the same way.
        "battery_temp_C": limits.battery_temp_from_cells(
            _num(v) for k, v in temp.items()
            if k.startswith("bms_cell_temp_") and k.endswith("_C")),
        **{f"{p}_temp_{i}_C": _num(battery.get(f"{p}_temp_{i}_C"))
           for p in ("bms", "bms2") for i in (1, 2, 3)},
        # DS003. One key per possible thermistor slot; .get() returns None
        # for anything the module hasn't loaded/enabled (or reported yet) —
        # never a fabricated 0 (see BMS_CELL_COLUMN_COUNT's cell-voltage
        # comment above for the same reasoning applied to voltage taps).
        **{f"bms_cell_temp_{i:02d}_C": _num(temp.get(f"bms_cell_temp_{i:02d}_C"))
           for i in range(1, THERMISTOR_CELL_COLUMN_COUNT + 1)},
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
        # Throttle. _num keeps a missing reading NULL rather than 0: the car
        # omits mms_throttle_percent entirely when the pedal voltage is
        # implausible (unplugged sensor), and a stored 0 there would read as a
        # driver who lifted off.
        "mms_throttle_percent": _num(motor.get("mms_throttle_percent")),
        "mms_throttle_mv": _num(motor.get("mms_throttle_mv")),
        "mms_throttle_zone": _join(motor.get("mms_throttle_zone")),
        "total_race_energy": _num(motor.get("total_race_energy")),
        "last_lap_energy": _num(motor.get("last_lap_energy")),
        "last_lap_regen_energy": _num(motor.get("last_lap_regen_energy")),
        "stint_energy": _num(motor.get("stint_energy")),
        "stint_regen_energy": _num(motor.get("stint_regen_energy")),
        "last_lap_time_s": _num(motor.get("last_lap_time_s")),
        "last_lap_distance_m": _num(motor.get("last_lap_distance_m")),
        "lap_distance_m": _num(motor.get("lap_distance_m")),
        "lap_source": _join(motor.get("lap_source")),
        "last_lap_kind": _join(motor.get("last_lap_kind")),
        "last_lap_flags": _join(motor.get("last_lap_flags")),
        "zone": _join(motor.get("zone")),
        "current_lap": _num(motor.get("current_lap")),
        "last_lap_number": _num(motor.get("last_lap_number")),
        "lap_seq": _num(motor.get("lap_seq")),
        "last_lap_stopped_s": _num(motor.get("last_lap_stopped_s")),
        "lap_started_ts": _num(motor.get("lap_started_ts")),
        "track_pos_m": _num(motor.get("track_pos_m")),
        "active_strategy": _join(motor.get("active_strategy")),
        "odometer_m": _num(motor.get("odometer_m")),
        "calculated_lap": _num(motor.get("calculated_lap")),
        "stopwatch_s": _num(motor.get("stopwatch_s")),
        "stopwatch_stopped": _flag(motor.get("stopwatch_stopped")),
        # _flag, so a car that does not send the key at all stores NULL rather
        # than a confident 0 -- the difference between "not charging" and "this
        # build cannot tell you". See the column's comment in STATE_COLUMNS.
        "is_charging": _flag(motor.get("is_charging")),
        "lat": _num(gps.get("lat")),
        "lon": _num(gps.get("lon")),
        "gps_age_s": _num(gps.get("fix_age_s")),
        # Faults
        "bms_has_error": _flag(battery.get("bms_has_error")),
        "bms_error_code": _int(battery.get("bms_error_code")),
        "bms_protections": _join(battery.get("bms_protections")),
        "mms_has_error": _flag(motor.get("mms_has_error")),
        "mms_error_code": _int(motor.get("mms_error_code")),
        "mms_alerts": _join(motor.get("mms_alerts")),
        # The car's own health block. Absent from every build older than the
        # heartbeat, and .get() lands those rows as NULL rather than raising.
        "pi_uptime_s": _num(health.get("pi_uptime_s")),
        "can_state": health.get("can_state"),
        "can_silent_s": _num(health.get("can_silent_s")),
        "can_detail": health.get("can_detail"),
        "can_frames": _int(health.get("can_frames")),
        "gps_fix": _flag(health.get("gps_fix")),
        "gps_detail": health.get("gps_detail"),
        "raw_json": json.dumps(car, separators=(",", ":")),
    }


_COLUMNS = ["rtdb_key", "device_id", "device_ts", "ingested_ts", *EXPORT_COLUMNS, "raw_json"]
_INSERT_SQL = (
    f"INSERT OR IGNORE INTO telemetry ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in _COLUMNS)})"
)

_LAST_KNOWN_SQL = (
    "INSERT INTO last_known (device_id, metric, value_num, value_text, device_ts) "
    "VALUES (:device_id, :metric, :value_num, :value_text, :device_ts) "
    "ON CONFLICT(device_id, metric) DO UPDATE SET "
    "value_num = excluded.value_num, value_text = excluded.value_text, "
    "device_ts = excluded.device_ts "
    "WHERE excluded.device_ts > last_known.device_ts"
)


def _last_known_rows(row: dict):
    """Expand one flattened telemetry row into its non-null (metric, value)
    pairs for the last_known upsert. Skipped entirely if the row has no
    device_ts -- there is nothing to order a carry-forward against."""
    ts = row.get("device_ts")
    if ts is None:
        return []
    device_id = row["device_id"]
    out = []
    for col in EXPORT_COLUMNS:
        val = row.get(col)
        if val is None:
            continue
        is_text = _COL_TYPES[col] == "TEXT"
        out.append({
            "device_id": device_id,
            "metric": col,
            "value_num": None if is_text else val,
            "value_text": val if is_text else None,
            "device_ts": ts,
        })
    return out


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

    # Count the telemetry inserts ALONE. Spanning the last_known upserts too
    # made this return roughly 48x the truth -- a 5,000-sample catch-up page
    # logged "+242791 sample(s)" beside a running total that had gone up by
    # 4,999 -- because every sample also touches dozens of last_known metrics.
    before = conn.total_changes
    conn.executemany(_INSERT_SQL, rows)
    inserted = conn.total_changes - before

    last_known_rows = [lk for row in rows for lk in _last_known_rows(row)]
    if last_known_rows:
        conn.executemany(_LAST_KNOWN_SQL, last_known_rows)

    conn.commit()
    return inserted


def latest_known(conn: sqlite3.Connection, device_id: str = DEVICE_ID) -> dict:
    """{metric: (value, device_ts)} for every metric ever reported non-null by
    this device. At most ~73 rows, a primary-key range scan, so cost is
    independent of how large telemetry itself has grown."""
    rows = conn.execute(
        "SELECT metric, value_num, value_text, device_ts FROM last_known "
        "WHERE device_id = ?",
        (device_id,),
    ).fetchall()
    out = {}
    for r in rows:
        value = r["value_text"] if r["value_text"] is not None else r["value_num"]
        out[r["metric"]] = (value, r["device_ts"])
    return out


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
    race-clock state in app_state is left untouched. Also clears last_known for
    the device, so a fresh race doesn't carry forward values from the last
    one."""
    save_cursor(conn, get_last_key(conn))
    cur = conn.execute("DELETE FROM telemetry WHERE device_id = ?", (device_id,))
    conn.execute("DELETE FROM last_known WHERE device_id = ?", (device_id,))
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


# Laps fit to build energy and strategy figures from. A lap with no kind at all
# comes from a car that predates the tags; whether to trust those is the
# caller's decision, see flying_laps().
FLYING = "flying"


def fetch_laps(conn: sqlite3.Connection, device_id: str = DEVICE_ID,
               since_ts: float = None, until_ts: float = None):
    """One dict per COMPLETED lap, oldest first, as the car measured and tagged it.

        lap, seq, energy_wh, regen_wh, lap_time_s, distance_m, lap_source,
        kind, flags (list), stopped_s, finished_ts

    The car holds a finished lap's figures constant on every row of the
    following lap, so a lap is a run of rows sharing those figures. Rows from
    the gate-based tracker are grouped on `lap_seq`, which the pit cannot set
    and which therefore survives a lap-number correction; the lap's figures are
    part of the key too, so a car whose checkpoint was wiped (lap_seq back to
    1 on another evening) still yields separate laps instead of one lap with
    the MAX() of both - the defect documented above lap_traces().

    A figure that CHANGES mid-lap therefore opens a second group, and the lap
    gets listed twice. That is not hypothetical: reset_trip() on the car used
    to null last_lap_distance_m, and reset_energy() last_lap_energy_wh, while
    the lap they belonged to was already over. Both now leave a finished lap's
    figures alone, and _merge_split_laps() below folds back the splits already
    sitting in stores recorded before that fix.

    Rows from an older car have no lap_seq and fall back to grouping on
    calculated_lap, exactly as fetch_lap_summary() does, with kind = None.

    `finished_ts` is the first row that carried the lap, i.e. when the pit
    first heard it was over. Ordering is by that, never by lap number.

    `since_ts` / `until_ts` bound the ROWS considered, which is how the Excel
    export asks for the laps inside its window. The export sheet and the pit's
    per-lap charts are the same list bounded differently -- they must never be
    two counts of the same race, so there is no second lap builder anywhere.
    """
    where = "device_id = ?"
    args = [device_id]
    if since_ts is not None:
        where += " AND device_ts >= ?"
        args.append(since_ts)
    if until_ts is not None:
        where += " AND device_ts <= ?"
        args.append(until_ts)
    tags = has_lap_tags(conn)
    tagged = [] if not tags else conn.execute(
        # last_lap_number is set once, when the lap is cut, so it is constant
        # across the group and MAX() of it is simply that value -- the point of
        # the aggregate is that a BARE column here is whatever row SQLite
        # happened to stop on.
        #
        # THE FALLBACK IS lap_seq, NOT calculated_lap. A FINISHED LAP IS NEVER
        # LAP 0: _close_lap() increments the count and only then records the
        # number, so the car's own laps run 1, 2, 3... calculated_lap is the
        # count of laps completed SO FAR, and the pit can set it to anything --
        # a store here had 525 rows with lap_seq 11 (an eleventh lap really was
        # cut), no last_lap_number, and calculated_lap reset to 0, so the
        # workbook and the charts both opened on a "lap 0" that never happened.
        # lap_seq counts the same thing last_lap_number does, from 1, and the
        # pit cannot move it. It is also the grouping term below, so selecting
        # it needs no aggregate to be deterministic.
        "SELECT CAST(COALESCE(MAX(last_lap_number), lap_seq) AS INTEGER) AS lap, "
        "       CAST(lap_seq AS INTEGER) AS seq, "
        "       last_lap_energy AS energy_wh, last_lap_regen_energy AS regen_wh, "
        "       last_lap_time_s AS lap_time_s, last_lap_distance_m AS distance_m, "
        "       MAX(lap_source) AS lap_source, MAX(last_lap_kind) AS kind, "
        "       MAX(last_lap_flags) AS flags, MAX(last_lap_stopped_s) AS stopped_s, "
        "       MIN(device_ts) AS finished_ts "
        "FROM telemetry WHERE " + where + " AND lap_seq IS NOT NULL AND lap_seq > 0 "
        "GROUP BY CAST(lap_seq AS INTEGER), last_lap_time_s, last_lap_energy, "
        "         last_lap_distance_m",
        args).fetchall()
    legacy = conn.execute(
        "SELECT CAST(calculated_lap AS INTEGER) AS lap, NULL AS seq, "
        "       MAX(last_lap_energy) AS energy_wh, "
        "       MAX(last_lap_regen_energy) AS regen_wh, "
        "       MAX(last_lap_time_s) AS lap_time_s, "
        "       MAX(last_lap_distance_m) AS distance_m, "
        "       MAX(lap_source) AS lap_source, NULL AS kind, NULL AS flags, "
        "       NULL AS stopped_s, MIN(device_ts) AS finished_ts "
        "FROM telemetry WHERE " + where
        + (" AND lap_seq IS NULL " if tags else " ") +
        "  AND calculated_lap IS NOT NULL "
        "  AND (last_lap_energy IS NOT NULL OR last_lap_time_s IS NOT NULL) "
        "GROUP BY CAST(calculated_lap AS INTEGER)",
        args).fetchall()
    laps = []
    for r in list(legacy) + list(tagged):
        lap = dict(r)
        lap["flags"] = [f for f in (lap["flags"] or "").split(",") if f]
        if lap["lap_source"] == "none":         # an old car's sentinel, not a source
            lap["lap_source"] = None
        laps.append(lap)
    laps.sort(key=lambda lap: lap["finished_ts"])
    return _merge_split_laps(laps)


# What a split has to agree on before two fragments can be the same lap, and
# below it what the first fragment may take from a later one when it never
# carried it at all.
_LAP_FIGURES = ("lap_time_s", "energy_wh", "regen_wh", "distance_m")
_LAP_TAGS = ("lap", "lap_source", "kind", "stopped_s")


def _merge_split_laps(laps):
    """Fold fragments of ONE lap back together. `laps` must be oldest first.

    Two entries are the same lap when they share a lap_seq and no figure
    contradicts the other -- every figure equal, or missing on one side. A
    fragment is what a mid-lap change to last_lap_* leaves behind (see
    fetch_laps), and it must not be counted as a lap of its own: it carries the
    same number and the same time, so the History table showed that lap twice,
    the workbook listed it twice, and the copy went through the average as if
    the car had driven it.

    Laps that genuinely share a lap_seq -- a checkpoint wiped between sessions,
    so the count started again at 1 -- disagree on their figures and are kept
    apart. That is what the figures are in the group key for.

    The earliest fragment wins: it holds finished_ts, the moment the pit first
    heard the lap was over. The others only fill in what it is missing.
    """
    out = []
    latest = {}                      # lap_seq -> the entry still open to merges
    for lap in laps:
        seq = lap.get("seq")
        into = latest.get(seq) if seq is not None else None
        if into is not None and all(
                into[f] is None or lap[f] is None or into[f] == lap[f]
                for f in _LAP_FIGURES):
            for f in _LAP_FIGURES + _LAP_TAGS:
                if into[f] is None:
                    into[f] = lap[f]
            if not into["flags"]:
                into["flags"] = lap["flags"]
            continue
        out.append(lap)
        if seq is not None:
            latest[seq] = lap
    return out


def flying_laps(laps):
    """The laps fit for energy and strategy figures.

    Tagged laps: only kind == "flying". If NOTHING is tagged the whole list is
    from an older car, and refusing all of it would blank every chart the pit
    had yesterday - so untagged laps are returned as they always were. Once
    one tagged lap exists, untagged ones are dropped: they cannot be told from
    in-laps, and the tagged ones can.
    """
    if any(lap["kind"] for lap in laps):
        return [lap for lap in laps if lap["kind"] == FLYING]
    return list(laps)


def fetch_lap_track(conn: sqlite3.Connection, lap: int, device_id: str = DEVICE_ID):
    """(device_ts, lap_distance_m) for one lap, ascending — for sector timing.

    Only the two columns sector splits need, so a lap's worth of samples is a
    cheap read even at 1 Hz over a long race.

    MATCHED AS A HALF-OPEN RANGE, NOT WITH A CAST. This used to say
    `CAST(calculated_lap AS INTEGER) = ?`, and wrapping the column in a function
    makes the term unusable as an index constraint: SQLite fell back to
    idx_telemetry_dev_ts with only `device_id = ?` to go on and walked every row
    the car has ever sent, evaluating the CAST on each one, to return the two
    hundred belonging to one lap.

    It was not a small difference. read_sector_times calls this TWICE, behind a
    4-second cache, from the Driver Telemetry tab — the tab the dashboard opens
    on. Measured against a 96 MB store: 141 ms per call, so 282 ms of the pit
    wall's single script-run thread every four seconds, for the whole race,
    growing with the database. As a range on the raw column it plans as
    SEARCH telemetry USING INDEX idx_telemetry_lap and takes 0.08 ms.

    The range is [lap, lap+1) rather than = lap because calculated_lap is stored
    REAL (everything numeric goes through _num), so an equality test against an
    int would depend on float representation. The half-open interval selects
    exactly the same rows without caring.
    """
    return conn.execute(
        "SELECT device_ts, lap_distance_m FROM telemetry "
        "WHERE device_id = ? AND calculated_lap >= ? AND calculated_lap < ? "
        "  AND device_ts IS NOT NULL AND lap_distance_m IS NOT NULL "
        "ORDER BY device_ts ASC",
        (device_id, float(int(lap)), float(int(lap)) + 1.0),
    ).fetchall()


def lap_start_energy(conn: sqlite3.Connection, lap: int,
                     device_id: str = DEVICE_ID):
    """The energy baseline of one lap: (total_race_energy, lap_distance_m) of
    its EARLIEST sample, or None when no sample of it carries energy.

    "Energy used so far this lap" is the one per-lap figure the car does not
    publish. lap_tracker.snapshot() sends lap_distance_m -- metres since the
    trigger -- but has no energy counterpart, so the pit subtracts this
    baseline from the live total_race_energy instead. NET of regen, like every
    energy column here, which is exactly what makes the result comparable with
    the last_lap_energy tile beside it.

    lap_distance_m comes back with it so the caller can tell whether this
    baseline really is the start of the lap. If the link was down when the lap
    began, the earliest sample the pit HAS may be hundreds of metres in, and
    the subtraction then understates the lap by whatever was missed. Saying so
    is the point of returning it — see the tile note in the dashboard.

    MATCHED AS A HALF-OPEN RANGE, not with a CAST, for the reason spelled out
    in fetch_lap_track's docstring. `ORDER BY device_ts ASC LIMIT 1` keeps the
    plan on idx_telemetry_lap (verified with EXPLAIN QUERY PLAN) and sorts only
    the one lap's rows rather than walking the table in device_ts order.

    COST SCALES WITH THE LAP, so the caller must not run this every frame. A
    normal 210 s lap is ~400 rows and 0.2 ms. A lap whose counter STALLED is
    not: the same _runs() failure the sector code guards against leaves hours
    of driving under one lap tag, and this measured 200 ms over a 49k-row lap
    in a bench store. build_live() runs every 2 s for every viewer, so api.py
    caches the result per lap.
    """
    return conn.execute(
        "SELECT total_race_energy, lap_distance_m FROM telemetry "
        "WHERE device_id = ? AND calculated_lap >= ? AND calculated_lap < ? "
        "  AND total_race_energy IS NOT NULL "
        "ORDER BY device_ts ASC LIMIT 1",
        (device_id, float(int(lap)), float(int(lap)) + 1.0),
    ).fetchone()


# How far back down the index recent_laps is willing to look. Four laps at the
# car's 0.5 s push is ~3,400 samples, so this covers them with room to spare --
# and it is also a horizon: 8,192 samples is about 68 minutes of a car that is
# reporting, so a lap whose last sample is further back than that is not
# returned. That is the intended answer rather than a limitation. A previous
# lap an hour and a half ago means the car has been standing still since, and
# its sector times are not what the grid is being read for.
RECENT_LAPS_SCAN = 8192


def recent_laps(conn: sqlite3.Connection, count: int = 2,
                device_id: str = DEVICE_ID, since_ts: float = None):
    """The `count` lap tags with the newest SAMPLES, newest first.

    ORDERED BY TIME, NOT BY LAP NUMBER, and the difference is not academic:
    calculated_lap is not unique. It restarts whenever the car's counter does
    -- the green-flag reset, a pit set_lap, a checkpoint wiped between sessions
    -- which is the defect documented above lap_traces().

    Ordering by number picked the HIGHEST tag in the store and called it the
    lap being driven. After a race start zeroes the car that is a WARM-UP lap:
    the sector grid sat on two laps from before the green flag, comparing one
    to its neighbour from the warm-up, and never moved again however far the
    race got. Ordering by the newest sample answers the question that was
    actually being asked -- which lap is the car on now.

    `since_ts` bounds it to the race, which is how the sector grid asks, so a
    lap driven before the flag cannot be shown as the current one. It is the
    same rule _fold_bests already applies to the purple cell: a lap that
    predates the race is not part of it.

    WALKS BACK FROM THE NEWEST SAMPLE, and does NOT group. The obvious
    spelling -- GROUP BY lap ORDER BY MAX(device_ts) DESC -- gives the right
    answer and costs what a full scan costs, because SQLite's index-max
    optimisation applies only to a LONE aggregate (see store_watermark, which
    is split in two for the same reason). Walking the tail of
    idx_telemetry_dev_ts and taking the tags in the order they appear is the
    same answer, read lazily and stopped at the last tag wanted.

    Measured over four laps, this endpoint being the most polled in the app:

        demo_telemetry.db   11,812 rows, laps of ~420 samples
                            0.9 ms walked | 3.1 ms by number | 38 ms grouped

    The walk is fastest where the store looks like a race, because it stops as
    soon as it has four tags. Where one tag covers tens of thousands of samples
    -- an August test store with 5k rows tagged lap 0 -- it walks the full
    window at 165 ms, which a 0.5 s push over four real laps never reaches.

    Fewer than `count` tags come back when the window holds no more -- see
    RECENT_LAPS_SCAN for the horizon and why stopping there is the answer and
    not a shortfall.
    """
    where = "device_id = ? AND calculated_lap IS NOT NULL"
    args = [device_id]
    if since_ts is not None:
        where += " AND device_ts >= ?"
        args.append(float(since_ts))
    sql = ("SELECT CAST(calculated_lap AS INTEGER) AS lap FROM telemetry "
           "WHERE " + where + " ORDER BY device_ts DESC LIMIT ?")

    # ONE PASS, walked rather than fetchall()'d: the tags are wanted in the
    # order they appear and the walk stops at the last one, so a car that is
    # lapping costs the rows of `count` laps and no more.
    want = int(count)
    seen = []
    for row in conn.execute(sql, args + [max(RECENT_LAPS_SCAN, want * 1024)]):
        if row[0] not in seen:
            seen.append(row[0])
            if len(seen) >= want:
                break
    return seen


def latest_sample(conn: sqlite3.Connection, device_id: str = DEVICE_ID):
    """Most recent sample by car timestamp — the dashboard's 'live' value."""
    return conn.execute(
        "SELECT * FROM telemetry WHERE device_id = ? "
        "ORDER BY device_ts DESC LIMIT 1",
        (device_id,),
    ).fetchone()


# --------------------------------------------------------------------------- #
# Rule 3.5.6 — cell extremes over the last 2 hours
# --------------------------------------------------------------------------- #
def _extremes_sql():
    """The one aggregate that finds every gated cell column's MAX and MIN over a
    time range. Built once: the column lists never change at runtime.

    The gates are the car's (cell_extremes.py), written as SQL so no row has
    to leave SQLite: a voltage counts only inside the plausible range and only
    for a tap the BMS says is wired; a temperature counts only above the
    failed-thermistor floor. Without them an unwired tap's 0.000 V would be the
    "lowest cell voltage" of every window.
    """
    lo, hi = cell_extremes.CELL_V_PLAUSIBLE_MIN, cell_extremes.CELL_V_PLAUSIBLE_MAX
    floor = limits.CELL_TEMP_IMPLAUSIBLE_BELOW
    parts, cols = [], []
    for i in range(1, BMS_CELL_COLUMN_COUNT + 1):
        c = f"bms_cell_{i:02d}_V"
        g = (f"CASE WHEN {c} BETWEEN {lo} AND {hi} AND (bms_string_count IS NULL "
             f"OR {i} <= bms_string_count) THEN {c} END")
        parts += [f"MAX({g})", f"MIN({g})"]
        cols.append(("volt", i, c, g))
    for i in range(1, THERMISTOR_CELL_COLUMN_COUNT + 1):
        c = f"bms_cell_temp_{i:02d}_C"
        g = f"CASE WHEN {c} >= {floor} THEN {c} END"
        parts += [f"MAX({g})", f"MIN({g})"]
        cols.append(("temp", i, c, g))
    any_cell = " OR ".join(f"({g}) IS NOT NULL" for _k, _i, _c, g in cols)
    parts.append(f"MIN(CASE WHEN {any_cell} THEN device_ts END)")
    sql = ("SELECT " + ", ".join(parts) + " FROM telemetry "
           "WHERE device_id = ? AND device_ts > ? AND device_ts <= ?")
    return sql, cols


_EXTREMES_SQL = None


def cell_extremes_report(conn: sqlite3.Connection,
                         window_s: float = None, device_id: str = DEVICE_ID,
                         end_ts: float = None):
    """Rule 3.5.6 for the pit: the same dict cell_extremes.RollingExtremes
    .result() gives the car, plus `end_ts`.

    The window ends at the NEWEST STORED SAMPLE (the car's clock), not at the
    pit laptop's now: if the car has been quiet for ten minutes the report is
    still about the 2 hours the car actually reported, and end_ts says so.

    Cost: one indexed range aggregate over ~2 h of rows, then one indexed
    lookup per extreme for its time. Ties go to the EARLIEST reading, as on the
    car, so the time shown is when the value first occurred in the window.
    """
    global _EXTREMES_SQL
    if _EXTREMES_SQL is None:
        _EXTREMES_SQL = _extremes_sql()
    sql, cols = _EXTREMES_SQL
    window_s = float(window_s or cell_extremes.WINDOW_S)
    empty = {k: None for k in cell_extremes.KEYS}
    empty.update(covers_s=0.0, window_s=window_s, end_ts=None)

    if end_ts is None:          # given explicitly only by tests and replays
        row = conn.execute("SELECT MAX(device_ts) FROM telemetry WHERE device_id = ?",
                           (device_id,)).fetchone()
        end_ts = row[0] if row else None
    if end_ts is None:
        return empty
    start_ts = end_ts - window_s
    agg = conn.execute(sql, (device_id, start_ts, end_ts)).fetchone()
    if agg is None:
        return empty

    # Per extreme: the value, then EVERY cell that reached it (a tie across
    # cells is common at 1 °C / 1 mV resolution).
    best = {}
    for n, (kind, cell, col, gate) in enumerate(cols):
        for name, v in ((f"{kind}_max", agg[2 * n]), (f"{kind}_min", agg[2 * n + 1])):
            if v is None:
                continue
            cur = best.get(name)
            if cur is None or (v > cur[0] if name.endswith("_max") else v < cur[0]):
                best[name] = (v, [(cell, gate)])
            elif v == cur[0]:
                cur[1].append((cell, gate))

    out = dict(empty, end_ts=end_ts)
    for name, (v, tied) in best.items():
        # The EARLIEST row in the window where any tied cell held the value,
        # and within that row the lowest cell number -- the car's rule too
        # (cell_extremes keeps the first reading and offers cells in order).
        # Only the tied columns are tested, so this stays a short scan.
        where = " OR ".join(f"({g}) = ?" for _c, g in tied)
        hit = conn.execute(
            "SELECT device_ts, " + ", ".join(f"({g})" for _c, g in tied) +
            " FROM telemetry WHERE device_id = ? AND device_ts > ? AND device_ts <= ? "
            f"AND ({where}) ORDER BY device_ts ASC LIMIT 1",
            (device_id, start_ts, end_ts, *([v] * len(tied)))).fetchone()
        if hit is None:
            out[name] = (float(v), int(tied[0][0]), None)
            continue
        cell = next((c for k, (c, _g) in enumerate(tied) if hit[1 + k] == v), tied[0][0])
        out[name] = (float(v), int(cell), float(hit[0]))
    first = agg[-1]
    if first is not None:
        out["covers_s"] = min(window_s, max(0.0, end_ts - first))
    return out


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
    """(min_device_ts, max_device_ts) over stored samples, or (None, None).

    TWO STATEMENTS, NOT ONE. SQLite's index optimisation for MIN/MAX -- walk to
    one end of the index and stop -- applies only to a LONE aggregate. Asking
    for both in one SELECT gives up on it and scans the table, which on this
    store means dragging 170 MB of raw_json through the page cache to read two
    timestamps: 15.9 ms against 0.2 ms for the pair split apart. Same trap as
    store_watermark and recent_laps, both of which are split for this reason.

    Every history request calls this, so it is 15 ms on the front of each one.
    """
    lo = conn.execute("SELECT MIN(device_ts) AS v FROM telemetry "
                      "WHERE device_id = ?", (device_id,)).fetchone()
    hi = conn.execute("SELECT MAX(device_ts) AS v FROM telemetry "
                      "WHERE device_id = ?", (device_id,)).fetchone()
    return (lo["v"] if lo else None), (hi["v"] if hi else None)


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


# Names a caller is allowed to ask fetch_series for. The list is interpolated
# into SQL rather than bound as parameters (column names cannot be bound), so it
# is checked against the real schema first. Every caller passes a module
# constant, so in practice this catches a typo rather than an attack -- but it
# means the f-string below is obviously safe to whoever reads it next.
_COLUMN_SET = frozenset(_COLUMNS)


def count_range(conn: sqlite3.Connection, start_ts: float = None,
                end_ts: float = None, device_id: str = DEVICE_ID) -> int:
    """How many samples fall in [start_ts, end_ts].

    Index-only against idx_telemetry_dev_ts, so it stays milliseconds even when
    the range is the whole race. fetch_series needs it to work out a stride, and
    the History caption needs it to say honestly how many samples a thinned
    chart was built from.
    """
    clauses = ["device_id = ?"]
    params = [device_id]
    if start_ts is not None:
        clauses.append("device_ts >= ?")
        params.append(start_ts)
    if end_ts is not None:
        clauses.append("device_ts <= ?")
        params.append(end_ts)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM telemetry WHERE {' AND '.join(clauses)}",
        params).fetchone()
    return int(row["n"]) if row else 0


def fetch_series(conn: sqlite3.Connection, columns, start_ts: float = None,
                 end_ts: float = None, limit: int = None,
                 stride_target: int = None, device_id: str = DEVICE_ID):
    """Rows for CHARTING: only the columns asked for, optionally thinned in SQL.

    Returns (rows, total, step) -- the rows ascending by car time, how many
    samples the range actually holds, and the stride that was applied (1 when
    every row in range was returned).

    WHY THIS EXISTS ALONGSIDE fetch_samples
    fetch_samples is `SELECT *`, and it has to stay that way: the CSV/Excel
    export resolves its column list at runtime and genuinely wants all 118 of
    them. The chart wants 16. The other 102 include raw_json, which is ~1.6 kB
    per row and 61% of the database file -- read off disk, boxed into Python and
    thrown away, once per row, on a path that runs every 10 seconds. Measured on
    the real store: 100k rows took 25.7 s as `SELECT *` and 1.6 s as the sixteen
    columns the chart reads.

    WHY THE STRIDE IS IN SQL
    The chart draws at most OVERLAY_MAX_POINTS (900) points and thins to that in
    pandas -- after transferring every row. Doing it here means a 24-hour window
    never materialises 100k rows to draw 900 of them.

    It is a STRIDE, not a time-bucket average, and that is deliberate: every
    point drawn stays a value the car actually measured at a moment it actually
    measured it. Bucketing would need a different aggregate per metric (max for
    temperatures, min for voltage sag, mean for speed) and would put numbers on
    screen that were never sampled -- in a dashboard whose whole convention is
    that a missing reading shows as an em dash rather than a plausible zero,
    that is the wrong trade.

    `(rn - 1) % step = 0` counts from the NEWEST row, so rn = 1 always
    survives: the live end of every trace is exact no matter the stride.
    """
    bad = [c for c in columns if c not in _COLUMN_SET]
    if bad:
        raise ValueError(f"fetch_series: unknown column(s) {bad}")
    cols = ", ".join(columns)

    clauses = ["device_id = ?"]
    params = [device_id]
    if start_ts is not None:
        clauses.append("device_ts >= ?")
        params.append(start_ts)
    if end_ts is not None:
        clauses.append("device_ts <= ?")
        params.append(end_ts)
    where = " AND ".join(clauses)

    total = count_range(conn, start_ts, end_ts, device_id)
    # `limit` keeps its fetch_samples meaning: the newest N rows in range.
    considered = min(total, limit) if limit is not None else total

    step = 1
    if stride_target and considered > stride_target:
        step = -(-considered // stride_target)      # ceil, no float rounding

    if step > 1:
        inner_limit = limit if limit is not None else considered
        sql = (f"SELECT {cols} FROM ("
               f"SELECT {cols}, ROW_NUMBER() OVER (ORDER BY device_ts DESC) "
               f"AS _rn FROM telemetry WHERE {where} "
               f"ORDER BY device_ts DESC LIMIT ?) "
               f"WHERE (_rn - 1) % ? = 0 ORDER BY device_ts ASC")
        rows = conn.execute(sql, [*params, inner_limit, step]).fetchall()
    elif limit is not None:
        sql = (f"SELECT {cols} FROM (SELECT {cols} FROM telemetry WHERE {where} "
               f"ORDER BY device_ts DESC LIMIT ?) ORDER BY device_ts ASC")
        rows = conn.execute(sql, [*params, limit]).fetchall()
    else:
        sql = f"SELECT {cols} FROM telemetry WHERE {where} ORDER BY device_ts ASC"
        rows = conn.execute(sql, params).fetchall()

    return rows, total, step


def series_stats(conn: sqlite3.Connection, columns, start_ts: float = None,
                 end_ts: float = None, device_id: str = DEVICE_ID):
    """min/avg/max/count and the newest reading, per column, worked out in SQL.

    Returns ({column: {min, avg, max, samples, now}}, rows_in_window).

    WHY NOT IN PYTHON, WHICH IS WHERE IT WAS. The History tab's stat strip
    polls every 10 seconds, and on the "All" window it was pulling every row
    of the store -- all 118 columns, raw_json included -- to take three numbers
    off fifteen of them. Thirty seconds of work, on repeat, for a strip of text
    that fits on one line. In SQL it is one pass and it never leaves the index.

    EXACT, NOT SAMPLED, and that is the point of doing it separately from the
    chart: the chart is thinned to a few thousand points and its own extremes
    would miss the peak. These are every sample in the window.

    `samples` counts the readings that EXIST -- SQL aggregates skip NULL, which
    is the same rule the rest of the pit follows: a metric the car never sent is
    absent, not zero, and must not be averaged as one. Subtract it from the
    rows figure for how many samples were missing that metric.

    `now` is the newest non-null reading in the window, not the newest row's
    value, so a metric that dropped out for the last few seconds still reports
    what it last actually said.
    """
    bad = [c for c in columns if c not in _COLUMN_SET]
    if bad:
        raise ValueError(f"series_stats: unknown column(s) {bad}")

    clauses = ["device_id = ?"]
    params = [device_id]
    if start_ts is not None:
        clauses.append("device_ts >= ?")
        params.append(start_ts)
    if end_ts is not None:
        clauses.append("device_ts <= ?")
        params.append(end_ts)
    where = " AND ".join(clauses)

    select = ["COUNT(*)"]
    args = []
    for c in columns:
        select += [f"MIN({c})", f"AVG({c})", f"MAX({c})", f"COUNT({c})",
                   # The scalar subqueries sit in the SELECT list, so their
                   # parameters bind BEFORE the outer WHERE's -- hence this
                   # order. Each walks the index back from the newest row and
                   # stops at the first reading, so it costs nothing on a
                   # metric the car is sending.
                   f"(SELECT {c} FROM telemetry WHERE {where} "
                   f"AND {c} IS NOT NULL ORDER BY device_ts DESC LIMIT 1)"]
        args += params
    row = conn.execute("SELECT %s FROM telemetry WHERE %s"
                       % (", ".join(select), where), [*args, *params]).fetchone()

    out = {}
    for i, c in enumerate(columns):
        lo, avg, hi, n, now = row[1 + i * 5:6 + i * 5]
        out[c] = {"min": lo, "avg": avg, "max": hi, "samples": int(n or 0),
                  "now": now}
    return out, int(row[0] or 0)


def fetch_faults(conn: sqlite3.Connection, limit: int = 2000,
                 device_id: str = DEVICE_ID, columns=None):
    """Rows with a BMS or MMS fault flag set, ascending by car time.
    With `limit`, returns the most recent `limit` fault rows (still ascending).
    Backed by the partial fault index, so it stays cheap on a large table."""
    where = "device_id = ? AND (bms_has_error = 1 OR mms_has_error = 1)"
    # Defaults to SELECT * so the CSV export keeps every column; the dashboard's
    # fault timeline passes the seven it actually reads.
    if columns is None:
        cols = "*"
    else:
        bad = [c for c in columns if c not in _COLUMN_SET]
        if bad:
            raise ValueError(f"fetch_faults: unknown column(s) {bad}")
        cols = ", ".join(columns)
    if limit is not None:
        sql = (f"SELECT {cols} FROM (SELECT {cols} FROM telemetry WHERE {where} "
               f"ORDER BY device_ts DESC LIMIT ?) ORDER BY device_ts ASC")
        return conn.execute(sql, (device_id, limit)).fetchall()
    sql = f"SELECT {cols} FROM telemetry WHERE {where} ORDER BY device_ts ASC"
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


# --------------------------------------------------------------------------- #
# Who was driving — stints, and the lap each one covers
# --------------------------------------------------------------------------- #
# The pit logs a driver change on the wall (Pit_Web's "Driver changed" button),
# which is the ONLY record anywhere of who was in the car. The car reports no
# driver: nothing on the CAN bus knows one, so a lap's driver can never be
# recovered from telemetry.db alone — it has to come from what the pit logged.
#
# Read here, not in Pit_Web, because BOTH readers need it: /api/laps for the
# per-lap table and export.py for the workbook's Driver column. Two readers of
# one record is the same rule fetch_laps() is under — the charts and the
# spreadsheet must never be two different accounts of the same race.
#
# The record lives in app_state under 'driver_stint', written by Pit_Web/api.py
# (same place the race clock lives, read by load_race_state above). It holds
# the CURRENT stint at the top level plus `log`, the stints already finished.
# A stint that was never named has driver None — an unnamed stint is not an
# error and must not read as one, so it stays None and shows as "—".
def load_driver_stints(conn: sqlite3.Connection) -> list:
    """Every stint this race, oldest first, as the pit logged it.

        [{"stint": 1, "driver": "Noa"|None,
          "started_at": 1.7e9, "ended_at": 1.7e9|None}, ...]

    `ended_at` is None on the last one — that driver is still in the car.
    Empty when no stint has ever been logged (no race started yet, or a car
    running without the pit wall). Never raises on a malformed record: a
    missing driver list must not take the lap table down with it.
    """
    row = conn.execute(
        "SELECT value FROM app_state WHERE key = 'driver_stint'").fetchone()
    if not row or not row["value"]:
        return []
    try:
        st = json.loads(row["value"]) or {}
    except (ValueError, TypeError):
        return []
    if not isinstance(st, dict):
        return []
    out = []
    for e in (st.get("log") or []):
        if isinstance(e, dict) and e.get("started_at") is not None:
            out.append({"stint": e.get("stint"), "driver": e.get("driver") or None,
                        "started_at": float(e["started_at"]),
                        "ended_at": (None if e.get("ended_at") is None
                                     else float(e["ended_at"]))})
    # The stint in progress is not in the log — it is the record itself, so a
    # rename ("Name current driver") lands on it with nothing to keep in sync.
    if st.get("started_at") is not None:
        out.append({"stint": st.get("stint"), "driver": st.get("driver") or None,
                    "started_at": float(st["started_at"]), "ended_at": None})
    out.sort(key=lambda s: s["started_at"])
    return out


def driver_at(stints: list, ts) -> str:
    """The driver in the car at `ts`, or None if nobody was logged then.

    A lap is credited to whoever was driving when it FINISHED, which is what
    `fetch_laps` gives as finished_ts. A driver change is logged during the pit
    stop, after the in-lap is already over: the in-lap therefore falls before
    the change and stays with the driver who drove it, and the out-lap falls
    after and goes to the new one. That is the intended reading.

    Laps completed before any stint was logged get None, not the first driver —
    inventing an attribution for a lap nobody was logged for would put a name
    against a lap that name may not have driven.
    """
    if ts is None:
        return None
    for s in stints:
        if s["started_at"] <= ts and (s["ended_at"] is None or ts < s["ended_at"]):
            return s["driver"]
    return None


def attach_lap_drivers(laps: list, stints: list) -> list:
    """Add a `driver` key to each lap from `fetch_laps`. Mutates and returns."""
    for lap in laps:
        lap["driver"] = driver_at(stints, lap.get("finished_ts"))
    return laps
