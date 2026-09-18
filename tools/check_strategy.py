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

# A FIXTURE, deliberately fixed: this file checks the SHAPE of what the
# endpoint serves, so it must not move when the matrix is re-measured. Kept in
# step with constants.PROFILE_MATRIX anyway, because a fixture describing a car
# that does not exist is how the old 210 s / 80 Wh ladder outlived the profiles
# it came from.
TABLE = [
    {"label": "Fast (-10%)",    "lap_time_min": 256.50 / 60.0, "energy_wh": 159.5},
    {"label": "Med-Fast (-5%)", "lap_time_min": 270.75 / 60.0, "energy_wh": 152.25},
    {"label": "Base (285s)",    "lap_time_min": 285.00 / 60.0, "energy_wh": 145.0},
    {"label": "Med-Slow (+5%)", "lap_time_min": 299.25 / 60.0, "energy_wh": 137.75},
    {"label": "Slow (+10%)",    "lap_time_min": 313.50 / 60.0, "energy_wh": 130.5},
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
# Per WATT-HOUR, not per minute: 10 % of a pack is a tenth of the energy of
# 50 % of it, so a real tapering curve can still finish the top slice sooner.
# What a taper claims is that the last watt-hours go in slower.
def wh_per_min(a, b):
    t = se.charging_time_min(a, b)
    return (se.BATTERY_FULL_WH * (b - a) / 100.0) / t if t else float("inf")


want(wh_per_min(90, 100) < wh_per_min(5, 55),
     "no taper: the last 10%% takes %.0f Wh/min against %.0f Wh/min for the "
     "first 50%%" % (wh_per_min(90, 100), wh_per_min(5, 55)))
print("    5->55%%: %.1f min (%.0f Wh/min)   90->100%%: %.1f min (%.0f Wh/min)" % (
    se.charging_time_min(5, 55), wh_per_min(5, 55),
    se.charging_time_min(90, 100), wh_per_min(90, 100)))

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

# --------------------------------------------------------------------------- #
# The matrix editor's arithmetic, which is now a thing the crew presses mid-race
# --------------------------------------------------------------------------- #
# Only the FILL is checked here, never the save: writing constants.py is not
# something a check should do to a machine it is run on. The save's own
# guards -- the range refusals and the readback -- are exercised by calling the
# validator with nonsense, which writes nothing because it raises first.
print("\nmatrix fill (energy_model.ladder_from_anchor, via /api/strategy/matrix/fill):")
base_rows = api._matrix_rows()
for anchor in (base_rows[0], base_rows[len(base_rows) // 2], base_rows[-1]):
    filled = api.api_strategy_matrix_fill(api.MatrixFillBody(
        key=anchor["key"], target_s=anchor["target_s"],
        energy_wh=anchor["energy_wh"]))["rows"]
    by_key = {r["key"]: r for r in filled}

    # 1: the anchor reproduces itself. A fill that moves the row somebody just
    # typed is the fastest way to lose the crew's trust in the button.
    a = by_key[anchor["key"]]
    want(abs(a["target_s"] - anchor["target_s"]) < 0.05,
         "%s: the anchor's own lap time moved" % anchor["label"])
    want(abs(a["energy_wh"] - anchor["energy_wh"]) < 0.05,
         "%s: the anchor's own Wh moved" % anchor["label"])

    # 2: the pace ladder keeps its spacing, whichever row was the anchor
    for r in base_rows:
        ratio_was = r["target_s"] / anchor["target_s"]
        ratio_now = by_key[r["key"]]["target_s"] / a["target_s"]
        want(abs(ratio_was - ratio_now) < 1e-3,
             "%s: spacing changed at %s" % (anchor["label"], r["label"]))

    # 3: faster costs more, always. This is the one direction the whole tab
    # exists to judge, and a flat percentage on a ladder that was edited by
    # hand can invert it.
    ordered = sorted(filled, key=lambda r: r["target_s"])
    for x, y in zip(ordered, ordered[1:]):
        want(y["energy_wh"] < x["energy_wh"],
             "%s: %s (%.0f s) costs more than the slower %s (%.0f s)"
             % (anchor["label"], x["key"], x["target_s"], y["key"], y["target_s"]))

    # 4: and it costs more than a flat percentage says, because rolling loss
    # does not get cheaper when the driver slows down
    slowest = ordered[-1]
    # a flat ladder mirrors the pace change: +10 % slower -> -10 % energy
    flat = anchor["energy_wh"] * (2.0 - slowest["target_s"] / anchor["target_s"])
    if slowest["key"] != anchor["key"]:
        want(slowest["energy_wh"] > flat,
             "%s: the slow end is not dearer than a flat ladder (%.1f vs %.1f)"
             % (anchor["label"], slowest["energy_wh"], flat))

    print("    anchored on %-16s -> %s" % (
        anchor["label"],
        "  ".join("%.0f Wh" % r["energy_wh"] for r in ordered)))

# 5: the editor refuses what a slipped decimal point looks like
for bad, why in ((0.0, "zero Wh"), (-5.0, "negative Wh"), (99999.0, "99 kWh a lap")):
    try:
        api.api_strategy_matrix_save(api.MatrixBody(rows=[api.MatrixRow(
            key=base_rows[0]["key"], target_s=base_rows[0]["target_s"],
            energy_wh=bad)]))
    except Exception:
        pass                       # HTTPException, which is the point
    else:
        want(False, "the editor accepted %s" % why)
for bad, why in ((0.0, "a zero lap time"), (2.0, "a 2 s lap")):
    try:
        api.api_strategy_matrix_save(api.MatrixBody(rows=[api.MatrixRow(
            key=base_rows[0]["key"], target_s=bad,
            energy_wh=base_rows[0]["energy_wh"])]))
    except Exception:
        pass
    else:
        want(False, "the editor accepted %s" % why)
print("    refuses zero, negative and absurd values without writing")

print()
if failures:
    print("FAILED (%d)" % len(failures))
    sys.exit(1)
print("All strategy payload checks passed.")
