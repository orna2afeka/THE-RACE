#!/usr/bin/env python3
"""
demo_seed.py - build a self-contained DEMO store so the whole dashboard is live
===============================================================================
    python tools/demo_seed.py [path/to/demo.db]

Writes a COPY of the telemetry store containing a synthetic race that is
already an hour old, so every panel on the dashboard has something real to
show without a car, without Firebase and without the collector.

IT NEVER TOUCHES Pit_Dashboard/telemetry.db. The demo store is a separate file
and the dashboard is pointed at it with SOLARRACE_DB_PATH, so nothing here can
reach race data. Run "Demo Dashboard.bat" to seed and launch in one step.

WHAT THE DEMO IS BUILT TO SHOW, panel by panel:

  Sector times   All five cell states at once, which is the point of the
                 rework. The last completed lap owns sectors 2 and 6 outright
                 (PURPLE), several sectors improved on the lap before (GREEN)
                 and several dropped off (YELLOW). A deliberate 40 s telemetry
                 dropout in sector 2 of the lap IN PROGRESS leaves those gates
                 unknowable (DASHED, "missing"), while the sectors either side
                 of the hole survive it. That lap stops mid-sector-5, so the
                 rest of its row is PENDING - a plain em dash, visibly
                 different from the dashed one.

  Track position The car is mid-lap with a target speed taken from the profile
                 it reports in `active_strategy`, so the card reads
                 "from base_210s" rather than the assumed-profile warning.

  Driver stint   Started 108 minutes ago against a 2 hour limit, so the
                 countdown is inside the amber warning band and the banner is
                 up. Press "Driver changed" to watch it reset.

  Live tiles     Every metric the car publishes, with a plausible state of
                 charge, pack and motor temperatures, power and regen.

  History        An hour of samples at 2 Hz, so every chart has a real curve
                 with real gaps rather than a flat line.

  Race control   A race running for an hour, so "Correct start time", "Reset
                 race clock" and the undo all have something to act on.

Deliberately NOT seeded: faults. A demo that cries wolf teaches the crew to
ignore the fault panel, which is the one thing it must never do.
"""

import json
import math
import os
import random
import sqlite3
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "Pit_Dashboard"))

SRC = os.path.join(_ROOT, "Pit_Dashboard", "telemetry.db")
DST = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_ROOT, "demo_telemetry.db")

LAP_M = 4000.0
HZ = 2.0
NLAPS = 20
BOUNDS = [(1, 0, 600), (2, 600, 1000), (3, 1000, 1800), (4, 1800, 1910),
          (5, 1910, 2400), (6, 2400, 2500), (7, 2500, 3000), (8, 3000, 3430),
          (9, 3430, 4000)]
BASE = {sid: (b - a) / LAP_M * 210.0 for sid, a, b in BOUNDS}

# Zolder paddock, the same fallback the map uses. The demo drives a rough oval
# around it so the GPS trail and the last-lap trace both draw.
LAT0, LON0 = 50.9895, 5.2568

random.seed(11)          # reproducible: a demo that looks different every run
                         # is a demo you cannot describe to anyone.


def gps(frac):
    """A lap fraction as a point on a rough oval around the paddock."""
    a = 2.0 * math.pi * frac
    return (LAT0 + 0.0042 * math.sin(a) - 0.0011 * math.sin(2 * a),
            LON0 + 0.0065 * math.cos(a) + 0.0009 * math.sin(3 * a))


