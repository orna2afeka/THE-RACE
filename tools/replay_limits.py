#!/usr/bin/env python3
"""
replay_limits.py - measure what the thresholds would actually have done
======================================================================
Replays every recorded sample in the pit's telemetry store through
limits.classify() and reports, per metric, the share of samples that would have
shown amber and red.

This is the check that keeps limits.py honest. "Is 120 the right motor-temp
warning?" is not an opinion once you can answer "it would have been amber for
4.5 % of the last race". The thresholds in limits.py were chosen against this
output, so re-run it after changing any of them.

    python tools/replay_limits.py [path-to-telemetry.db]

Read-only, always: it opens the store with mode=ro and never writes. Point it at
one of the telemetry.db.*.bak snapshots if you would rather not touch the live
file at all while the collector has it open.

WHAT GOOD LOOKS LIKE
Amber should be rare enough to be worth reading and red rarer still. A metric
sitting amber for a third of a race is the failure this whole file exists to
catch: the crew stops looking. Roughly:

    amber   under ~5 % of samples
    red     essentially 0 % across a race the car survived

A metric far outside that is mis-set - or the car really was in trouble, which
the fault log will agree with.
"""
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import limits                                                   # noqa: E402

DEFAULT_DB = os.path.join(_REPO, "Pit_Dashboard", "telemetry.db")

# metric -> (db column, compare magnitude?)
#
# The magnitude flag mirrors what the HUD does at the call site: both currents
# are signed, discharge is negative, and the gauges apply abs() before handing
# the value over. Comparing the raw signed value here would report a discharge
# spike as perfectly healthy.
COLUMNS = [
    ("motor temp",      limits.MOTOR_TEMP,    "mms_motor_temp_C",         False),
    ("ctrl temp",       limits.CTRL_TEMP,     "mms_temperature_C",        False),
    ("max cell temp",   limits.CELL_TEMP,     "battery_temp_C",           False),
    ("pack voltage",    limits.PACK_VOLTAGE,  "mms_measured_voltage_V",   False),
    ("state of charge", limits.SOC,           "bms_soc_percent",          False),
    ("batt current",    limits.BATT_CURRENT,  "bms_current_A",            True),
    ("motor current",   limits.MOTOR_CURRENT, "mms_current_A",            True),
    ("motor power",     limits.POWER,         "mms_power_W",              False),
    ("speed",           limits.SPEED,         "mms_vehicle_speed_kmh",    False),
]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    if not os.path.exists(path):
        print(f"no telemetry store at {path}")
        return 2

    # mode=ro on a URI, never a plain connect(): a plain open would create the
    # file if the path were wrong, and could write to a store the collector owns.
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    total = conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]
    print(f"{os.path.basename(path)}: {total:,} samples")
    print()
    print(f"{'metric':<16} {'samples':>8} {'amber':>8} {'red':>8}   "
          f"{'warn':>7} {'crit':>7}   worst seen")
    print("-" * 78)

    for name, t, col, magnitude in COLUMNS:
        rows = [r[0] for r in conn.execute(
            f"SELECT {col} FROM telemetry WHERE {col} IS NOT NULL")]
        if magnitude:
            rows = [abs(v) for v in rows]
        if t is limits.PACK_VOLTAGE:
            # Mirror what the HUD does on the way in, or this report credits the
            # thresholds with alarms that the real display never shows.
            rows = [v for v in (limits.plausible_pack_voltage(x) for x in rows)
                    if v is not None]
        n = len(rows)
        if not n:
            print(f"{name:<16} {'-':>8}   (never reported)")
            continue

        warn = sum(1 for v in rows if limits.classify(v, t) == limits.WARNING)
        crit = sum(1 for v in rows if limits.classify(v, t) == limits.CRITICAL)
        # "Worst" is the low end for a low-side metric and the high end for the
        # rest, which is the number a human actually wants to see next to a
        # fire-rate: the closest this metric ever came to the threshold.
        worst = min(rows) if t.low_side else max(rows)

        def f(v):
            return "   -   " if v is None else f"{v:7.1f}"

        flag = ""
        if crit * 100.0 / n > 1.0:
            flag = "  <-- red too often"
        elif warn * 100.0 / n > 10.0:
            flag = "  <-- amber too often"

        print(f"{name:<16} {n:>8,} {warn*100.0/n:>7.2f}% {crit*100.0/n:>7.2f}%   "
              f"{f(t.warn)} {f(t.crit)}   {worst:>8.1f}{flag}")

    print()
    print("Amber under ~5 % and red near 0 % is the target. Anything flagged")
    print("above either needs a different number or the car needs looking at.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
