#!/usr/bin/env python3
"""
check_charge_clock.py - the charging clock starts, runs and stops for the right reasons
=======================================================================================
    python tools/check_charge_clock.py

Drives the pit's charging clock against a throwaway store. No car, no network.

Two limits ride on this: the crew's hour on the charger, and the REGULATION
that a fourth charge classifies the car behind everyone who made three. A clock
that misses a charge, or counts one that did not happen, is wrong about a rule
that decides the result -- so what is checked is WHEN it moves, not how it draws.

  AUTO      the car reporting is_charging starts it, with no press.
  SILENCE   a car that goes quiet mid-charge does NOT stop it. This is why the
            pit owns the clock: a car switched off on the charger says nothing.
  MOVING    the car heard moving again stops it, and records how long it ran.
  NOT ZERO  is_charging falling to 0 while the car stands still does NOT stop
            it -- a car switched back on reports 0 for the detector's confirm
            window before it reports anything else.
  BACKLOG   an old sample that says "charging" is history arriving late. It must
            not start a clock, and must not cost one of the three.
  ONCE      a clock already running is never restarted, by the car or by a press.
  COUNT     each charge counts once; discard gives it back; a new race is zero;
            and the pit can state the count, because a car switched off on the
            charger is a charge the store never saw.
  LATE      the hour runs from the PLUG, not from the press or from Pit Web
            starting: a press can be backdated, and an auto-start is dated from
            the car's first unbroken report of charging.
"""

import os
import sys
import tempfile
import time
import warnings

warnings.filterwarnings("ignore")

