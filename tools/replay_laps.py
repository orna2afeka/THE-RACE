#!/usr/bin/env python3
"""
replay_laps.py - run the car's lap tracker over telemetry the pit has stored
=============================================================================
The lap tracker runs on the Pi, where nobody can watch it think. This feeds it
the same GPS fixes, motor frames and TRIP counts the car published — read out
of the pit's own store, with the car's timestamps as the clock — and prints
what it would have decided next to what the car actually published.

    python tools/replay_laps.py                      # the whole store
    python tools/replay_laps.py --since 2026-09-19   # from a date (UTC)
    python tools/replay_laps.py --last-hours 3
    python tools/replay_laps.py --db path/to/telemetry.db --device car-1

Use it
  - after a session, before trusting a change to lap_tracker.py or the gate:
    the laps it counts should be the laps the officials counted
  - after surveying the gate (track.GATE_LEFT/RIGHT_LATLON): replay the
    morning's laps and see that every one is cut by "gps"
  - when the pit's lap count looks wrong: the event list says which passage was
    ignored or re-synced, and why

WHAT IT CANNOT SHOW. The store holds one row per telemetry upload (about 2 Hz),
the car samples GPS at 10 Hz and motor frames faster still, so replayed lap
times are good to about half a second, not the tenth the car achieves. Rows
lost to a dead link are lost here too. And the store opens read-only: this
never writes to it.
"""

import argparse
import datetime
import io
import json
import os
import sqlite3
import sys
from contextlib import redirect_stdout

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_REPO, os.path.join(_REPO, "SolarRace_OS", "modules")):
    if p not in sys.path:
        sys.path.insert(0, p)

import lap_tracker                                                # noqa: E402
import track                                                      # noqa: E402

DEFAULT_DB = os.path.join(_REPO, "Pit_Dashboard", "telemetry.db")

# A hole this long in the car's timestamps is a new session (the car was off,
# or the link was): the tracker is checkpointed and restored across it, which
# is exactly what the Pi does across a reboot.
SESSION_GAP_S = 300.0


