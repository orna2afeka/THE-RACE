"""
backfill_columns.py — populate newly added telemetry columns from raw_json
=========================================================================
Every row in telemetry.db keeps the car's full payload in `raw_json`, which is
why adding a column is never a data-loss event: the values were always stored,
just not in a place the dashboard could chart or export.

    python tools/backfill_columns.py --dry-run   # report only, touch nothing
    python tools/backfill_columns.py             # write the values in

WHY THIS EXISTS
The car published sixteen fields the pit had no column for, so they were dropped
on arrival. Seven of them are real measurements the pit wants — the controller's
pack voltage and current, its own speed and SoC estimate, regen energy, the trip
counter and the profile's target speed. Adding the columns makes new samples land
correctly, but historical rows stay NULL until something fills them, which would
leave a race's worth of charts blank for exactly the metrics you just added.

SAFETY
  * Only ever writes a column that is currently NULL. A value already in a column
    wins — this cannot overwrite something the collector wrote.
  * Never touches raw_json, so it can be run again, and re-run after adding more
    columns later.
  * Wrapped in one transaction: it either completes or changes nothing.
  * --dry-run prints exactly what would change without opening a write.

Take a copy of telemetry.db first anyway. It costs nothing and this is race data.
"""

import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "Pit_Dashboard"))

import db  # noqa: E402  (needs the path set up first)

# Column -> the car_data block it arrives in. The key inside the block is the
# column name itself, which is why db.flatten_record can stay this simple.
BACKFILL = {
    "mms_measured_voltage_V": "motor",
    "mms_current_A": "motor",
    "mms_vehicle_speed_kmh": "motor",
    "mms_trip_m": "motor",
    "mms_estimated_soc_percent": "motor",
    "regen_energy": "motor",
    "target_speed_kmh": "motor",
}


def backfill(conn, dry_run=False, batch=2000):
    """Fill NULL cells in BACKFILL columns from each row's raw_json.

    Returns {column: cells_written}."""
    missing = [c for c in BACKFILL if c not in
               {r[1] for r in conn.execute("PRAGMA table_info(telemetry)")}]
    if missing:
        raise SystemExit(f"telemetry.db has no column {missing} — run the "
                         f"dashboard once so db.init_db() migrates the schema.")

    # Only rows where at least one target column is still empty.
    where = " OR ".join(f"{c} IS NULL" for c in BACKFILL)
    rows = conn.execute(
        f"SELECT rtdb_key, raw_json, {', '.join(BACKFILL)} FROM telemetry "
        f"WHERE raw_json IS NOT NULL AND ({where})"
    ).fetchall()
    print(f"{len(rows):,} row(s) with at least one empty column")

    written = {c: 0 for c in BACKFILL}
    updates = []
    for row in rows:
        try:
            car = json.loads(row["raw_json"])
        except (ValueError, TypeError):
            continue                      # unparseable payload — skip, don't guess
        if not isinstance(car, dict):
            continue
        patch = {}
        for col, block_name in BACKFILL.items():
            if row[col] is not None:
                continue                  # already populated; never overwrite
            block = car.get(block_name)
            if not isinstance(block, dict):
                continue
            value = db._num(block.get(col))
            if value is not None:
                patch[col] = value
                written[col] += 1
        if patch:
            updates.append((patch, row["rtdb_key"]))

    print(f"{len(updates):,} row(s) would be updated" if dry_run
          else f"writing {len(updates):,} row(s)...")
    for col, n in sorted(written.items()):
        print(f"   {col:28} {n:7,} cell(s)")

    if dry_run or not updates:
        return written

    # One transaction: complete, or leave the file exactly as it was.
    with conn:
        for start in range(0, len(updates), batch):
            for patch, key in updates[start:start + batch]:
                sets = ", ".join(f"{c} = :{c}" for c in patch)
                conn.execute(f"UPDATE telemetry SET {sets} WHERE rtdb_key = :k",
                             {**patch, "k": key})
            print(f"   committed {min(start + batch, len(updates)):,}/{len(updates):,}")
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    ap.add_argument("--db", default=db.SQLITE_PATH, help="path to telemetry.db")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the automatic .bak copy (not recommended)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit(f"no database at {args.db}")

    if not args.dry_run and not args.no_backup:
        backup = f"{args.db}.{time.strftime('%Y%m%d_%H%M%S')}.bak"
        shutil.copy2(args.db, backup)
        print(f"backup: {backup}")

    conn = db.get_conn(args.db)
    try:
        backfill(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    print("dry run — nothing written" if args.dry_run else "done")


if __name__ == "__main__":
    main()
