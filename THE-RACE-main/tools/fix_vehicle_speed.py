"""
fix_vehicle_speed.py - correct historical mms_vehicle_speed_kmh values
======================================================================
    python tools/fix_vehicle_speed.py --dry-run   # report only, touch nothing
    python tools/fix_vehicle_speed.py             # write the corrected values

WHY THIS EXISTS
The car used to store 0x610 bytes 4-5 verbatim under a column headed "km/h".
That field is not km/h: it is 0.1 km/h AND it is not gear-corrected, because the
controller's gear ratio was never configured so it computes speed as though the
motor drove the wheel directly. The raw number is therefore ~50.9x too large,
which is how telemetry.db came to hold 4,282 rows above 200 km/h and a peak of
6583 for a car that never exceeded about 130.

mms_parser.decode_vehicle_speed_kmh() now applies both corrections on the car.
This script applies the same function - imported, not re-implemented, so the
history can never be corrected by a different formula than the live path uses -
to rows that were written before that fix.

WHAT IT WILL NOT DO
Correct a row twice. That is the whole risk here: 12,062 rows already hold a
value below 200, so "looks plausible" proves nothing about whether the fix has
run. Two independent guards instead:

  1. A watermark in app_state (KEY below) records the highest rowid corrected.
     A plain re-run refuses rather than sweeping the table again.
  2. Every row is checked against its own raw_json, which keeps the payload the
     car actually sent and is never rewritten. A row is corrected ONLY if its
     column still equals that original value, i.e. it is demonstrably untouched.
     Anything else is left alone and counted as "skipped".

AFTER YOU DEPLOY THE CAR FIX
Until SolarRace_OS ships to the Pi, the collector keeps writing raw values, so
rows added between this run and that deployment still need correcting. Re-run
with the rowid of the last raw row:

    python tools/fix_vehicle_speed.py --through-rowid 41500

Guard 2 still applies, so naming too high a rowid cannot corrupt rows the car
sent correctly - they no longer match raw_json's untouched signature only if
they were already converted, and rows the updated car wrote are skipped because
their column already equals a decoded value rather than a raw one.

  WARNING: That last point is the one thing to get right: run this BEFORE the car
  starts sending corrected values, or pass --through-rowid to stop short of
  them. The backup exists precisely because that ordering is easy to fumble.

SAFETY
  * Takes a WAL-safe backup first via SQLite's own backup API. A file copy is
    NOT safe here: telemetry.db carries a 40 MB+ write-ahead log and copying
    just the .db would silently lose everything still in it.
  * One transaction: it either completes or changes nothing.
  * --dry-run opens the database read-only.
  * Safe to run while the collector and dashboard are live (busy_timeout set),
    though a quiet moment is still kinder.
"""

import argparse
import json
import os
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
# The decode lives with the parser; import it rather than restating the maths.
sys.path.insert(0, os.path.join(_ROOT, "SolarRace_OS", "modules"))
sys.path.insert(0, _ROOT)

from mms_parser import decode_vehicle_speed_kmh  # noqa: E402

DEFAULT_DB = os.path.join(_ROOT, "Pit_Dashboard", "telemetry.db")
KEY = "vehicle_speed_decode_fix"
COL = "mms_vehicle_speed_kmh"


def _raw_from_json(blob):
    """The speed the car originally sent for this row, or None.

    raw_json is the untouched payload, so it is the only reliable evidence of
    what the column held before anything rewrote it.
    """
    if not blob:
        return None
    try:
        doc = json.loads(blob)
    except (ValueError, TypeError):
        return None
    # Payloads appear both bare ({"motor": {...}}) and wrapped in car_data.
    motor = (doc.get("car_data") or doc).get("motor")
    if not isinstance(motor, dict):
        return None
    return motor.get(COL)


def _marker(conn):
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (KEY,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return None


def backup(path):
    """Consistent copy including the write-ahead log."""
    dest = f"{path}.{time.strftime('%Y%m%d_%H%M%S')}.speedfix.bak"
    src = sqlite3.connect(path)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    return dest


def plan(conn, through):
    """Rows to correct, rows to skip, and why - without writing anything."""
    fix, skipped, no_evidence = [], 0, 0
    sql = (f"SELECT rowid, {COL}, raw_json FROM telemetry "
           f"WHERE {COL} IS NOT NULL AND rowid <= ? ORDER BY rowid")
    for rowid, value, blob in conn.execute(sql, (through,)):
        raw = _raw_from_json(blob)
        if raw is None:
            no_evidence += 1          # cannot prove it is untouched -> leave it
            continue
        # Untouched means the column still carries exactly what the car sent.
        if abs(float(value) - float(raw)) > 1e-6:
            skipped += 1              # already corrected, or edited by something
            continue
        fix.append((decode_vehicle_speed_kmh(raw), rowid))
    return fix, skipped, no_evidence


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change and exit without writing")
    ap.add_argument("--db", default=DEFAULT_DB, help="path to telemetry.db")
    ap.add_argument("--no-backup", action="store_true", help="skip the backup copy")
    ap.add_argument("--through-rowid", type=int, default=None,
                    help="highest rowid still holding RAW values; required to "
                         "re-run after the first pass")
    a = ap.parse_args()

    if not os.path.exists(a.db):
        sys.exit(f"no such database: {a.db}")

    uri = f"file:{a.db}?mode=ro" if a.dry_run else a.db
    conn = sqlite3.connect(uri, uri=a.dry_run, timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")

    done = _marker(conn)
    if done and a.through_rowid is None:
        print(f"Already applied {done.get('applied_at')}: "
              f"{done.get('rows_changed')} rows through rowid "
              f"{done.get('through_rowid')}.")
        print("Nothing to do. To correct raw rows added since, re-run with "
              "--through-rowid <last raw rowid>.")
        return

    top = conn.execute("SELECT MAX(rowid) FROM telemetry").fetchone()[0] or 0
    through = a.through_rowid if a.through_rowid is not None else top
    floor = done.get("through_rowid", 0) if done else 0
    if through <= floor:
        sys.exit(f"--through-rowid {through} is at or below the {floor} already "
                 f"corrected; nothing to do.")

    fix, skipped, no_evidence = plan(conn, through)

    print(f"database      : {a.db}")
    print(f"max rowid     : {top:,}")
    print(f"correcting    : rowid {floor + 1:,} .. {through:,}")
    print(f"rows to fix   : {len(fix):,}")
    print(f"skipped       : {skipped:,} (column no longer matches raw_json - "
          f"already corrected)")
    print(f"no evidence   : {no_evidence:,} (raw_json carries no speed; left alone)")
    if fix:
        before = [conn.execute(f"SELECT {COL} FROM telemetry WHERE rowid=?",
                               (r,)).fetchone()[0] for _, r in fix[:3]]
        print(f"sample        : {before} -> {[round(v, 2) for v, _ in fix[:3]]}")
        print(f"new max speed : {max(v for v, _ in fix):.2f} km/h")

    if a.dry_run:
        print("\n--dry-run: nothing written.")
        return
    if not fix:
        print("\nNothing to change.")
        return

    if not a.no_backup:
        print(f"\nbackup        : {backup(a.db)}")

    with conn:
        conn.executemany(f"UPDATE telemetry SET {COL} = ? WHERE rowid = ?", fix)
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (KEY, json.dumps({
                "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "through_rowid": through,
                "rows_changed": (done.get("rows_changed", 0) if done else 0) + len(fix),
                "note": "raw 0.1 km/h, gear-uncorrected -> km/h",
            })))
    print(f"\nOK: corrected {len(fix):,} rows through rowid {through:,}.")


if __name__ == "__main__":
    main()
