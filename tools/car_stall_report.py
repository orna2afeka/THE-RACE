#!/usr/bin/env python3
"""
car_stall_report.py - how often the car's CAN thread froze, from the pit's data
================================================================================
    python tools/car_stall_report.py                    # most recent day in the store
    python tools/car_stall_report.py --date 2026-09-18
    python tools/car_stall_report.py --since 2026-09-15
    python tools/car_stall_report.py --db path/to/telemetry.db

Read-only (db.get_conn_ro). Safe beside a live collector and a live pit wall.

WHY THIS CAN BE JUDGED FROM THE LAPTOP. Every sample the car pushes carries
`device_ts`, stamped by the car at push time, and a health block (pi_uptime_s,
can_state, can_frames) that the CAN worker thread refreshes every 0.5 s
(SolarRace_OS/main.py: _publish_gps and _publish_heartbeat). Both are written
by the one thread that reads the CAN bus and -- on main -- performs the
Firebase upload synchronously.

So two consecutive rows whose device_ts are more than GAP_S apart, with
can_state live on both sides and pi_uptime_s NOT advanced between them, mean
that thread ran none of its loop in the interval. Not busy: blocked. A busy
thread would still have come round and refreshed the health block. On the
driver's screen that interval is a freeze followed by a jump. The only calls
on that thread that can block for seconds are the synchronous Firebase writes
(8 s timeout per attempt, retried inside firebase-admin) and bus.send timeouts
(at most 1.5 s per BMS poll), so a long block is the upload.

What is NOT counted as a block:
  * gaps while the bus is silent -- the heartbeat pushes every 5 s, so 5 s
    gaps are the design, not a fault;
  * a restart (pi_uptime_s went backwards -- it counts from the CAN worker's
    start, so a HUD relaunch, Ctrl+R or a reboot all reset it);
  * a break longer than BREAK_S -- the car was off, or the link was down for
    a whole stretch; that is an outage, not a stall.

Ingest latency (ingested_ts - device_ts) is printed as a hint only: it also
contains the pit collector's own catch-up time, so it says "the link was
struggling around then", not how long any one write blocked.

Measured on 2026-09-18 (practice, 08:27-13:38): 260 blocks, 9 % of live
time, 123 of them while driving, worst 19.3 s at 51 km/h, and the car's own
can_frames counter identical on both sides of every one. Branch pi-outbox
moves the upload to its own thread; re-run this after it is on the car and
the block count should be ~0.
"""

import argparse
import collections
import datetime as dt
import os
import sys

# The pit laptop's console is cp1252; the missing-value dash would crash the
# print. Same fix main.py applies to the HUD's own stdout.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "Pit_Dashboard"))

import db                                                        # noqa: E402

GAP_S = 1.5            # a push interval longer than this is a gap (nominal is 0.5 s)
BREAK_S = 60.0         # longer than this is an outage / session end, not a stall
HEALTH_FROZEN_S = 0.5  # pi_uptime_s advanced by less than this = the loop never ran
DRIVING_KMH = 3.0      # rows faster than this count as "while driving"
BUCKETS = ((1.5, 3), (3, 6), (6, 10), (10, 20), (20, 60))

COLUMNS = ("device_ts", "ingested_ts", "can_state", "can_frames",
           "pi_uptime_s", "mms_vehicle_speed_kmh")


def _day(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _hms(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _q(sorted_vals, q):
    """Quantile of an already-sorted list; None when empty (never 0)."""
    if not sorted_vals:
        return None
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * q))]


def _f(v, nd=2):
    return "—" if v is None else f"{v:.{nd}f}"


