#!/usr/bin/env python3
"""
demo_feed.py - keep the demo store LIVE, the way the collector would
====================================================================
    python tools/demo_feed.py [path/to/demo.db]

demo_seed.py writes a race that is already an hour old. Ten seconds later the
newest sample is older than DATA_STALE_AFTER_S and the whole dashboard greys
out as STALE, which is correct behaviour reading as a broken demo. This is the
other half: it appends a fresh sample twice a second, so the demo behaves like
a car that is actually out on track.

That matters for the sector rework specifically, because the thing worth
watching is MOTION. The current row filling in sector by sector, the rows
swapping when the lap rolls, a green turning purple when the driver takes a
sector outright. A still picture cannot show any of it.

It writes ONLY to the demo store handed to it on the command line, and refuses
to run against Pit_Dashboard/telemetry.db. Stop it with Ctrl+C.
"""

import math
import os
import random
import sqlite3
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))

from demo_seed import BOUNDS, BASE, HZ, LAP_M, gps          # noqa: E402

REAL = os.path.join(_ROOT, "Pit_Dashboard", "telemetry.db")
DST = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_ROOT, "demo_telemetry.db")


def lap_plan():
    """Sector durations for one lap, a little different every time.

    The spread is what makes the demo worth watching: over a few laps the
    driver takes some sectors outright and loses others, so purple moves
    around instead of sitting still.
    """
    return {sid: BASE[sid] * (1.0 + random.uniform(-0.05, 0.06))
            for sid, _a, _b in BOUNDS}


def main():
    if os.path.abspath(DST) == os.path.abspath(REAL):
        sys.exit("refusing to write to the real store at %s" % REAL)
    if not os.path.exists(DST):
        sys.exit("%s does not exist - run tools/demo_seed.py first" % DST)

    # demo_seed seeds the RNG for reproducibility; the live feed wants
    # variety, so undo that here.
    random.seed()

    conn = sqlite3.connect(DST)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT calculated_lap, lap_distance_m, bms_soc_percent, "
        "       total_race_energy, mms_motor_temp_C, battery_temp_C "
        "FROM telemetry ORDER BY device_ts DESC LIMIT 1").fetchone()
    tag = float(row["calculated_lap"])
    dist = float(row["lap_distance_m"] or 0.0)
    soc = float(row["bms_soc_percent"] or 90.0)
    energy = float(row["total_race_energy"] or 0.0)
    motor_c = float(row["mms_motor_temp_C"] or 43.0)
    batt_c = float(row["battery_temp_C"] or 28.0)

    plan = lap_plan()
    lap_energy = 0.0
    row2 = conn.execute("SELECT last_lap_time_s, last_lap_energy FROM telemetry "
                        "WHERE last_lap_time_s IS NOT NULL ORDER BY device_ts DESC LIMIT 1").fetchone()
    lap_last = float(row2["last_lap_time_s"]) if row2 else None
    energy_last = float(row2["last_lap_energy"]) if row2 and row2["last_lap_energy"] is not None else None
    lap_started = time.time() - dist / LAP_M * sum(plan.values())

    print("Feeding %s" % DST)
    print("  resuming lap %d at %.0f m, %.1f%% charge" % (tag, dist, soc))
    print("  appending a sample every %.2f s - Ctrl+C to stop" % (1.0 / HZ))

    step = 1.0 / HZ
    nxt = time.time()
    t0 = time.time()
    frames = 120000
    try:
        while True:
            now = time.time()
            if now < nxt:
                time.sleep(min(step, nxt - now))
                continue
            nxt += step

            # Which sector are we in, and how fast does the plan say to go?
            sid, a, b = next(((s, x, y) for s, x, y in BOUNDS if x <= dist < y),
                             BOUNDS[-1])
            speed_ms = (b - a) / plan[sid]
            dist += speed_ms * step

            if dist >= LAP_M:                 # the lap trigger fires
                dist -= LAP_M
                tag += 1
                plan = lap_plan()
                lap_last = time.time() - lap_started
                lap_started = time.time()
                energy_last, lap_energy = lap_energy, 0.0
                print("  lap %d done in %.2f s" % (tag - 1, lap_last))

            throttle = max(0.0, min(100.0, 58 + 26 * math.sin(dist / 300.0)))
            power = 900 + 520 * math.sin(dist / 260.0) + random.uniform(-40, 40)
            soc = max(5.0, soc - 0.00055)
            motor_c += (0.0009 if throttle > 60 else -0.0006)
            batt_c += (0.0004 if power > 1100 else -0.0003)
            energy += max(power, 0.0) / 3600.0 / HZ
            lap_energy += max(power, 0.0) / 3600.0 / HZ
            lat, lon = gps(dist / LAP_M)

            # The car's heartbeat. For 40 s in every 4 minutes the second CAN
            # channel goes quiet, which is the failure the badge exists to
            # surface: can0 talking normally while can1 has died. The badge
            # turns amber and names the channel, then clears on its own.
            up = time.time() - t0
            quiet = (up % 240.0) > 200.0
            frames += 0 if quiet else 40
            health = ("live", 0.4, "can1 silent %ds" % int(up % 240.0 - 200.0) if quiet else "", 1)

            cells = {"bms_cell_%02d_V" % i: (3.71 - (96.0 - soc) * 0.004
                                            - (0.55 if i == 9 else 0.0)
                                            + 0.01 * math.sin(i))
                     for i in range(1, 27)}
            cells.update({"bms_cell_temp_%02d_C" % i: 29.0 + 4.0 * math.sin(i / 3.0) + (98.0 - soc)
                          for i in list(range(1, 14)) + list(range(21, 34))})
            cells["bms_cell_temp_14_C"] = -41.0       # the failed thermistor
            ccols = sorted(cells)

            conn.execute(
                "INSERT INTO telemetry (device_ts, device_id, lap_distance_m,"
                " calculated_lap, active_strategy, mms_vehicle_speed_kmh, lat, lon,"
                " bms_soc_percent, mms_estimated_soc_percent, bms_voltage_V,"
                " mms_measured_voltage_V, bms_current_A, mms_current_A, mms_power_W,"
                " mms_rpm, mms_temperature_C, mms_motor_temp_C, battery_temp_C,"
                " mms_motor_ohms, mms_throttle_percent, mms_throttle_mv,"
                " target_speed_kmh, total_race_energy, mms_trip_m,"
                " last_lap_time_s, last_lap_energy,"
                " pi_uptime_s, can_state, can_silent_s, can_detail, can_frames,"
                " gps_fix, gps_detail, bms_string_count, " + ",".join(ccols) + ")"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?,?,?,?,?" + ",?" * len(ccols) + ")",
                (time.time(), "solarcar", dist, tag, "base_210s",
                 speed_ms * 3.6, lat, lon, soc, soc - 0.6,
                 117.4 - (96.0 - soc) * 0.21, 117.0 - (96.0 - soc) * 0.21,
                 power / 117.0, power / 117.0 * 0.98, power,
                 speed_ms / (0.278 * 2 * math.pi) * 60.0,
                 34 + 6 * math.sin(dist / 900.0), motor_c, batt_c, 0.081,
                 throttle, 800 + throttle * 31.0, 68.0,
                 energy, tag * LAP_M + dist, lap_last, energy_last,
                 3600.0 + up, health[0], health[1], health[2], frames,
                 health[3], "3D fix, 11 satellites", 26,
                 *[cells[c] for c in ccols]))
            conn.commit()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
