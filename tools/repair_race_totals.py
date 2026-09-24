#!/usr/bin/env python3
"""
repair_race_totals.py - write back into the store what the car's totals lost
============================================================================
    python tools/repair_race_totals.py                # say what would change, touch nothing
    python tools/repair_race_totals.py --apply        # back the store up, then change it
    python tools/repair_race_totals.py --db COPY.db --apply --no-backup   # rehearse on a copy

When the Pi restarts without its lap checkpoint, total_race_energy,
regen_energy and odometer_m start again from zero (Zolder, 2026-09-19 21:44:
10154 Wh, 1010 Wh, 348.8 km). Pit_Dashboard/race_totals.py adds the loss back
wherever the pit SHOWS a total. This writes it into the stored rows, so the
History charts and the workbook are one continuous race as well.

WHAT IT CHANGES: those three columns, on rows after each loss, plus their
last_known entries. Every row gets exactly what race_totals.py would have
added to it -- the two read the store with the same function.

WHAT IT LEAVES: raw_json, which stays what the car said, and every per-lap
figure (last_lap_energy, stint_energy ...), which are differences and were
never wrong.

RUN IT AFTER THE CAR HAS STOPPED SENDING. It is safe to run twice and safe to
run early -- a car still publishing its short count leaves a fall at the seam,
the dashboard's live correction finds it, and a second run moves the seam to
the end -- but the lap in progress at the seam loses its energy baseline, and
the UPDATE holds the write lock against the collector while it runs. So it
refuses while the newest sample is fresh unless told --while-live.

The backup is an online SQLite backup (consistent with the collector running)
into Pit_Dashboard/archive/<stamp>_pre-totals-repair/, which is gitignored.
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIT = os.path.join(ROOT, "Pit_Dashboard")
for _p in (ROOT, PIT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import db  # noqa: E402
import race_totals as R  # noqa: E402

LIVE_WITHIN_S = 120.0


def _clock(ts):
    return datetime.fromtimestamp(ts).strftime("%d %H:%M:%S")


def plan(conn, race_start, device_id):
    """[(column, ts_from, ts_until | None, amount)] -- ranges are [from, until)."""
    rows = conn.execute(
        "SELECT device_ts, total_race_energy, odometer_m "
        "FROM telemetry INDEXED BY idx_telemetry_chart "
        "WHERE device_id = ? AND device_ts >= ? ORDER BY device_ts",
        (device_id, race_start)).fetchall()
    ts = [r[0] for r in rows]
    out = []
    for n, col in enumerate(R.RESET_FALL, start=1):
        falls, segments, _ = R.trace([r[n] for r in rows], R.RESET_FALL[col])
        amounts = {col: [f[2] for f in falls]}
        for follower, leader in R.FOLLOWS.items():
            if leader != col:
                continue
            amounts[follower] = []
            for before_i, after_i, _ in falls:
                was = conn.execute(
                    "SELECT %s FROM telemetry WHERE device_id = ? AND device_ts <= ? "
                    "AND device_ts >= ? AND %s IS NOT NULL "
                    "ORDER BY device_ts DESC LIMIT 1" % (follower, follower),
                    (device_id, ts[before_i], race_start)).fetchone()
                now = conn.execute(
                    "SELECT %s FROM telemetry WHERE device_id = ? AND device_ts >= ? "
                    "AND %s IS NOT NULL ORDER BY device_ts LIMIT 1"
                    % (follower, follower), (device_id, ts[after_i])).fetchone()
                lost = (was[0] - now[0]) if was and now else 0.0
                amounts[follower].append(max(lost, 0.0))
        for k, (start_i, in_force) in enumerate(segments):
            until = ts[segments[k + 1][0]] if k + 1 < len(segments) else None
            for name, per_fall in amounts.items():
                amount = sum(per_fall[f] for f in in_force)
                if amount > 0:
                    out.append((name, ts[start_i], until, amount))
    return out


def count(conn, device_id, col, ts_from, ts_until):
    sql = ("SELECT COUNT(*) FROM telemetry WHERE device_id = ? AND device_ts >= ? "
           "AND %s IS NOT NULL" % col)
    args = [device_id, ts_from]
    if ts_until is not None:
        sql, args = sql + " AND device_ts < ?", args + [ts_until]
    return conn.execute(sql, args).fetchone()[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[2])
    ap.add_argument("--db", default=os.path.join(PIT, "telemetry.db"))
    ap.add_argument("--device", default=db.DEVICE_ID)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-backup", action="store_true",
                    help="only for a copy you can afford to lose")
    ap.add_argument("--while-live", action="store_true",
                    help="run although the car is still sending")
    args = ap.parse_args()

    # A dry run cannot write even by accident: it opens the store read-only.
    if args.apply:
        conn = sqlite3.connect(args.db, timeout=60.0)
    else:
        conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"),
                               uri=True, timeout=60.0)
    conn.row_factory = sqlite3.Row
    race = db.load_race_state(conn)
    start = race.get("race_start_time")
    if not start:
        print("No race start in app_state: nothing to measure a loss from.")
        return 1
    print("store: %s\nrace started %s" % (args.db, _clock(start)))

    steps = plan(conn, start, args.device)
    if not steps:
        print("No total has fallen since the green flag. Nothing to repair.")
        return 0
    big = [s for s in steps if s[2] is None]
    print("\n%d range(s); the ones that run to the end of the store:" % len(steps))
    for col, a, _, amount in big:
        print("  %-18s +%-12.3f from %s  (%d rows)"
              % (col, amount, _clock(a), count(conn, args.device, col, a, None)))
    short = len(steps) - len(big)
    if short:
        print("  ...and %d short range(s) where the old and the new process's rows "
              "alternate at a restart" % short)

    newest = conn.execute("SELECT MAX(device_ts) FROM telemetry WHERE device_id = ?",
                          (args.device,)).fetchone()[0]
    age = time.time() - newest
    print("\nnewest sample: %.0f s ago" % age)
    if not args.apply:
        print("Dry run. --apply to write.")
        return 0
    if age < LIVE_WITHIN_S and not args.while_live:
        print("The car is still sending. Run this when it has stopped "
              "(or --while-live, see the header).")
        return 2

    if not args.no_backup:
        folder = os.path.join(PIT, "archive", "%s_pre-totals-repair"
                              % datetime.now().strftime("%Y-%m-%d_%H%M%S"))
        os.makedirs(folder)
        dest = os.path.join(folder, "telemetry.db")
        print("backing up to %s ..." % dest)
        with sqlite3.connect(dest) as copy:
            conn.backup(copy)
        copy.close()
        print("  %.0f MB" % (os.path.getsize(dest) / 1e6))

    t0 = time.time()
    with conn:                                   # one transaction: all or nothing
        conn.execute("BEGIN IMMEDIATE")
        for col, a, b, amount in steps:
            sql = ("UPDATE telemetry SET %s = %s + ? WHERE device_id = ? "
                   "AND device_ts >= ? AND %s IS NOT NULL" % (col, col, col))
            params = [amount, args.device, a]
            if b is not None:
                sql, params = sql + " AND device_ts < ?", params + [b]
            conn.execute(sql, params)
            if b is None:
                conn.execute(
                    "UPDATE last_known SET value_num = value_num + ? "
                    "WHERE device_id = ? AND metric = ? AND device_ts >= ?",
                    (amount, args.device, col, a))
    print("written in %.1f s" % (time.time() - t0))

    left = [s for s in plan(conn, start, args.device)]
    print("falls left in the store: %d%s" % (
        len(left), "" if not left else "  <-- the car sent more while this ran; "
                                        "run it again when it has stopped"))
    row = conn.execute(
        "SELECT total_race_energy, regen_energy, odometer_m FROM telemetry "
        "WHERE device_id = ? AND total_race_energy IS NOT NULL "
        "ORDER BY device_ts DESC LIMIT 1", (args.device,)).fetchone()
    print("newest row now: %.1f Wh, %.1f Wh regen, %.1f km"
          % (row[0], row[1], row[2] / 1000.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