def classify(a, b):
    """What the gap between consecutive rows `a` and `b` is. None = no gap."""
    d = b["device_ts"] - a["device_ts"]
    if d <= GAP_S:
        return None
    if d > BREAK_S:
        return "break"
    ua, ub = a["pi_uptime_s"], b["pi_uptime_s"]
    if ua is not None and ub is not None and ub < ua:
        # pi_uptime_s counts from the CAN worker's construction (main.py
        # _boot_ts), so it goes backwards when the HUD or its worker restarts
        # -- a supervisor relaunch after a crash, Ctrl+R, or a real reboot.
        return "restart"
    live_a = a["can_state"] == "live"
    live_b = b["can_state"] == "live"
    if not live_a and not live_b:
        return "heartbeat"          # bus silent: 5 s cadence is expected
    if live_a != live_b:
        return "transition"         # bus went quiet or came back inside the gap
    if ua is None or ub is None:
        return "unexplained"        # a build that predates the health block
    if ub - ua < HEALTH_FROZEN_S:
        return "blocked"
    return "unexplained"


def load(conn, since, until):
    cols = ", ".join(COLUMNS)
    return conn.execute(
        f"SELECT {cols} FROM telemetry "
        "WHERE device_ts IS NOT NULL AND device_ts >= ? AND device_ts < ? "
        "ORDER BY device_ts",
        (since, until)).fetchall()


def pick_window(conn, args):
    """(since, until, label) in unix seconds, from --date / --since / default."""
    if args.date:
        d0 = dt.datetime.strptime(args.date, "%Y-%m-%d")
        return d0.timestamp(), (d0 + dt.timedelta(days=1)).timestamp(), args.date
    if args.since:
        d0 = dt.datetime.strptime(args.since, "%Y-%m-%d")
        return d0.timestamp(), 2 ** 40, f"since {args.since}"
    row = conn.execute("SELECT MAX(device_ts) FROM telemetry").fetchone()
    if not row or row[0] is None:
        return None, None, None
    d0 = dt.datetime.strptime(_day(row[0]), "%Y-%m-%d")
    return d0.timestamp(), (d0 + dt.timedelta(days=1)).timestamp(), _day(row[0])


