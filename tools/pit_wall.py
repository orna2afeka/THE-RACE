"""
pit_wall.py — the big-screen circuit view for the pit
======================================================
Serves ONE page and ONE JSON endpoint on the pit LAN, so a TV in the garage can
show the circuit and the numbers that matter without anybody touching it.

    python tools/pit_wall.py                 # http://<pit-ip>:8503/
    python tools/pit_wall.py --port 9000
    python tools/pit_wall.py --once          # print one snapshot and exit

WHY THIS IS NOT THE SPECTATOR PAGE
docs/index.html is published to GitHub Pages for the families at home. That page
is deliberately starved: a short whitelist, because anyone with the URL can
read it. It is no longer true that it shows nothing a rival could use -- the
team chose to publish live position, and on race morning the charging flag as
well -- but everything NOT on that whitelist still stays off it. A pit wall
wants the opposite — pack voltage, temperatures, the gap to the strategy —
and none of that may be published. So this is a separate page on a separate server that
never leaves the LAN.

WHY IT IS NOT PART OF THE PIT DASHBOARD
It was written when the pit dashboard was a Streamlit app, which ran one
ScriptRunner thread per session shared by every widget on every tab. Hanging a
second always-on screen off that thread is how the dashboard's own refresh got
slow in the first place. The separation still pays with the React dashboard:
this process has its own thread, its own read-only connection, and cannot make
the dashboard wait.

WHY ONE THREAD OWNS THE DATABASE
Every read happens on the Feed thread and nowhere else. Request handlers copy a
dict. That is not just tidiness: sqlite3 connections refuse use from a thread
other than the one that opened them, and ThreadingHTTPServer hands each request
to a different thread — an earlier version shared one handle behind a lock and
every poll after the first came back
"SQLite objects created in a thread can only be used in that same thread".
Serving from a snapshot removes the question entirely, and it also means a slow
read can never make a screen wait.

WHY IT IS READ-ONLY, TWICE OVER
The connection is opened mode=ro, falling back to query_only=ON (db.get_conn_ro),
so a bug here cannot corrupt the race's telemetry. The ingest writer and the pit
dashboard keep working normally while this serves: SQLite's WAL allows readers
and one writer at the same time.
"""

