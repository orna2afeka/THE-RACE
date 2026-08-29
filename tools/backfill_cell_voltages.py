"""
backfill_cell_voltages.py — pull individual cell voltages out of raw_json
==========================================================================
    python tools/backfill_cell_voltages.py [--db PATH] [--force]

db.py already stores every field the car ever reports inside raw_json, so
nothing was ever actually LOST — but bms_string_count and the 30
bms_cell_NN_V fields only became real, queryable columns once
Pit_Dashboard/db.py's METRIC_COLUMNS grew to include them. Rows ingested
before that change have those columns NULL even though the data is sitting
right there in their own raw_json blob. This is a one-time migration that
fills them in from history, so past races are not missing the very data this
script exists to surface.

WHY json_extract() AND NOT A PYTHON LOOP
The obvious approach — SELECT raw_json, json.loads() each row, UPDATE with
the extracted values — works, but a per-row Python round trip over a
66,000-row table is exactly the kind of "full table, once per launch" cost
this codebase has already been bitten by twice this project (see db.py's
init_db() migration note, and the fetch_lap_track CAST-defeats-the-index
story). SQLite's own json_extract() does the same extraction inside the
database engine, in ONE UPDATE statement: 66,465 rows in 2.3 seconds, versus
tens of seconds to minutes parsing JSON one row at a time in Python.
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
_PIT = os.path.join(_REPO, "Pit_Dashboard")
if _PIT not in sys.path:
    sys.path.insert(0, _PIT)

import db  # noqa: E402  (path set up immediately above; also gives us BMS_CELL_COLUMN_COUNT)

MIGRATION_KEY = "cell_voltage_backfill"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join(_PIT, "telemetry.db"))
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

    # Ensure the target columns exist before trying to fill them — a DB that
    # has never been opened by the current db.py won't have them yet.
    db.init_db(conn)

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
    backup_path = f"{db_path}.{stamp}.cellvoltagebackfill.bak"
    conn.close()
    shutil.copy2(db_path, backup_path)
    for ext in ("-wal", "-shm"):
        side = db_path + ext
        if os.path.exists(side):
            shutil.copy2(side, backup_path + ext)
    print(f"backup written: {backup_path}")

    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")

    before_populated = conn.execute(
        "SELECT COUNT(*) AS n FROM telemetry WHERE bms_cell_01_V IS NOT NULL"
    ).fetchone()["n"]

    cell_sets = ",\n        ".join(
        f"bms_cell_{i:02d}_V = json_extract(raw_json, '$.battery.bms_cell_{i:02d}_V')"
        for i in range(1, db.BMS_CELL_COLUMN_COUNT + 1))

    with conn:
        cur = conn.execute(f"""
            UPDATE telemetry SET
                bms_string_count = json_extract(raw_json, '$.battery.bms_string_count'),
                {cell_sets}
            WHERE raw_json IS NOT NULL
        """)
        rows_touched = cur.rowcount
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (MIGRATION_KEY, json.dumps({
                "applied_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "rows_touched": rows_touched,
                "cell_columns": db.BMS_CELL_COLUMN_COUNT,
                "backup": os.path.basename(backup_path),
            })),
        )

    after_populated = conn.execute(
        "SELECT COUNT(*) AS n FROM telemetry WHERE bms_cell_01_V IS NOT NULL"
    ).fetchone()["n"]
    distinct_counts = [r["bms_string_count"] for r in conn.execute(
        "SELECT DISTINCT bms_string_count FROM telemetry "
        "WHERE bms_string_count IS NOT NULL ORDER BY bms_string_count")]
    conn.close()

    print(f"rows with raw_json touched: {rows_touched}")
    print(f"rows with cell 1 populated: {before_populated} -> {after_populated}")
    print(f"distinct bms_string_count values seen across history: {distinct_counts}")


if __name__ == "__main__":
    main()