def _latency(r):
    if r["ingested_ts"] is None or r["device_ts"] is None:
        return None
    v = r["ingested_ts"] - r["device_ts"]
    return v if v >= 0 else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1].strip())
    ap.add_argument("--db", default=db.SQLITE_PATH, help="pit telemetry.db (default: the pit's own)")
    ap.add_argument("--date", help="one day, YYYY-MM-DD (default: the most recent day stored)")
    ap.add_argument("--since", help="from this day on, YYYY-MM-DD")
    args = ap.parse_args()

    conn, mode = db.get_conn_ro(args.db)
    print(f"store: {args.db}  (opened {mode})")
    since, until, label = pick_window(conn, args)
    if since is None:
        print("no telemetry with a device_ts in this store")
        return 1
    rows = load(conn, since, until)
    print(f"window: {label}   rows: {len(rows)}")
    if len(rows) < 2:
        print("not enough rows to measure intervals")
        return 1
    print(f"span: {_hms(rows[0]['device_ts'])} – {_hms(rows[-1]['device_ts'])}  "
          f"({_day(rows[0]['device_ts'])})")

    kinds = collections.Counter()
    blocks = []                    # (a, b, seconds)
    live_s = 0.0
    driving_iv = []                # push intervals while live and driving
    fps = []                       # CAN frames/s the Pi itself counted
    for a, b in zip(rows, rows[1:]):
        d = b["device_ts"] - a["device_ts"]
        if a["can_state"] == "live" and d <= BREAK_S:
            live_s += d
            spd = a["mms_vehicle_speed_kmh"]
            if spd is not None and spd > DRIVING_KMH:
                driving_iv.append(d)
            if a["pi_uptime_s"] is not None and b["pi_uptime_s"] is not None \
                    and a["can_frames"] is not None and b["can_frames"] is not None:
                du = b["pi_uptime_s"] - a["pi_uptime_s"]
                df = b["can_frames"] - a["can_frames"]
                if 0.4 < du < 2 and df > 0:
                    fps.append(df / du)
        k = classify(a, b)
        if k is None:
            continue
        kinds[k] += 1
        if k == "blocked":
            blocks.append((a, b, d))

    live_rows = sum(1 for r in rows if r["can_state"] == "live")
    print(f"\nlive rows: {live_rows}   live seconds: {live_s:.0f}   "
          f"gaps > {GAP_S} s: {sum(kinds.values())}")
    legend = {
        "blocked":     "bus live, health never refreshed: the CAN thread was blocked",
        "heartbeat":   "bus silent, 5 s heartbeat cadence: expected",
        "transition":  "bus went quiet or came back inside the gap",
        "restart":     "pi_uptime_s went backwards: HUD or CAN worker restarted",
        "unexplained": "bus live but health did advance, or no health block",
        "break":       f"longer than {BREAK_S:.0f} s: car off or link down",
    }
    for k in ("blocked", "heartbeat", "transition", "restart", "unexplained", "break"):
        if kinds.get(k):
            print(f"  {k:<12} {kinds[k]:>4}   {legend[k]}")

    print("\n== CAN thread blocked (bus live on both sides, health block never refreshed) ==")
    if not blocks:
        print("none — no freeze of the kind this tool detects in this window")
    else:
        blocked_s = sum(d for _, _, d in blocks)
        pct = (100.0 * blocked_s / live_s) if live_s else None
        print(f"blocks: {len(blocks)}   blocked seconds: {blocked_s:.0f}   "
              f"of live time: {_f(pct, 1)} %")
        hist = collections.Counter()
        for _, _, d in blocks:
            for lo, hi in BUCKETS:
                if d <= hi:
                    hist[(lo, hi)] += 1
                    break
        print("  by length (s): " + "   ".join(
            f"{lo}-{hi}: {hist.get((lo, hi), 0)}" for lo, hi in BUCKETS))
        by_hour = collections.Counter(dt.datetime.fromtimestamp(a["device_ts"]).strftime("%H")
                                      for a, _, _ in blocks)
        print("  by hour:       " + "   ".join(f"{h}h: {n}" for h, n in sorted(by_hour.items())))
        drive_blocks = [x for x in blocks if (x[0]["mms_vehicle_speed_kmh"] or 0) > DRIVING_KMH]
        print(f"  while driving (> {DRIVING_KMH:.0f} km/h): {len(drive_blocks)}")
        print("  longest:")
        for a, b, d in sorted(blocks, key=lambda x: -x[2])[:10]:
            spd = a["mms_vehicle_speed_kmh"]
            print(f"    {_hms(a['device_ts'])}  {d:5.1f} s   speed {_f(spd, 0)} km/h   "
                  f"can_frames {a['can_frames']} -> {b['can_frames']}")

    print("\n== push interval while live and driving ==")
    if not driving_iv:
        print("no driving rows in this window")
    else:
        s = sorted(driving_iv)
        print(f"n {len(s)}   median {_f(_q(s, 0.5), 3)}   p95 {_f(_q(s, 0.95))}   "
              f"p99 {_f(_q(s, 0.99))}   max {_f(s[-1], 1)}   "
              f"(nominal 0.5 s; > {GAP_S} s: {sum(1 for x in s if x > GAP_S)})")

    print("\n== CAN load the Pi reported (can_frames per second of pi_uptime) ==")
    if fps:
        s = sorted(fps)
        print(f"n {len(s)}   median {_f(_q(s, 0.5), 0)}   p10 {_f(_q(s, 0.1), 0)}   "
              f"p90 {_f(_q(s, 0.9), 0)}   max {_f(s[-1], 0)} frames/s")
    else:
        print("no usable health rows")

    print("\n== ingest latency, ingested_ts - device_ts (hint only: includes the pit's own catch-up) ==")
    all_l = sorted(v for v in (_latency(r) for r in rows) if v is not None)
    after = sorted(v for v in (_latency(b) for _, b, _ in blocks) if v is not None)
    print(f"all rows:            median {_f(_q(all_l, 0.5))} s   p90 {_f(_q(all_l, 0.9))} s   "
          f"max {_f(all_l[-1] if all_l else None, 0)} s")
    print(f"row after a block:   median {_f(_q(after, 0.5))} s   p90 {_f(_q(after, 0.9))} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
