#!/usr/bin/env python3
"""
check_public_estimate.py - the spectator page's estimate starts and ends honestly
=================================================================================
    python tools/check_public_estimate.py

Calls the pit's estimate endpoints against a throwaway store with the write to
Firebase stubbed, and reads the generated spectator page. No network, no car.

WHAT THIS IS. When the car is out of contact the public page used to sit on a
frozen marker, which reads as a stopped car to the family it is for. The pit
can now ask the page to show where the car SHOULD be -- a lap and a place, walked
round the team's 4:40 profile from the instant of the press.

It is a moving car on a PUBLIC page that the car did not send, so what must
stay true is mostly about honesty, not arithmetic:

  BY COMMAND  nothing estimates until the pit presses Start, with a lap and a
              position it typed. No field has a default.
  NOT LIVE    it is refused while the car is being heard. The page would not
              show it anyway; a press that seems to do nothing is worse than
              one that says why.
  ENDS        the first fresh sample clears it, in the pit's store and on the
              public node. An anchor typed at 14:00 must not come back to life
              at the next dropout and move the marker and the lap count to
              somewhere nobody chose.
  BACKLOG     a collector paging through hour-old samples is NOT the car being
              heard, and must not end an estimate.
  ONE NODE    it rides on /public/driver, which the page already polls; the
              database's egress is metered per viewer per poll.
  MARKED      the page marks the lap and the speed with "~", says ESTIMATED in
              words, and only consults the estimate while the car is silent.
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
    print("  %-50s %s" % (label, "OK  " if ok else "FAIL"))
    if detail:
        print("       " + detail)
    if not ok:
        FAILED.append("%s - %s" % (label, detail))
    return ok


def main():
    import db
    store = os.path.join(tempfile.mkdtemp(prefix="estimate_"), "t.db")
    os.environ["SOLARRACE_DB_PATH"] = store
    conn = db.get_conn(store)
    db.init_db(conn)

    def sample(age_s, lap=45, dist=1200.0):
        """One telemetry row whose car clock reads `age_s` ago."""
        ts = time.time() - age_s
        conn.execute(
            "INSERT INTO telemetry (device_id, rtdb_key, device_ts, ingested_ts,"
            " calculated_lap, lap_distance_m) VALUES (?,?,?,?,?,?)",
            ("solarcar", "-K%012d" % int(ts * 1000), ts, time.time(), lap, dist))
        conn.commit()

    from fastapi.testclient import TestClient
    from Pit_Web import api
    import driver_message

    published = []
    driver_message.publish_driver_name = (
        lambda name, changing_since=None, estimate=None, note=None:
        published.append(estimate))
    api.PUBLIC_DRIVER_ENABLED = True
    c = TestClient(api.app)

    print("\nthe spectator estimate: started by the pit, ended by the car\n")

    sample(age_s=1800)                                 # the car: 30 min silent
    r = c.get("/api/public/estimate").json()
    check("BY COMMAND: nothing is estimated until asked",
          r["active"] is False and r["estimate"] is None,
          "active=%s" % r["active"])
    check("        the fields are prefilled from the car's last word",
          r["prefill"] == {"lap": 46, "distM": 1200.0},
          "prefill %s (lap 45 completed, so it is driving lap 46)" % r["prefill"])

    check("        a lap is required", c.post("/api/public/estimate",
          json={"distM": 100}).status_code == 422, "422 with no lap")
    check("        a place off the track is refused", c.post(
          "/api/public/estimate", json={"lap": 46, "distM": 9000}
          ).status_code == 400, "400 for 9000 m")

    before = time.time()
    r = c.post("/api/public/estimate", json={"lap": 46, "distM": 1500})
    est = r.json().get("estimate") or {}
    check("a press starts it, from NOW, where the pit said",
          r.status_code == 200 and est.get("lap") == 46
          and est.get("distM") == 1500 and abs(est["startedAt"] - before) < 2,
          "lap %s at %s m" % (est.get("lap"), est.get("distM")))

    api.sync_public_driver()
    check("ONE NODE: it is published on /public/driver",
          bool(published) and published[-1] is not None
          and published[-1]["lap"] == 46,
          "publish_driver_name(estimate=%s)" % (published[-1],))

    # BACKLOG: an hour-old sample arriving now is not the car being heard.
    sample(age_s=3000, lap=44)
    api.sync_public_driver()
    check("BACKLOG: old samples arriving do not end it",
          c.get("/api/public/estimate").json()["active"] is True,
          "a 50-minute-old row was ingested; still estimating")

    # ENDS: a current sample does.
    sample(age_s=1, lap=47, dist=300.0)
    api.sync_public_driver()
    r = c.get("/api/public/estimate").json()
    check("ENDS: the first fresh sample clears it",
          r["active"] is False, "active=%s, car %.0f s ago" % (r["active"], r["carAgeS"]))
    check("        on the public node too",
          published[-1] is None, "last publish carried estimate=%s" % (published[-1],))

    r = c.post("/api/public/estimate", json={"lap": 47, "distM": 300})
    check("NOT LIVE: refused while the car is being heard",
          r.status_code == 409, "%s %s" % (r.status_code, r.json().get("detail")))

    # Stop, by hand.
    conn.execute("DELETE FROM telemetry")
    conn.commit()
    sample(age_s=900)
    c.post("/api/public/estimate", json={"lap": 47, "distM": 0})
    c.post("/api/public/estimate/stop")
    api.sync_public_driver()
    check("Stop ends it by hand",
          c.get("/api/public/estimate").json()["active"] is False
          and published[-1] is None, "cleared and republished")

    api.PUBLIC_DRIVER_ENABLED = False
    r = c.post("/api/public/estimate", json={"lap": 1, "distM": 0})
    check("a demo dashboard cannot publish one",
          r.status_code == 409, "%s" % r.json().get("detail"))
    conn.close()

    # MARKED: read off the page that is actually published.
    page = open(os.path.join(_ROOT, "docs", "index.html"), encoding="utf-8").read()
    check("MARKED: the page says ESTIMATED in words",
          "ESTIMATED at race pace" in page, "in the status line")
    check("        lap and speed carry a ~",
          '"~" + est.lap' in page and '"~" + Math.round(est.speed)' in page,
          "both figures the estimate produces")
    check("        and it is consulted only while the car is silent",
          "(stale || old) ? estNow(now) : null" in page
          and "(!fresh || isOld(nowS)) ? estNow(nowS) : null" in page,
          "render() and frame() both gate on silence")
    check("        it reads no node of its own",
          "public/estimate" not in page, "rides on the driver node")

    if FAILED:
        print("\n%d FAILED:" % len(FAILED))
        for f in FAILED:
            print("  - %s" % f)
        return 1
    print("\nAll spectator-estimate checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
