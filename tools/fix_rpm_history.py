#!/usr/bin/env python3
"""
fix_rpm_history.py - correct historical mms_rpm and mms_vehicle_speed_kmh
=========================================================================
    python tools/fix_rpm_history.py            # DRY RUN: report, touch nothing
    python tools/fix_rpm_history.py --apply    # back up, then write

WHY THIS EXISTS
Two errors, both fixed on the car on 2026-08-23, left the store inconsistent:

  1. The controller reports exactly HALF the real motor speed (its pole-pair
     setting is twice the motor's actual count). Every stored mms_rpm is
     therefore half what it should be. Measured against gpsd over 1,955 moving
     samples: multiplier 2.0101, i.e. 2 to within half a percent.

  2. mms_vehicle_speed_kmh was decoded with the wrong divisor for the era.
     The controller was reconfigured at 2026-08-20 12:12, and the same raw field
     means different speeds either side of that. 82 % of the store predates it
     and came out 2.57x too low.

WHERE THE TRUTH COMES FROM
raw_json. The collector stores the payload the car actually sent and never
rewrites it, so it is the only trustworthy source for what a row originally
held. Both corrected values are recomputed from it - never from the current
column, which may already have been rewritten by an earlier repair.

The speed is recomputed by calling db._vehicle_speed(), the SAME function the
live ingest path uses, imported rather than re-implemented. History cannot then
be corrected by a different formula than new samples are.

WHY IT IS SAFE TO RE-RUN
It is idempotent by construction rather than by bookkeeping: the correct value
is derived from raw_json every time, so a second run computes the identical
number, sees the column already matches, and writes nothing. The watermark in
app_state is a convenience for reporting, not the safety mechanism.

Rows with no raw_json are SKIPPED, not guessed at - there is nothing to verify
them against.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for _p in (_REPO, os.path.join(_REPO, "Pit_Dashboard")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import drivetrain                                        # noqa: E402
import db as pit_db                                      # noqa: E402
from pit_config import SQLITE_PATH                       # noqa: E402

WATERMARK_KEY = "fix_rpm_history_done_at"


def corrected_row(payload):
    """(rpm, speed) as they should be, from the payload the car sent.

    Returns (None, None) when the payload carries neither, so the caller can
    count it as nothing-to-do rather than writing NULLs over real columns.
    """
    motor = (payload or {}).get("motor") or {}
    orig_rpm = motor.get("mms_rpm")
    orig_speed = motor.get("mms_vehicle_speed_kmh")

    rpm = None if orig_rpm is None else drivetrain.true_motor_rpm(orig_rpm)
    # _vehicle_speed needs the CORRECTED rpm: it identifies which era a row
    # belongs to from the ratio of the speed field to the rpm.
    speed = None if orig_speed is None else pit_db._vehicle_speed(orig_speed, rpm)
    return rpm, speed


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is a dry run)")
    ap.add_argument("--db", default=SQLITE_PATH, help="telemetry store to fix")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"no store at {args.db}")
        return 2

    print(f"store            : {args.db}")
    print(f"rpm multiplier   : x{1.0 / drivetrain.RPM_REPORT_SCALE:g}")
    print(f"speed divisors   : {drivetrain.CONTROLLER_SPEED_DIVISOR_LEGACY:.4f} "
          f"(pre-reconfig) / {drivetrain.CONTROLLER_SPEED_DIVISOR:.4f} (current)")
    print(f"mode             : {'APPLY' if args.apply else 'DRY RUN'}")
    print()

    if args.apply:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup = f"{args.db}.{stamp}.rpmfix.bak"
        print(f"backing up to {os.path.basename(backup)} ...", end=" ", flush=True)
        shutil.copy2(args.db, backup)
        print("done")
        print()

    # Read-only pass first, so a dry run cannot lock the store against the
    # collector and an apply run knows exactly what it intends to change.
    ro = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = ro.execute("SELECT rowid, raw_json, mms_rpm, mms_vehicle_speed_kmh "
                      "FROM telemetry").fetchall()
    ro.close()

    updates = []
    n_total = len(rows)
    n_norawjson = n_rpm = n_speed = n_already = 0
    rpm_before = rpm_after = 0.0
    spd_before = spd_after = 0.0

    for rowid, rj, cur_rpm, cur_speed in rows:
        if not rj:
            n_norawjson += 1
            continue
        try:
            payload = json.loads(rj)
        except (ValueError, TypeError):
            n_norawjson += 1
            continue

        want_rpm, want_speed = corrected_row(payload)
        set_rpm = want_rpm is not None and (
            cur_rpm is None or abs(float(cur_rpm) - want_rpm) > 1e-6)
        set_speed = want_speed is not None and (
            cur_speed is None or abs(float(cur_speed) - want_speed) > 1e-6)

        if not (set_rpm or set_speed):
            n_already += 1
            continue
        if set_rpm:
            n_rpm += 1
            rpm_before += abs(float(cur_rpm or 0.0))
            rpm_after += abs(want_rpm)
        if set_speed:
            n_speed += 1
            spd_before += abs(float(cur_speed or 0.0))
            spd_after += abs(want_speed)
        updates.append((want_rpm if set_rpm else cur_rpm,
                        want_speed if set_speed else cur_speed, rowid))

    print(f"rows in store            : {n_total:,}")
    print(f"  already correct        : {n_already:,}")
    print(f"  no raw_json (skipped)  : {n_norawjson:,}")
    print(f"  mms_rpm to correct     : {n_rpm:,}")
    print(f"  speed to correct       : {n_speed:,}")
    if n_rpm:
        print(f"    mean |rpm|  {rpm_before / n_rpm:8.1f} -> {rpm_after / n_rpm:8.1f}")
    if n_speed:
        print(f"    mean |speed| {spd_before / n_speed:7.2f} -> {spd_after / n_speed:7.2f}")
    print()

    if not updates:
        print("nothing to do")
        return 0

    if not args.apply:
        print(f"DRY RUN - {len(updates):,} rows would change. Re-run with --apply.")
        return 0

    conn = sqlite3.connect(args.db)
    try:
        conn.executemany(
            "UPDATE telemetry SET mms_rpm = ?, mms_vehicle_speed_kmh = ? "
            "WHERE rowid = ?", updates)
        conn.execute(
            "INSERT INTO app_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (WATERMARK_KEY, time.strftime("%Y-%m-%dT%H:%M:%S")))
        conn.commit()
    finally:
        conn.close()
    print(f"updated {len(updates):,} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
