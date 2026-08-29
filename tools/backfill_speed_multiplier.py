"""
python tools/backfill_speed_multiplier.py [--db PATH] [--force]
"""

import argparse
import datetime
import json
import os
import shutil
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import drivetrain  # noqa: E402

# The value CONTROLLER_SPEED_DIVISOR held before this fix. Every row currently
# stored in mms_vehicle_speed_kmh was normalised at ingest (db._vehicle_speed)
# using this divisor (or the unrelated, unchanged CONTROLLER_SPEED_DIVISOR_LEGACY
# for pre-reconfiguration rows, then already reconciled onto the same scale by
# the prior vehicle_speed_decode_fix), so every stored value is already on one
# consistent "true km/h" scale tied to this number.
OLD_DIVISOR = 6.5455
NEW_DIVISOR = drivetrain.CONTROLLER_SPEED_DIVISOR
FACTOR = OLD_DIVISOR / NEW_DIVISOR

MIGRATION_KEY = "controller_speed_divisor_backfill"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(
        _REPO, "Pit_Dashboard", "telemetry.db"))
    ap.add_argument("--force", action="store_true",
                    help="re-apply even if this migration already ran")
    args = ap.parse_args()

    db_path = os.path.abspath(args.db)
    if not os.path.exists(db_path):
        sys.exit(f"no such file: {db_path}")

    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_state ("
        "  key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    prev = conn.execute(
        "SELECT value FROM app_state WHERE key = ?", (MIGRATION_KEY,)).fetchone()
    if prev is not None and not args.force:
        conn.close()
        sys.exit(f"already applied: {prev['value']}\n"
                 f"re-run with --force to apply again")

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.{stamp}.speeddivisorfix.bak"
    conn.close()
    shutil.copy2(db_path, backup_path)
    for ext in ("-wal", "-shm"):
        side = db_path + ext
        if os.path.exists(side):
            shutil.copy2(side, backup_path + ext)
    print(f"backup written: {backup_path}")
    print(f"factor = {OLD_DIVISOR} / {NEW_DIVISOR} = {FACTOR:.6f}")

    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")

    before = conn.execute(
        "SELECT COUNT(*) AS n FROM telemetry WHERE mms_vehicle_speed_kmh IS NOT NULL"
    ).fetchone()["n"]

    with conn:
        cur = conn.execute(
            "UPDATE telemetry SET mms_vehicle_speed_kmh = mms_vehicle_speed_kmh * ? "
            "WHERE mms_vehicle_speed_kmh IS NOT NULL",
            (FACTOR,),
        )
        rows_changed = cur.rowcount
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (MIGRATION_KEY, json.dumps({
                "applied_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "old_divisor": OLD_DIVISOR,
                "new_divisor": NEW_DIVISOR,
                "factor": FACTOR,
                "rows_changed": rows_changed,
                "note": ("mms_vehicle_speed_kmh *= OLD_DIVISOR/NEW_DIVISOR "
                        "to match the corrected CONTROLLER_SPEED_DIVISOR"),
                "backup": os.path.basename(backup_path),
            })),
        )

    after_max = conn.execute(
        "SELECT MAX(mms_vehicle_speed_kmh) AS m FROM telemetry").fetchone()["m"]
    conn.close()

    print(f"rows with a speed value: {before}")
    print(f"rows updated: {rows_changed}")
    print(f"max speed after: {after_max:.2f} km/h")


if __name__ == "__main__":
    main()