def _utc(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%m-%d %H:%M:%S")


def _rows(conn, device, since_ts):
    sql = ("SELECT device_ts, lat, lon, gps_age_s, mms_rpm, mms_power_W, "
           "mms_trip_m, calculated_lap, lap_source, raw_json FROM telemetry "
           "WHERE device_ts >= ? ")
    args = [since_ts]
    if device:
        sql += "AND device_id = ? "
        args.append(device)
    return conn.execute(sql + "ORDER BY device_ts", args)


def _fix(lat, lon, gps_age_s, raw_json):
    """Rebuild the dict gps_reader.get_coordinates() gave the car."""
    if lat is None or lon is None:
        return None
    fix = {"lat": lat, "lon": lon, "fix_mode": 3, "stale": False,
           "fix_age_s": gps_age_s or 0.0}
    try:
        gps = (json.loads(raw_json) or {}).get("gps") or {} if raw_json else {}
    except (TypeError, ValueError):
        gps = {}
    for key in ("fix_mode", "stale", "speed_kmh", "fix_age_s"):
        if gps.get(key) is not None:
            fix[key] = gps[key]
    return fix


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--device", default=None, help="device_id (default: all rows)")
    ap.add_argument("--since", default=None, help="UTC date or datetime, ISO format")
    ap.add_argument("--last-hours", type=float, default=None)
    ap.add_argument("--quiet", action="store_true", help="laps only, no event list")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"no store at {args.db}")
        return 1
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    since_ts = 0.0
    if args.since:
        since_ts = datetime.datetime.fromisoformat(args.since).replace(
            tzinfo=datetime.timezone.utc).timestamp()
    if args.last_hours:
        newest = conn.execute("SELECT MAX(device_ts) FROM telemetry").fetchone()[0]
        since_ts = max(since_ts, (newest or 0.0) - args.last_hours * 3600.0)

    gate = "SURVEYED" if track.GATE_LEFT_LATLON else "derived from the map (not surveyed)"
    print(f"store  {args.db}")
    print(f"gate   {track.GATE_LEFT_M + track.GATE_RIGHT_M:.0f} m, {gate}\n")

    t = lap_tracker.LapTracker()
    t0 = prev_ts = None
    rows = fixes = 0
    stored_laps = []               # (ts, calculated_lap, lap_source) on change
    last_stored = None
    zone_s = {}
    last_zone = None
    chatter = io.StringIO()        # the tracker's own prints, shown as events

    def say(ts, text):
        if not args.quiet:
            print(f"  {_utc(ts)}  {text}")

    for (ts, lat, lon, age, rpm, power, trip, stored_lap, stored_src,
         raw) in _rows(conn, args.device, since_ts):
        rows += 1
        if t0 is None:
            t0 = ts
        if prev_ts is not None and ts - prev_ts > SESSION_GAP_S:
            say(ts, f"--- {(ts - prev_ts) / 60:.0f} min with no telemetry: "
                    f"checkpoint + restore, as on a reboot ---")
            saved = t.state_dict(now=prev_ts - t0)
            t = lap_tracker.LapTracker()
            t.restore(saved, now=ts - t0)
        now = ts - t0
        if prev_ts is not None:
            zone_s[t.zone] = zone_s.get(t.zone, 0.0) + min(ts - prev_ts, 5.0)
        prev_ts = ts

        before = t.lap_count
        with redirect_stdout(chatter):
            if rpm is not None:
                t.update_motion(rpm, now=now)
            if power is not None:
                t.update_energy(power, now=now)
            if trip is not None:
                t.update_odometer(trip, now=now)
            fix = _fix(lat, lon, age, raw)
            fixes += fix is not None
            event = t.update_gps(fix, now=now)

        if t.lap_count != before or event == "lap":
            time_s = "—" if t.last_lap_time_s is None else f"{t.last_lap_time_s:7.1f} s"
            say(ts, f"LAP {t.lap_count:3d}  {t.last_lap_kind:8s} {time_s}  "
                    f"{t.last_lap_distance_m:6.0f} m  {t.last_lap_energy_wh:7.1f} Wh  "
                    f"by {t.lap_source}"
                    f"{'  [' + ', '.join(t.last_lap_flags) + ']' if t.last_lap_flags else ''}")
        elif event:
            say(ts, f"{event}")
        for line in chatter.getvalue().splitlines():
            if "gate passed" in line or "TRIP jumped" in line:
                say(ts, line.split(" ", 1)[-1])
        chatter.seek(0)
        chatter.truncate()
        if t.zone != last_zone:
            say(ts, f"zone -> {t.zone}")
            last_zone = t.zone

        if stored_lap is not None and stored_lap != last_stored:
            stored_laps.append((ts, stored_lap, stored_src))
            last_stored = stored_lap

    if not rows:
        print("no rows in that range")
        return 1

    print(f"\n{rows} rows, {fixes} with a position, "
          f"{_utc(t0)} .. {_utc(prev_ts)} UTC")
    print(f"zones  " + ", ".join(f"{z or 'unknown'} {s / 60:.0f} min"
                                 for z, s in sorted(zone_s.items(), key=lambda kv: -kv[1])))
    print(f"\nreplayed   {t.lap_count} laps "
          f"({t.gps_lap_count} at the gate, {t.resyncs} re-syncs, "
          f"{t.rejected_crossings} passages ignored, "
          f"{t.backward_crossings} backward)")
    if stored_laps:
        first, last = stored_laps[0][1], stored_laps[-1][1]
        by = {}
        for _ts, _lap, src in stored_laps[1:]:
            by[src] = by.get(src, 0) + 1
        print(f"car said   lap {first:.0f} -> {last:.0f} over the same rows "
              f"({', '.join(f'{n} by {s}' for s, n in by.items()) or 'no lap cut'})")
    else:
        print("car said   nothing: no calculated_lap in these rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
