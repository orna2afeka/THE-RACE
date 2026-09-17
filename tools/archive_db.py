#!/usr/bin/env python3
"""
archive_db.py - retire the telemetry store so the next session starts empty
===========================================================================
    python tools/archive_db.py --label practice
    python tools/archive_db.py --label prerace
    python tools/archive_db.py --dry-run          # say what would happen, touch nothing

Moves Pit_Dashboard/telemetry.db into a folder of its own under
Pit_Dashboard/archive/ and leaves a fresh, empty, correctly-schemad database in
its place. Run it with the collector and the dashboard STOPPED.

WHY A RENAME AND NOT "RESET HISTORY". The dashboard's Reset History button runs
a DELETE. That frees pages INSIDE the file without shrinking it: a 355 MB store
is still a 355 MB store afterwards, every index keeps its depth, and every query
keeps paying for a race that is already over. Retiring the file is the only
thing that actually hands the next session a small database.

WHY A FOLDER PER SESSION, rather than the flat telemetry.db.<date>.<label>.bak
names this used to leave in Pit_Dashboard/. Those sorted into one
undifferentiated pile, carried an extension nothing will open, and stranded
their -wal/-shm sidecars beside them. One folder per session keeps the database
together with the manifest.txt that says what is in it, and lets .gitignore
exclude the lot in one line.

WHAT IS DELIBERATELY LOST. app_state travels with the old file, so the race
clock, the driver/stint counter and last_known all start blank. That is the
point of a fresh start; it is also why this refuses to run while the race clock
is going, where losing it would be unrecoverable.

WHAT MUST NOT BE CARRIED FORWARD: stream_cursor. Seeding it into the new
database would send the collector down the catch-up path at next start and pull
back every sample just archived. The new file is left genuinely empty so the
collector takes the INITIAL_BACKFILL_LIMIT branch instead and goes live-now.
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "Pit_Dashboard"))

import db                                                        # noqa: E402
from pit_config import SQLITE_PATH, DEVICE_ID                    # noqa: E402

ARCHIVE_DIR = os.path.join(os.path.dirname(SQLITE_PATH), "archive")
SIDECARS = ("-wal", "-shm")


def _mb(n):
    return "%.1f MB" % (n / 1048576.0)


def _shown(path):
    """Repo-relative where that is actually shorter, absolute otherwise."""
    rel = os.path.relpath(path, _ROOT)
    return path if rel.startswith("..") else rel


def drop_sidecars(path):
    """Remove the -wal/-shm beside `path`, which by here hold nothing.

    Merely OPENING a WAL database creates its -shm, so the read-back that
    verifies an archive leaves a fresh pair beside the file it just verified --
    reintroducing the stray sidecars this tool exists to avoid. Safe only
    because the WAL was TRUNCATE-checkpointed first, so everything is in the
    database itself and the sidecars are rebuilt on demand by whoever opens it
    next."""
    for ext in SIDECARS:
        stale = path + ext
        if os.path.exists(stale):
            os.remove(stale)


def _stamp(ts):
    """A device_ts as a readable local time, or an em dash when there is none.

    Missing is missing: an absent timestamp is never rendered as an epoch or as
    a zero, in the manifest any more than on the dashboard."""
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def survey(path):
    """Row count, time span and app_state of a store, read-only.

    Opened through get_conn_ro so that surveying a database never modifies it --
    including under --dry-run, which must leave the file exactly as it found
    it."""
    conn, _mode = db.get_conn_ro(path)
    try:
        rows = db.count_samples(conn, DEVICE_ID)
        first, last = db.time_bounds(conn, DEVICE_ID)
        state = {r["key"]: r["value"]
                 for r in conn.execute("SELECT key, value FROM app_state")}
        return {"rows": rows, "first": first, "last": last, "state": state}
    finally:
        conn.close()


def racing(state):
    """True only if the race clock is actually running in the old store."""
    try:
        return bool(json.loads(state.get("race") or "{}").get("is_racing"))
    except (ValueError, AttributeError):
        return False


def checkpoint(path):
    """Fold the WAL back into the database so the archived file stands alone.

    An archive is a single .db that any SQLite browser can open. Without this
    the newest samples live only in telemetry.db-wal, and moving the .db by
    itself would silently leave them behind -- which is how the stray .bak-wal
    files in Pit_Dashboard came to exist. Returns the wal_checkpoint row; a
    leading 1 means something else still holds the file open."""
    conn = sqlite3.connect(path, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_size_limit=33554432;")
        return conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.close()


def assert_unlocked(path):
    """Fail loudly NOW if a collector or dashboard still holds the database.

    Renaming an open SQLite file is refused outright on Windows, and on POSIX it
    succeeds while leaving the running collector writing into a file that no
    longer has a name. Neither is a thing to discover halfway through, so prove
    an exclusive lock is available before anything moves."""
    conn = sqlite3.connect(path, timeout=5.0)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        raise SystemExit(
            "\n  [X] %s is in use (%s).\n"
            "      Close the Pit Web, Pit Collector, pit wall and profile builder\n"
            "      windows, then run this again. Nothing has been changed.\n"
            % (os.path.basename(path), exc)
        )
    finally:
        conn.close()


def manifest(info, archived_name, label, checkpoint_row):
    span = ""
    if info["first"] and info["last"]:
        span = "  (%.1f h)" % ((info["last"] - info["first"]) / 3600.0)
    lines = [
        "Solar race telemetry archive",
        "=" * 62,
        "file          : %s" % archived_name,
        "archived at   : %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "label         : %s" % (label or "—"),
        "device_id     : %s" % DEVICE_ID,
        "",
        "samples       : %d" % info["rows"],
        "first sample  : %s" % _stamp(info["first"]),
        "last sample   : %s%s" % (_stamp(info["last"]), span),
        "wal_checkpoint: %s" % (checkpoint_row,),
        "",
        "A complete, self-contained SQLite database: the WAL was folded in before",
        "it was moved, so there is no -wal or -shm that belongs with it. Open it",
        "directly, or point a tool at it:",
        "",
        "    python tools/replay_limits.py --db <this file>",
        "",
        "app_state as it stood in this file (the live store restarted without it):",
    ]
    for key in sorted(info["state"]):
        value = info["state"][key]
        if len(value) > 300:
            value = value[:300] + " ..."
        lines.append("  %-32s %s" % (key, value))
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(
        description="Archive telemetry.db and start the next session empty.")
    ap.add_argument("--label", default="",
                    help="what this session was, e.g. practice or prerace. "
                         "Becomes part of the folder and file name.")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would happen and change nothing.")
    ap.add_argument("--force", action="store_true",
                    help="archive even with the race clock running. Loses it.")
    args = ap.parse_args()

    label = re.sub(r"[^A-Za-z0-9_-]+", "-", args.label).strip("-").lower()

    print()
    print("  Telemetry archive")
    print("  " + "-" * 62)
    print("  store : %s" % SQLITE_PATH)

    if not os.path.exists(SQLITE_PATH):
        print("  There is no telemetry.db here - nothing to archive.")
        if not args.dry_run:
            conn = db.get_conn(SQLITE_PATH)
            db.init_db(conn)
            conn.close()
            print("  Created an empty one. The next start is already fresh.")
        return 0

    live_bytes = os.path.getsize(SQLITE_PATH)
    for ext in SIDECARS:
        if os.path.exists(SQLITE_PATH + ext):
            live_bytes += os.path.getsize(SQLITE_PATH + ext)

    assert_unlocked(SQLITE_PATH)
    info = survey(SQLITE_PATH)

    print("  size  : %s   samples: %d" % (_mb(live_bytes), info["rows"]))
    print("  span  : %s  ->  %s" % (_stamp(info["first"]), _stamp(info["last"])))

    if racing(info["state"]) and not args.force:
        raise SystemExit(
            "\n  [X] The race clock is RUNNING in this store.\n"
            "      It lives in app_state inside this same file, so archiving now\n"
            "      loses the race start time and it cannot be recovered.\n"
            "      Archive before the clock starts, or pass --force if you are\n"
            "      certain. Nothing has been changed.\n")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = stamp + ("_" + label if label else "")
    dest_dir = os.path.join(ARCHIVE_DIR, folder)
    archived_name = "telemetry_%s.db" % folder
    dest = os.path.join(dest_dir, archived_name)

    print()
    print("  archive to : %s" % _shown(dest))
    print("  then       : a new empty telemetry.db, collector goes live-now")

    if args.dry_run:
        print()
        print("  --dry-run: nothing was changed.")
        return 0

    row = checkpoint(SQLITE_PATH)
    print()
    print("  wal_checkpoint(TRUNCATE) -> %s" % (row,))
    if row and row[0]:
        raise SystemExit(
            "  [X] The WAL would not checkpoint: something still holds the file.\n"
            "      Nothing has been moved.\n")

    os.makedirs(dest_dir, exist_ok=True)
    shutil.move(SQLITE_PATH, dest)

    # These describe a database that is no longer at this path. A stale -shm
    # left where the NEW file is about to be created is actively harmful:
    # SQLite would read it as belonging to that file.
    drop_sidecars(SQLITE_PATH)

    # Prove the archive is readable and complete BEFORE the empty store replaces
    # it as the one the pit will use.
    check = survey(dest)
    if check["rows"] != info["rows"]:
        raise SystemExit(
            "  [X] Archived copy holds %d samples, expected %d. The original is\n"
            "      at %s - do not start the collector until this is understood.\n"
            % (check["rows"], info["rows"], dest))
    archived_bytes = os.path.getsize(dest)
    drop_sidecars(dest)

    with open(os.path.join(dest_dir, "manifest.txt"), "w", encoding="utf-8") as fh:
        fh.write(manifest(info, archived_name, label, row))

    conn = db.get_conn(SQLITE_PATH)
    db.init_db(conn)
    conn.close()

    print("  archived   : %d samples, %s, verified"
          % (check["rows"], _mb(archived_bytes)))
    print("  new store  : %s, empty, schema ready"
          % _mb(os.path.getsize(SQLITE_PATH)))
    print()
    print("  Start the pit as usual. The collector finds no rows and no cursor,")
    print("  so it goes straight to the live tail - no history is pulled back.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