_ROOT = os.environ.get("SOLARRACE_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
for p in (_ROOT, os.path.join(_ROOT, "Pit_Dashboard")):
    if p not in sys.path:
        sys.path.insert(0, p)

FAILED = []


def check(label, ok, detail=""):
    print("  %-52s %s" % (label, "OK  " if ok else "FAIL"))
    if detail:
        print("       " + detail)
    if not ok:
        FAILED.append("%s - %s" % (label, detail))
    return ok


def main():
    import db
    store = os.path.join(tempfile.mkdtemp(prefix="charge_"), "t.db")
    os.environ["SOLARRACE_DB_PATH"] = store
    conn = db.get_conn(store)
    db.init_db(conn)
    db.save_race_state(conn, True, time.time() - 3600)
    k = [0]

    def sample(age_s=0.5, charging=None, speed=0.0):
        """One row the car 'sent' `age_s` ago."""
        ts = time.time() - age_s
        k[0] += 1
        conn.execute(
            "INSERT INTO telemetry (device_id, rtdb_key, device_ts, ingested_ts,"
            " is_charging, mms_vehicle_speed_kmh, calculated_lap) VALUES (?,?,?,?,?,?,?)",
            ("solarcar", "-K%012d%03d" % (int(ts * 1000), k[0]), ts, time.time(),
             charging, speed, 10))
        conn.commit()

    from contextlib import closing
    from fastapi.testclient import TestClient
    from Pit_Web import api
    api.nudge_live = lambda: None
    c = TestClient(api.app)

    def clock():
        with closing(api.ro_conn()) as ro:
            return api.charge_clock(ro)

    print("\nthe charging clock: an hour at most, three a race\n")

    check("it serves the crew's hour and the regulation's three",
          clock()["limitS"] == 3600.0 and clock()["maxStops"] == 3,
          "limit %.0f s, %d stops" % (clock()["limitS"], clock()["maxStops"]))

    sample(age_s=900, charging=1)                     # 15 minutes old
    api.charge_watch_once()
    check("BACKLOG: an old 'charging' sample starts nothing",
          clock()["active"] is False and clock()["count"] == 0,
          "active=%s count=%d" % (clock()["active"], clock()["count"]))

    sample(charging=1)
    api.charge_watch_once()
    st = clock()
    check("AUTO: the car reporting a charge starts the clock",
          st["active"] and st["startedBy"] == "car" and st["count"] == 1,
          "started by %s, charge %d of %d" % (st["startedBy"], st["count"], st["maxStops"]))
    started = st["startedAt"]

    api.charge_watch_once()
    r = c.post("/api/charge", json={"action": "start"}).json()
    check("ONCE: neither the car nor a press restarts it",
          clock()["startedAt"] == started and clock()["count"] == 1
          and r["changed"] is False, "same start, still charge 1")

    sample(charging=0, speed=0.0)
    api.charge_watch_once()
    check("NOT ZERO: is_charging=0 at a standstill does not stop it",
          clock()["active"] is True, "still running")

    conn.execute("UPDATE telemetry SET device_ts = device_ts - 1200")   # 20 min of silence
    conn.commit()
    api.charge_watch_once()
    check("SILENCE: a car gone quiet does not stop it",
          clock()["active"] is True and clock()["startedAt"] == started,
          "20 minutes with nothing heard; still counting")

    sample(charging=0, speed=32.0)
    api.charge_watch_once()
    st = clock()
    check("MOVING: the car heard moving ends the charge",
          st["active"] is False and st["lastDurationS"] is not None,
          "ran %.1f s, count stays %d" % (st["lastDurationS"] or -1, st["count"]))

    # By hand: the car switched off on the charger.
    conn.execute("UPDATE telemetry SET device_ts = device_ts - 600")
    conn.commit()
    c.post("/api/charge", json={"action": "start"})
    st = clock()
    check("a press starts it for a car that cannot say so",
          st["active"] and st["startedBy"] == "pit" and st["count"] == 2,
          "charge %d of %d, started by the %s" % (st["count"], st["maxStops"], st["startedBy"]))

    c.post("/api/charge", json={"action": "discard"})
    st = clock()
    check("COUNT: discard stops it AND gives the charge back",
          st["active"] is False and st["count"] == 1, "back to %d" % st["count"])

    c.post("/api/charge", json={"action": "start"})
    c.post("/api/charge", json={"action": "stop"})
    check("        a real one, stopped by hand, is kept",
          clock()["count"] == 2 and clock()["active"] is False,
          "charge %d recorded" % clock()["count"])

    check("        an unknown action is refused",
          c.post("/api/charge", json={"action": "pause"}).status_code == 400, "400")

    r = c.post("/api/charge", json={"action": "set_count", "count": 3})
    check("        the pit can state the count outright",
          clock()["count"] == 3, "set to %d" % clock()["count"])

    # LATE: a plug that went in before anyone reached the laptop.
    c.post("/api/charge", json={"action": "start", "minutesAgo": 25})
    ran = time.time() - clock()["startedAt"]
    check("LATE: a press can say the plug went in 25 min ago",
          abs(ran - 1500) < 3, "clock already reads %.0f s" % ran)
    c.post("/api/charge", json={"action": "discard"})

    # LATE, from the car: Pit Web restarted 9 minutes into a charge.
    conn.execute("DELETE FROM telemetry")
    for i in range(540, -1, -30):                     # charging for 9 minutes
        sample(age_s=i + 0.5, charging=1)
    api.charge_watch_once()
    ran = time.time() - clock()["startedAt"]
    check("      and an auto-start dates it from the car's first report",
          clock()["active"] and abs(ran - 540) < 5,
          "clock reads %.0f s for a charge the car has reported for 540 s" % ran)
    c.post("/api/charge", json={"action": "discard"})

    db.save_race_state(conn, True, time.time())        # a new race
    check("        a new race opens at zero",
          clock()["count"] == 0 and clock()["active"] is False,
          "count %d" % clock()["count"])

    live = c.get("/api/config").status_code            # the app still serves
    with closing(api.ro_conn()) as ro:
        payload = api.build_live(ro)
    check("the live payload carries it", "charge" in payload
          and payload["charge"]["maxStops"] == 3 and live == 200,
          "live.charge present")
    conn.close()

    if FAILED:
        print("\n%d FAILED:" % len(FAILED))
        for f in FAILED:
            print("  - %s" % f)
        return 1
    print("\nAll charging-clock checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