def main():
    if not os.path.exists(SRC):
        sys.exit("cannot find %s - run this from the repository" % SRC)
    if os.path.abspath(DST) == os.path.abspath(SRC):
        sys.exit("refusing to overwrite the real store at %s" % SRC)

    src = sqlite3.connect(SRC)
    dst = sqlite3.connect(DST)
    with dst:
        src.backup(dst)                      # schema + a consistent copy
    src.close()
    dst.close()
    # Bring the copy up to the CURRENT schema. The real store is migrated by
    # the collector on start-up; a copy taken from an older snapshot would
    # otherwise lack the heartbeat and per-cell columns seeded below.
    import db
    dst = db.get_conn(DST)
    db.init_db(dst)
    dst.execute("DELETE FROM telemetry")     # demo data only, nothing real
    # And the carry-forward table, which the copy inherited from the real
    # store. Left in place it would quietly fill any field the demo does
    # not write with a REAL reading from weeks ago.
    dst.execute("DELETE FROM last_known")

    # --- the laps ---------------------------------------------------------- #
    laps = [{sid: BASE[sid] * (1.0 + random.uniform(-0.035, 0.045))
             for sid, _a, _b in BOUNDS} for _ in range(NLAPS)]
    # The last COMPLETED lap takes sectors 2 and 6 outright, so purple is on
    # screen the moment the page opens instead of depending on luck.
    for sid in (2, 6):
        laps[NLAPS - 2][sid] = min(l[sid] for l in laps) * 0.975

    total_s = sum(sum(l.values()) for l in laps)
    now = time.time()
    t = now - total_s
    race_start = t - 120.0                   # green flag just before lap one

    rows = []
    tag = 1
    soc, motor_c, batt_c, energy = 96.0, 42.0, 28.0, 0.0
    prev_lap_energy = 0.0
    for li, lap in enumerate(laps):
        partial = (li == NLAPS - 1)
        lap_energy = 0.0
        for sid, a, b in BOUNDS:
            dur, span = lap[sid], b - a
            n = max(2, int(dur * HZ))
            speed = span / dur * 3.6
            for k in range(n):
                frac = k / n
                d = a + frac * span
                ts = t + frac * dur
                # Plausible, gently varying physics. None of it is used for
                # anything but making the tiles and charts look like a car.
                throttle = max(0.0, min(100.0, 58 + 26 * math.sin(d / 300.0)))
                power = 900 + 520 * math.sin(d / 260.0) + random.uniform(-40, 40)
                regen = max(0.0, -power)
                soc -= 0.00055
                motor_c += (0.0009 if throttle > 60 else -0.0006)
                batt_c += (0.0004 if power > 1100 else -0.0003)
                energy += max(power, 0.0) / 3600.0 / HZ
                lap_energy += max(power, 0.0) / 3600.0 / HZ
                lat, lon = gps(d / LAP_M)
                rows.append({
                    "device_ts": ts, "device_id": "solarcar",
                    "lap_distance_m": d, "calculated_lap": float(tag),
                    "active_strategy": "base_210s",
                    "mms_vehicle_speed_kmh": speed,
                    "lat": lat, "lon": lon,
                    "bms_soc_percent": soc,
                    "mms_estimated_soc_percent": soc - 0.6,
                    "bms_voltage_V": 117.4 - (96.0 - soc) * 0.21,
                    "mms_measured_voltage_V": 117.0 - (96.0 - soc) * 0.21,
                    "bms_current_A": power / 117.0,
                    "mms_current_A": power / 117.0 * 0.98,
                    "mms_power_W": power,
                    "mms_rpm": speed / 3.6 / (0.278 * 2 * math.pi) * 60.0,
                    "mms_temperature_C": 34 + 6 * math.sin(d / 900.0),
                    "mms_motor_temp_C": motor_c,
                    "battery_temp_C": batt_c,
                    "mms_motor_ohms": 0.081,
                    "mms_throttle_percent": throttle,
                    "mms_throttle_mv": 800 + throttle * 31.0,
                    "target_speed_kmh": 68.0,
                    "solar_current_A": max(0.0, 5.2 + 2.1 * math.sin(ts / 900.0)),
                    "regen_energy": regen / 3600.0 / HZ,
                    "total_race_energy": energy,
                    "mms_trip_m": li * LAP_M + d,
                    # The car's heartbeat block: a healthy car sends an empty
                    # can_detail, so the badge shows plain LIVE.
                    "pi_uptime_s": 3600.0 + (ts - (now - total_s)),
                    "can_state": "live", "can_silent_s": 0.4, "can_detail": "",
                    "can_frames": 120000 + len(rows) * 40, "gps_fix": 1,
                    "gps_detail": "3D fix, 11 satellites",
                    # DS004: 26 modules wired and reporting, one running low so
                    # the compliance screen has a coloured tile to show.
                    "bms_string_count": 26,
                    **{"bms_cell_%02d_V" % i: (3.71 - (96.0 - soc) * 0.004
                                               - (0.55 if i == 9 else 0.0)
                                               + 0.01 * math.sin(i))
                       for i in range(1, 27)},
                    # DS003: modules A (ids 1-13) and B (ids 21-33) enabled;
                    # id 14 is a failed thermistor reporting the Orion module's
                    # nonsense negative, which the pit must gate out.
                    **{"bms_cell_temp_%02d_C" % i: 29.0 + 4.0 * math.sin(i / 3.0) + (98.0 - soc)
                       for i in list(range(1, 14)) + list(range(21, 34))},
                    "bms_cell_temp_14_C": -41.0,
                })
                # The car stamps the finished lap onto the rows of the NEXT
                # lap. Without this the last-lap tiles show whatever happened
                # to be carried forward, which read as a 17 minute lap.
                if li > 0:
                    rows[-1]["last_lap_time_s"] = sum(laps[li - 1].values())
                    rows[-1]["last_lap_energy"] = prev_lap_energy
            # A 40 s telemetry dropout in sector 2 of the lap IN PROGRESS.
            # On that row it costs the gates inside the hole and nothing else,
            # so the demo shows a dashed "missing" cell AND a complete last lap
            # with a real lap total. On the completed lap it would have blanked
            # the total, because a partial sum is a wrong lap time.
            if partial and sid == 2:
                del rows[-n:]
            t += dur
            if partial and sid == 5:         # the car is out on track, in S5
                break
        prev_lap_energy = lap_energy
        tag += 1

    # Land the newest sample a second ago. Without this the last row is as old
    # as the partial lap is short, and the whole dashboard opens greyed out as
    # STALE -- which is correct behaviour reading as a broken demo.
    shift = (now - 1.0) - max(r["device_ts"] for r in rows)
    for r in rows:
        r["device_ts"] += shift
    race_start += shift

    cols = sorted({k for r in rows for k in r})
    sql = "INSERT INTO telemetry (%s) VALUES (%s)" % (
        ",".join(cols), ",".join("?" * len(cols)))
    dst.executemany(sql, [[r.get(c) for c in cols] for r in rows])

    # --- race clock and driver stint --------------------------------------- #
    def put(key, value):
        dst.execute("INSERT INTO app_state (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(value)))

    put("race", {"is_racing": True, "race_start_time": race_start})
    # 108 minutes into a 2 hour limit leaves 12, inside the 15 minute amber
    # band, so the banner is up and one press of "Driver changed" resets it.
    stint_started = now - 108 * 60
    put("driver_stint", {"started_at": stint_started, "stint": 3,
                         "driver": "Dana", "accumulated_s": 0.0,
                         "running_since": stint_started})
    put("race_undo", {})
    dst.commit()

    n_laps = len({r["calculated_lap"] for r in rows})
    print("Demo store written: %s" % DST)
    print("  %d samples, %d laps, %.0f minutes of racing"
          % (len(rows), n_laps, total_s / 60.0))
    print("  race started %.0f min ago, driver stint 108 min in (amber)" % ((now - race_start) / 60))
    print("  sector times: last lap complete with purple in S2 and S6;")
    print("                current lap has a dropout in S2 and is out in S5")
    print("  cell voltages: 26/26 modules live, module 9 low; thermistor 14 gated")
    print("  car health: heartbeat healthy (the feed flips CAN silent now and then)")
    dst.close()


if __name__ == "__main__":
    main()