import argparse
import csv
import json
import math
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for _p in (_REPO, os.path.join(_REPO, "Pit_Dashboard")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import constants as C  # noqa: E402
import db  # noqa: E402

PAGE_PATH = os.path.join(_REPO, "Pit_Dashboard", "wall.html")
DEFAULT_PORT = 8503          # 8000 dashboard, 8502 builder, 8504 energy matrix

# How often the car's current state is re-read. The page polls at 1 Hz; this is
# a little quicker so a poll rarely waits a whole cycle for fresh numbers.
REFRESH_S = 0.5

# How often the completed-lap table is rebuilt.
#
# It is the one expensive read here: a GROUP BY over every row in telemetry,
# measured at 591 ms against a 118k-row database, against 0.3 ms for everything
# else put together. Narrowing it to "the last few laps" does not help and in
# fact measured WORSE (1049 ms): idx_telemetry_lap covers (device_id,
# calculated_lap) but not the lap_time/energy columns, so the range scan still
# visits the table for every row it touches.
#
# So it is not made cheaper, it is made rare. A lap table twenty seconds old has
# no consequence, because a lap takes ten times that.
LAP_TABLE_EVERY_S = 20.0

# --------------------------------------------------------------------------- #
# Energy used so far this lap
# --------------------------------------------------------------------------- #
# The car publishes last_lap_energy and total_race_energy, never "this lap so
# far", so the pit subtracts: live total minus the total at the lap's first
# sample. Net of regen on both sides, which is what makes it comparable with
# the Last figure beside it. Same rule as the dashboard's Current-lap tile
# (api.py), deliberately, so the TV and the dashboard cannot disagree about
# what the lap has cost.
#
# CACHED PER LAP. db.lap_start_energy() costs what the lap is big -- 0.2 ms for
# a normal lap, 200 ms for one whose counter stalled and swallowed hours of
# samples -- and the feed re-reads twice a second. The TTL is also what makes a
# late baseline self-correct: if the link was down at the lap trigger the
# earliest sample held is further in, and the collector backfills the missing
# ones minutes later.
LAP_ENERGY_TTL_S = 15.0

# How far into a lap the baseline may sit before the figure is meaningfully
# short. A sample lands every ~0.5 s, so a healthy baseline is a few metres in;
# 100 m means the start of the lap was never received. The page dashes the
# value rather than drawing a low number that is only low because of a dropout.
LAP_ENERGY_BASELINE_MAX_M = 100.0

# No lap can end this far BELOW where it started. Read off the strategy matrix
# so it stays right if the profiles are rebuilt: net regen over a lap is a
# fraction of the spend, never a multiple of it. Only an energy reset -- which
# zeroes total_race_energy mid-lap while the stored first sample still holds
# the pre-reset total -- can clear this bar.
LAP_ENERGY_IMPLAUSIBLE_WH = max(
    [m.get("energy_wh") or 0.0 for m in C.PROFILE_MATRIX.values()] or [100.0])

# Everything the page shows. Keeping the list here rather than in the page means
# a metric the car stops reporting arrives as null and renders as a dash,
# instead of a stale number looking current.
FIELDS = (
    "calculated_lap", "lap_distance_m", "odometer_m", "lap_source",
    "mms_vehicle_speed_kmh", "target_speed_kmh", "mms_power_W", "mms_rpm",
    # The MOTOR's own PT1000, and the CONTROLLER's internal sensor. Two
    # different parts and two different failure modes: the wall's temperature
    # tile said "Motor temp" while reading mms_temperature_C for its whole life,
    # so it was showing the controller and nobody could see the motor at all.
    "mms_motor_temp_C", "mms_temperature_C", "mms_measured_voltage_V",
    "bms_soc_percent", "bms_voltage_V", "bms_current_A", "battery_temp_C",
    "last_lap_time_s", "last_lap_energy", "last_lap_distance_m",
    "total_race_energy", "regen_energy", "stint_energy", "stint_regen_energy",
    # The GPS columns are "lat"/"lon" -- NOT gps_lat/gps_lon, which is what this
    # list said first. Nothing complained, and worse, a check of how well they
    # were populated came back "2000 of 2000": SQLite treats a double-quoted
    # name matching no column as a string literal, so COUNT("gps_lat") counted
    # rows and made a column that does not exist look perfect.
    # check_fields() exists so the next one of these is caught at startup.
    # gps_age_s travels WITH lat/lon and is not optional: the car keeps serving
    # its last known fix after the receiver loses lock, so a position can be
    # well-formed and half an hour old while the car is a kilometre away.
    # 1/0/NULL: a charger on the car, inferred by charge_detector.py from a
    # stationary car plus sustained current into the pack. NULL on rows from a
    # build older than 2026-09-19, which the page renders as no badge at all --
    # the same as "not charging", and right for a wall: the badge is there to
    # explain a stopped car, not to assert anything when it is moving.
    "is_charging",
    "active_strategy", "lat", "lon", "gps_age_s",
    "bms_has_error", "bms_error_code", "mms_has_error", "mms_error_code",
)


def check_fields(db_path=None):
    """[(field, why)] for anything in FIELDS this database cannot supply.

    Run at startup and printed, because the failure mode is silent: a misspelled
    field is null forever, and a null renders as a dash, and a dash on a pit wall
    reads as "the car is not sending that" rather than "this program asked for
    the wrong name".

    A field that is missing from telemetry but present in last_known is fine and
    not reported: init_db adds late columns by ALTER TABLE, so a database that
    predates one still carries the metric.
    """
    conn = None
    try:
        conn, _mode = db.get_conn_ro(db_path or db.SQLITE_PATH)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(telemetry)")}
        metrics = {r[0] for r in conn.execute(
            "SELECT DISTINCT metric FROM last_known")}
    except Exception as exc:
        return [("(database)", "%s: %s" % (type(exc).__name__, exc))]
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return [(n, "no such column in telemetry, and never seen in last_known")
            for n in FIELDS if n not in cols and n not in metrics]


DEMO_PROFILE = os.path.join(_REPO, "profiles", "dor_280s.csv")


class DemoFeed:
    """A car that is not there, driving the dor_280s profile round and round.

    So the TV, the LAN, the mount and the viewing angle can all be set up and
    argued about in the garage before anyone has driven a lap -- and so the wall
    can be shown to the team without waiting for the car.

    It NEVER opens the database. That is the point: the demo has to work on a
    laptop with no telemetry.db, no collector and no car, and it must not be
    able to touch the race's data even by accident.

    Every payload carries demo: true, and the page turns that into a badge that
    cannot be missed. A pit wall showing invented numbers without saying so is
    worse than a blank screen -- somebody will eventually walk past it during a
    race and read it as real.
    """

    def __init__(self, profile_path=DEMO_PROFILE, start_lap=42):
        self.profile_path = profile_path
        self.start_lap = start_lap
        self._grid = []          # [(distance_m, speed_ms)]
        self._lap_s = 210.0
        self._t0 = time.time()
        self._load()

    def _load(self):
        rows = []
        with open(self.profile_path, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                rows.append((float(r["d(m)"]), float(r["V(m/s)"]),
                             float(r["Time(s)"])))
        rows.sort()
        self._grid = [(d, v) for d, v, _ in rows]
        self._lap_s = rows[-1][2]
        self._times = [t for _, _, t in rows]

    def _at(self, lap_t):
        """(distance_m, speed_ms) this many seconds into the lap."""
        times, grid = self._times, self._grid
        lo, hi = 0, len(times) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if times[mid] <= lap_t:
                lo = mid
            else:
                hi = mid
        span = times[lo + 1] - times[lo]
        f = (lap_t - times[lo]) / span if span > 0 else 0.0
        d0, v0 = grid[lo]
        d1, v1 = grid[lo + 1]
        return d0 + (d1 - d0) * f, v0 + (v1 - v0) * f

    def start(self):
        return self

    def stop(self):
        pass

    def read_once(self):
        return self.get()

    def get(self):
        now = time.time()
        elapsed = now - self._t0
        laps_done = int(elapsed // self._lap_s)
        lap_t = elapsed - laps_done * self._lap_s
        dist, v_ms = self._at(lap_t)
        kmh = v_ms * 3.6

        # Road load from the same model the baseline spreadsheet uses:
        # P = v * (250*a + 11 + 0.05*v^2). Close enough to look right, and it
        # gives the power tile something that moves with the corners.
        _, v_next = self._at(min(lap_t + 1.0, self._lap_s))
        power = v_ms * (250.0 * (v_next - v_ms) + 11.0 + 0.05 * v_ms * v_ms)

        lap = self.start_lap + laps_done
        # Eight laps from the first second, not eight after half an hour: the
        # point of the demo is that the finished screen is on the TV straight
        # away, so the lap list must not start empty.
        recent = []
        for i in range(8):
            n = lap - 1 - i
            recent.append({"lap": n,
                           "time_s": round(self._lap_s + math.sin(n) * 1.8, 1),
                           "energy_wh": round(38.0 + math.cos(n) * 1.2, 2)})

        return {
            "demo": True,
            "served_ts": now,
            "device_ts": now,
            "store_mode": "demo (no database opened)",
            "carried_ts": {},
            "calculated_lap": lap,
            "lap_distance_m": dist,
            "odometer_m": (lap - self.start_lap) * 4000.0 + dist,
            "lap_source": "demo",
            "mms_vehicle_speed_kmh": kmh,
            "target_speed_kmh": kmh,
            "mms_power_W": power,
            "mms_rpm": kmh / 0.020355,
            # Motor hotter than the controller, as on the car: the two tiles
            # must not read alike, or the demo hides a tile wired to the wrong
            # sensor -- which is exactly how this one went unnoticed.
            "mms_motor_temp_C": 52.0 + kmh / 14.0,
            "mms_temperature_C": 46.0 + kmh / 22.0,
            "mms_measured_voltage_V": 48.4,
            "bms_soc_percent": max(8.0, 92.0 - elapsed / 180.0),
            "bms_voltage_V": 52.9 - (92.0 - max(8.0, 92.0 - elapsed / 180.0)) * 0.06,
            "bms_current_A": max(0.0, power) / 52.0,
            "battery_temp_C": 31.0 + kmh / 60.0,
            "last_lap_time_s": recent[0]["time_s"] if recent else None,
            "last_lap_energy": recent[0]["energy_wh"] if recent else None,
            "last_lap_distance_m": 4000.0 if recent else None,
            # WATT-HOURS, the unit the car actually stores. These were kWh
            # once, which made a wall that printed Wh values under a "kWh"
            # label look perfectly correct in the demo for its whole life.
            "total_race_energy": round((lap - self.start_lap) * 38.0 + 1600.0, 1),
            "regen_energy": round((lap - self.start_lap) * 4.0, 1),
            "lap_energy_wh": round(38.0 * (lap_t / self._lap_s), 1),
            "stint_energy": None, "stint_regen_energy": None,
            "active_strategy": "dor_280s",
            "lat": None, "lon": None,
            "bms_has_error": 0, "bms_error_code": 0,
            "mms_has_error": 0, "mms_error_code": 0,
            "is_racing": True,
            "race_start_time": self._t0 - 3 * 3600,
            "lap_started_ts": now - lap_t,
            "recent_laps": recent,
            "recent_laps_built_ts": now,
        }


class Feed:
    """Reads the car's state on one thread; everyone else copies the result.

    Never raises at the caller. A database that is momentarily locked, missing
    or mid-checkpoint leaves the previous snapshot in place with an `error`
    attached, because a pit screen that goes blank is worse than one still
    showing numbers whose age it states. device_ts does not advance while that
    holds, so the page dims itself on its own.
    """

    def __init__(self, db_path=None, lap_count=8):
        self.db_path = db_path or db.SQLITE_PATH
        self.lap_count = lap_count
        self._lock = threading.Lock()
        self._value = {"error": "no reading yet"}
        self._laps = []
        self._laps_at = None
        self._lap_base = None        # (lap, read_at, energy_wh, at_m)
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------ #
    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="pit-wall-feed")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def get(self):
        with self._lock:
            return self._value

    # ------------------------------------------------------------------ #
    def _loop(self):
        """Owns the connection for its whole life. See the module note."""
        conn = mode = None
        next_laps = 0.0
        while not self._stop.is_set():
            try:
                if conn is None:
                    # (conn, mode): "ro" is SQLite refusing writes at the VFS
                    # layer, "query_only" the weaker SQL-layer fallback. The
                    # page shows which, because "the pit wall cannot corrupt
                    # telemetry" is worth being able to check on race day rather
                    # than take on trust.
                    conn, mode = db.get_conn_ro(self.db_path)
                if time.time() >= next_laps:
                    self._refresh_laps(conn)
                    next_laps = time.time() + LAP_TABLE_EVERY_S
                fresh = self._query(conn, mode)
            except Exception as exc:
                # Drop the handle rather than keep retrying a broken one: the
                # database may have been checkpointed, moved or replaced under
                # us, and the next pass should get a clean open.
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                conn = None
                next_laps = 0.0
                fresh = {"error": "%s: %s" % (type(exc).__name__, exc)}
            with self._lock:
                if "error" in fresh and "error" not in self._value:
                    self._value = dict(self._value, error=fresh["error"])
                else:
                    self._value = fresh
            self._stop.wait(REFRESH_S)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    def _query(self, conn, mode):
        row = db.latest_sample(conn)
        known = db.latest_known(conn)
        out = {"served_ts": time.time(), "store_mode": mode}

        # The same two-source rule the dashboard uses (_val_cf): the newest row
        # wins, and last_known is only a fallback for columns that row is null
        # for.
        #
        # This matters more here than it looks. last_known holds the newest
        # NON-NULL value of each metric with its own timestamp, so a field the
        # car stopped sending still has a value sitting in it — against the
        # team's current database every motor field is 51 h old and reads 0.0.
        # Served unqualified that is a confident "0 km/h, motor 47 C" on the pit
        # wall. So anything carried forward travels with the age it came from,
        # and the page dashes it out rather than drawing it as current.
        carried = {}
        cols = set(row.keys()) if row is not None else set()
        for name in FIELDS:
            value = row[name] if name in cols else None
            if value is not None:
                out[name] = value
                continue
            pair = known.get(name)
            if pair is None:
                out[name] = None
                continue
            value, ts = pair
            out[name] = value
            carried[name] = float(ts) if ts else None
        out["device_ts"] = (float(row["device_ts"])
                            if row is not None and row["device_ts"] else None)
        out["carried_ts"] = carried

        race = db.load_race_state(conn)
        out["is_racing"] = race.get("is_racing")
        out["race_start_time"] = race.get("race_start_time")
        out["lap_started_ts"] = self._lap_start(conn, out.get("calculated_lap"))
        out["lap_energy_wh"] = self._lap_energy(
            conn, out.get("calculated_lap"), out.get("total_race_energy"))

        with self._lock:
            out["recent_laps"] = list(self._laps)
            out["recent_laps_built_ts"] = self._laps_at
        return out

    @staticmethod
    def _lap_start(conn, lap):
        """When the car crossed the line into the lap it is on now.

        Index-backed by idx_telemetry_lap (device_id, calculated_lap), so the
        cost does not grow with the race. Returning None is normal before the
        first crossing — the page then shows a dash for the running lap time
        rather than counting up from the start of the session.
        """
        if lap is None:
            return None
        try:
            row = conn.execute(
                "SELECT MIN(device_ts) FROM telemetry "
                "WHERE device_id = ? AND CAST(calculated_lap AS INTEGER) = ?",
                (db.DEVICE_ID, int(lap)),
            ).fetchone()
        except Exception:
            return None
        return row[0] if row and row[0] else None

    def _lap_energy(self, conn, lap, total_energy):
        """Wh used so far on the lap the car is on, or None.

        None -- a dash on the wall -- rather than a number, whenever the figure
        would be a guess: no lap yet, no energy total, no stored sample of this
        lap, a baseline too far into the lap to mean anything, or a baseline
        taken before an energy reset. A short number on a pit wall is read as a
        good lap, so it must never be produced by a dropout.

        Runs on the Feed thread only, like every other read here.
        """
        if lap is None or total_energy is None:
            return None
        try:
            lap = int(lap)
        except (TypeError, ValueError):
            return None

        hit = self._lap_base
        if hit is None or hit[0] != lap or (time.time() - hit[1]) >= LAP_ENERGY_TTL_S:
            try:
                row = db.lap_start_energy(conn, lap)
            except Exception:
                return None
            hit = (lap, time.time(),
                   row["total_race_energy"] if row is not None else None,
                   row["lap_distance_m"] if row is not None else None)
            self._lap_base = hit

        _, _, base, at_m = hit
        if base is None:
            return None
        # The baseline is not the start of the lap: the beginning was never
        # received and subtracting understates the lap by whatever was missed.
        if at_m is not None and at_m > LAP_ENERGY_BASELINE_MAX_M:
            return None
        used = float(total_energy) - float(base)
        # A mildly negative lap is REAL -- energy is net of regen and may
        # legitimately decrease on a descent. Only a drop bigger than any lap
        # could physically regen is the energy reset it actually is.
        return None if used < -LAP_ENERGY_IMPLAUSIBLE_WH else used

    def _refresh_laps(self, conn):
        """The last few completed laps, newest first.

        Straight off fetch_laps, which reads the figures the CAR integrated —
        the pit never re-derives a lap time from samples, so this screen and
        the dashboard cannot disagree about what a lap took. Ordered by when
        each lap finished, not by lap number, so a car whose counter restarted
        does not put last week's lap 40 above today's lap 3. `kind` is the
        car's own tag (flying, in, out, ...), None from an older car.
        """
        rows = db.fetch_laps(conn)
        out = [{"lap": r["lap"], "time_s": r["lap_time_s"],
                "energy_wh": r["energy_wh"], "kind": r["kind"]}
               for r in rows[-self.lap_count:]]
        out.reverse()
        with self._lock:
            self._laps, self._laps_at = out, time.time()

    def read_once(self):
        """One synchronous read, for --once. Opens and closes its own handle."""
        conn = None
        try:
            conn, mode = db.get_conn_ro(self.db_path)
            self._refresh_laps(conn)
            return self._query(conn, mode)
        except Exception as exc:
            return {"error": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


class Handler(BaseHTTPRequestHandler):
    server_version = "PitWall/1.0"
    feed = None
    page_path = PAGE_PATH

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/wall.html"):
            return self._send_page()
        if path == "/live.json":
            return self._send_json(self.feed.get())
        if path == "/healthz":
            return self._send_json({"ok": True, "ts": time.time()})
        self.send_error(404, "pit wall serves / and /live.json")

    def _send_page(self):
        try:
            with open(self.page_path, "rb") as fh:
                body = fh.read()
        except OSError:
            return self.send_error(
                500, "wall.html is missing",
                "Build it first:  python tools/build_zolder_animation.py")
        self._send(body, "text/html; charset=utf-8")

    def _send_json(self, payload):
        self._send(json.dumps(payload, default=str).encode("utf-8"),
                   "application/json")

    def _send(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The builder writes the page, not this server, so a screen left running
        # overnight must not keep serving yesterday's copy out of cache.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass          # a poll every second would bury anything worth reading


def _local_ips():
    """Addresses a TV on the pit LAN could actually reach this on."""
    import socket
    found = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))      # never sends: just picks the route
        found.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    except Exception:
        pass
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="0.0.0.0",
                    help="0.0.0.0 serves the pit LAN; 127.0.0.1 this machine only")
    ap.add_argument("--db", default=None, help="defaults to db.SQLITE_PATH")
    ap.add_argument("--page", default=PAGE_PATH)
    ap.add_argument("--once", action="store_true",
                    help="print one snapshot and exit, without serving")
    ap.add_argument("--demo", action="store_true",
                    help="drive the page from profiles/dor_280s.csv instead of "
                         "the database, for setting up the TV before the car "
                         "exists. The page says DEMO in large letters.")
    args = ap.parse_args()

    feed = DemoFeed() if args.demo else Feed(args.db)
    if args.once:
        print(json.dumps(feed.read_once(), indent=2, default=str))
        return 0

    if not os.path.exists(args.page):
        print("!! %s does not exist yet." % os.path.relpath(args.page, _REPO))
        print("   Build it:  python tools/build_zolder_animation.py")

    missing = [] if args.demo else check_fields(args.db)
    if missing:
        print("!! %d field(s) this database cannot supply - they would show as "
              "dashes forever:" % len(missing))
        for name, why in missing:
            print("     %-26s %s" % (name, why))

    # Refuse to start if something is already on this port.
    #
    # ThreadingHTTPServer sets allow_reuse_address, which on Windows means
    # SO_REUSEADDR lets a SECOND server bind a port a first one is already
    # listening on. Neither errors. The two then take alternate connections,
    # so a screen polling once a second gets served by whichever won the race
    # -- during development that showed as a demo page reporting "store ro"
    # and live numbers every other second.
    #
    # On a pit wall that is the worst class of bug: two servers disagreeing,
    # no error anywhere, and a screen that looks plausible while flickering
    # between a real car and an invented one. So check first and say so.
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.4)
    busy = probe.connect_ex(("127.0.0.1", args.port)) == 0
    probe.close()
    if busy:
        print("!! Port %d is already serving." % args.port)
        print("   Another pit wall is almost certainly still running - close its")
        print("   console window (Ctrl-C) and start this one again.")
        print("   Windows would let both bind, and they would then answer")
        print("   alternate requests, so this one is refusing to start.")
        print("   To run a second one anyway:  --port %d" % (args.port + 1))
        return 2

    Handler.feed = feed.start()
    Handler.page_path = args.page
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True

    print("pit wall serving on port %d" % args.port)
    if args.demo:
        print("   DEMO MODE - no database is opened, the car is not real")
        print("   profile   %s" % os.path.relpath(DEMO_PROFILE, _REPO))
    else:
        print("   database  %s  (read-only)"
              % os.path.relpath(feed.db_path, _REPO))
    print("   page      %s" % os.path.relpath(args.page, _REPO))
    if args.host == "0.0.0.0":
        for ip in _local_ips():
            print("   open      http://%s:%d/" % (ip, args.port))
        print("   (if the TV cannot reach it, the venue WiFi is isolating "
              "clients - use the pit's own hotspot)")
    else:
        print("   open      http://%s:%d/" % (args.host, args.port))
    print("Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        feed.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
