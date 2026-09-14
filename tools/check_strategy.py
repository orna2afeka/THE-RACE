#!/usr/bin/env python3
"""
check_strategy.py - the strategy screen's SERVED payload, checked like the engine
================================================================================
    python tools/check_strategy.py

strategy_engine.py self-checks itself (run it directly). This repeats the same
ten checks on what /api/strategy actually SERVES -- rows and traces together in
one payload -- for the engine's own five scenarios and every strategy. So a
mistake in the serialisation, or a chart trace that stopped matching the table
it sits under, cannot pass on the engine's good name.

Checks 1 and 2 are the ones that matter most. They are what the old
table/chart split would have failed: the table computed from a tapering
charge curve while the chart drew straight lines from a flat rate.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Pit_Web import api                                          # noqa: E402
import strategy_engine as se                                     # noqa: E402

TABLE = [
    {"label": "Fast (-10%)",    "lap_time_min": 3.15, "energy_wh": 88.0},
    {"label": "Med-Fast (-5%)", "lap_time_min": 3.33, "energy_wh": 84.0},
    {"label": "Base (210s)",    "lap_time_min": 3.50, "energy_wh": 80.0},
    {"label": "Med-Slow (+5%)", "lap_time_min": 3.67, "energy_wh": 76.0},
    {"label": "Slow (+10%)",    "lap_time_min": 3.85, "energy_wh": 72.0},
]
SCENARIOS = [
    ("24 h, full pack",        24 * 60.0, se.BATTERY_FULL_WH),
    ("6 h, part charged",      360.0,     3000.0),
    ("45 min, no stop fits",   45.0,      se.BATTERY_FULL_WH),
    ("20 min, nearly empty",   20.0,      600.0),
    ("24 h from the floor",    24 * 60.0, 500.0),
]
COLUMNS = ["Label", "Lap Time", "Speed (km/h)", "Total Laps", "Energy/Lap (Wh)",
           "Pit Strategy", "Charge To", "Pit Time", "Driver Swaps", "Final SoC"]

failures = []


def want(cond, msg):
    if not cond:
        failures.append(msg)
        print("      ** FAIL ** " + msg)


print("charging curve (NOT measured -- CHARGING_CURVE_IS_MEASURED=%s):"
      % se.CHARGING_CURVE_IS_MEASURED)
# check 10: monotonic, zero to or below, and the taper
want(se.charging_time_min(5, 55) < se.charging_time_min(5, 70)
     < se.charging_time_min(5, 90) < se.charging_time_min(5, 100),
     "charge time is not monotonic in target SoC")
want(se.charging_time_min(70, 70) == 0.0, "charging to where we are costs time")
want(se.charging_time_min(90, 70) == 0.0, "charging DOWN returns a time")
want(se.charging_time_min(90, 100) > se.charging_time_min(5, 55),
     "no taper: 90-100% should cost more than 5-55%")
print("    5->55%%: %.1f min   90->100%%: %.1f min" % (
    se.charging_time_min(5, 55), se.charging_time_min(90, 100)))

for name, left, start_wh in SCENARIOS:
    print("\n%s:" % name)
    out = api._strategy_payload(left, start_wh, 0, TABLE)
    want(list(out["rows"][0].keys()) == COLUMNS,
         "column order changed: %s" % list(out["rows"][0].keys()))
    want(len(out["rows"]) == len(out["traces"]), "rows and traces not aligned")
    want(out["floorWh"] == se.BATTERY_FLOOR_WH and out["capacityWh"] == se.BATTERY_FULL_WH,
         "served constants disagree with the engine")

    for row, tr in zip(out["rows"], out["traces"]):
        nm = row["Label"]
        if tr is None:
            want(row["Total Laps"] == 0, "%s: no trace but laps > 0" % nm)
            print("    %-16s no plan" % nm)
            continue
        pts = tr["points"]
        kinds = [p["kind"] for p in pts]
        laps = kinds.count("lap")
        swaps = kinds.count("swap")
        stops = kinds.count("stop")
        pit = sum(s["stopMin"] for s in tr["stops"])

        # 1-2: the chart and the table must describe the same race
        want(laps == row["Total Laps"],
             "%s: trace %d laps, table %d" % (nm, laps, row["Total Laps"]))
        want(stops == len(tr["stops"]), "%s: stop count disagrees" % nm)
        want(swaps == tr["swaps"], "%s: swap count disagrees" % nm)
        want(row["Driver Swaps"] == ("%d Swaps" % tr["swaps"]),
             "%s: swap column disagrees with trace" % nm)

        # 3-5: the race must be legal
        want(pts[-1]["minute"] <= left + 1e-6, "%s: runs past the flag" % nm)
        want(min(p["wh"] for p in pts) >= se.BATTERY_FLOOR_WH - 1e-6,
             "%s: goes below the floor" % nm)
        want(max(p["wh"] for p in pts) <= tr["capacityWh"] + 1e-6,
             "%s: goes above capacity" % nm)
        want(all(pts[i]["minute"] >= pts[i - 1]["minute"] - 1e-9
                 for i in range(1, len(pts))), "%s: time runs backwards" % nm)

        # 6-7: stops
        for st in tr["stops"]:
            want(st["stopMin"] >= se.MIN_STOP_DURATION_MIN - 1e-6,
                 "%s: a stop is under the minimum" % nm)
            want(st["stopMin"] >= st["chargeMin"] - 1e-6,
                 "%s: a stop is shorter than its own charge" % nm)
        want(abs(pit - tr["pitMin"]) < 1e-6, "%s: pit time does not add up" % nm)
        if tr["stops"]:
            want(row["Pit Time"] == ("%.0f m" % tr["pitMin"]),
                 "%s: Pit Time column disagrees with trace" % nm)
            want(row["Charge To"] == " / ".join("%.0f%%" % s["socAfter"] for s in tr["stops"]),
                 "%s: Charge To column disagrees with trace" % nm)

        # 8: the time budget balances
        want(abs((row["Total Laps"] * tr["lapTimeMin"]
                  + tr["swaps"] * se.DRIVER_CHANGE_TIME_MIN + pit)
                 - tr["timeUsedMin"]) < 1e-6,
             "%s: drive + swaps + pit != time used" % nm)

        # 9: no driver over the stint limit
        run = worst = 0.0
        for k in kinds:
            if k == "lap":
                run += tr["lapTimeMin"]
                worst = max(worst, run)
            elif k in ("swap", "stop"):
                run = 0.0
        want(worst <= se.DRIVER_STINT_LIMIT_MIN + 1e-6,
             "%s: a driver runs %.1f min" % (nm, worst))

        # the wire format itself
        want(all(isinstance(p["wh"], float) or isinstance(p["wh"], int) for p in pts),
             "%s: a non-numeric Wh in the trace" % nm)
        want(set(kinds) <= {"start", "lap", "swap", "stop", "charge", "hold"},
             "%s: unknown point kind %s" % (nm, set(kinds)))

        print("    %-16s %3d laps | %-26s | pit %5.1f | %d swaps | %d points"
              % (nm, row["Total Laps"], row["Pit Strategy"], pit, tr["swaps"], len(pts)))

print()
if failures:
    print("FAILED (%d)" % len(failures))
    sys.exit(1)
print("All strategy payload checks passed.")
