#!/usr/bin/env python3
"""
check_new_race.py - the green flag zeroes the car, and nothing else does
========================================================================
    python tools/check_new_race.py

Drives LapTracker.new_race() directly, round-trips the checkpoint the Pi
writes, and calls POST /api/race against a throwaway store with the send to
the car stubbed. No car, no network, no Firebase.

WHAT THIS IS FOR. The warm-up laps are not the race, but the car's lap
counter, odometer and energy totals have no way of knowing that, and its
checkpoint file carries them through a reboot -- so the only way to start
clean used to be to stop the car and delete lap_checkpoint.json by hand. The
pit now sends `new_race` with the green flag.

THE DANGEROUS HALF IS NOT THE RESET, IT IS WHEN IT FIRES. `new_race` throws
away recorded numbers and cannot be undone from the pit, so every one of these
must stay true:

  START     a race starting fires it exactly once.
  RESUME    Resume does NOT fire it. A mid-race browser refresh, a Stop and
            Resume around a red flag, a second Start of a race already
            running: none of them may wipe a lap count.
  CORRECT   correcting the start time ("the race began at 12:00, we got to the
            laptop at 12:20") does NOT fire it. That is the correction the pit
            is most likely to make while the car is out on track.
  SURVIVES  a car that cannot be reached does not stop the race starting, and
            the pit is TOLD the car kept its warm-up.
  ZEROED    the reset clears laps, distance, energy AND the finished-lap
            figures -- the last of these is what the other resets keep on
            purpose, and keeping them here would publish the warm-up's lap on
            every row of race lap 1.
  REBOOT    the checkpoint written afterwards restores the RACE, not the
            warm-up. This is the whole "stop deleting the JSON" claim.
  FLOOR     the pit's lap views start at the green flag, so a warm-up lap 1
            and a race lap 1 are not two laps with one number.
"""

import os
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")

_ROOT = os.environ.get("SOLARRACE_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
for p in (_ROOT, os.path.join(_ROOT, "SolarRace_OS"),
          os.path.join(_ROOT, "Pit_Dashboard")):
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


# --------------------------------------------------------------------------- #
# The car
# --------------------------------------------------------------------------- #
def driven_warm_up():
    """A tracker that has done three warm-up laps and is part way round a fourth."""
    from modules.lap_tracker import LapTracker
    t = LapTracker()
    t._have_distance = True
    t._have_energy = True
    for lap in range(3):
        t.odometer_m += 4000.0
        t.total_energy_wh += 120.0
        t.regen_energy_wh += 15.0
        t._trigger_lap("gate", now=100.0 * (lap + 1))
    t.odometer_m += 850.0                       # part way round the next one
    t.total_energy_wh += 26.0
    return t


def check_zeroed():
    t = driven_warm_up()
    before = (t.lap_count, t.odometer_m, t.total_energy_wh)
    check("the warm-up really counted something",
          before[0] == 3 and before[1] > 12000 and before[2] > 350,
          "lap %d, %.0f m, %.0f Wh before the flag" % before)

    t.new_race(now=500.0)
    snap = t.snapshot()

    zeroed = {"calculated_lap": 0, "lap_seq": 0, "odometer_m": 0.0,
              "lap_distance_m": 0.0, "total_race_energy": 0.0,
              "regen_energy": 0.0}
    wrong = {k: snap[k] for k, v in zeroed.items() if snap[k] != v}
    check("ZEROED: laps, distance and energy are back to 0",
          not wrong, str(wrong) if wrong else
          "lap 0, 0 m, 0 Wh, and lap_distance_m 0")

    carried = {k: snap[k] for k in
               ("last_lap_number", "last_lap_time_s", "last_lap_energy",
                "last_lap_distance_m", "last_lap_regen_energy",
                "last_lap_kind", "last_lap_flags", "last_lap_stopped_s")
               if snap[k] is not None}
    check("        and the finished lap is not carried into the race",
          not carried, str(carried) if carried else
          "every last_lap_* is null, so race lap 1 publishes no warm-up lap")

    check("        the car is looking for the line, not armed on it",
          t._armed is False and snap["lap_source"] is None,
          "armed=%s, the pit is shown lap_source=%r (the sentinel is not a "
          "trigger)" % (t._armed, snap["lap_source"]))
    check("        the lap clock restarts with the race",
          t.lap_started_ts is not None and t._lap_start_ts == 500.0,
          "lap datum at %.1f, wall clock set" % (t._lap_start_ts or -1))
    check("        distance measured from here is trusted again",
          t._distance_untrusted is False,
          "_distance_untrusted=%s" % t._distance_untrusted)
    return t


def check_reboot(t):
    """REBOOT: the checkpoint restores the race, not the warm-up."""
    from modules.lap_tracker import LapTracker
    state = t.state_dict(now=500.0)
    fresh = LapTracker()
    fresh.restore(state)
    snap = fresh.snapshot()
    fresh._have_distance = fresh._have_energy = True
    snap = fresh.snapshot()
    bad = {k: snap[k] for k, v in {"calculated_lap": 0, "lap_seq": 0,
                                   "odometer_m": 0.0,
                                   "total_race_energy": 0.0}.items()
           if snap[k] != v}
    check("REBOOT: the checkpoint brings back the race, not the warm-up",
          not bad, str(bad) if bad else
          "a Pi restarting after the flag comes up on lap 0 with 0 m and 0 Wh")


def check_command_accepted():
    from modules import lap_command
    check("the car accepts new_race as a command",
          "new_race" in lap_command.VALID_ACTIONS,
          "VALID_ACTIONS has %d actions" % len(lap_command.VALID_ACTIONS))
    # The age gate is the guard against a retained node firing it at boot.
    check("        and still refuses a stale one",
          lap_command.MAX_COMMAND_AGE_S <= 60,
          "a command older than %.0f s is adopted but not executed"
          % lap_command.MAX_COMMAND_AGE_S)


# --------------------------------------------------------------------------- #
# The pit: WHEN it fires
# --------------------------------------------------------------------------- #
def check_when_it_fires():
    import db
    tmp = tempfile.mkdtemp(prefix="newrace_")
    store = os.path.join(tmp, "race.db")
    conn = db.get_conn(store)
    db.init_db(conn)
    conn.close()
    os.environ["SOLARRACE_DB_PATH"] = store

    from fastapi.testclient import TestClient
    from Pit_Web import api
    import driver_message

    sent = []
    driver_message.send_new_race = lambda: sent.append(1) or {"id": 1}
    c = TestClient(api.app)

    def press(body):
        del sent[:]
        r = c.post("/api/race", json=body)
        return r.json(), len(sent)

    body, n = press({"isRacing": True})
    start = body.get("race_start_time")
    check("START: a race starting zeroes the car, once",
          n == 1 and body.get("newRace") is True, "%d command(s) sent" % n)

    _, n = press({"isRacing": True, "startTime": start})
    check("RESUME: starting a race already running does not",
          n == 0, "%d command(s) sent" % n)

    c.post("/api/race", json={"isRacing": False, "startTime": start})
    _, n = press({"isRacing": True, "startTime": start})
    check("RESUME: Stop then Resume does not",
          n == 0, "%d command(s) sent - a red flag must not wipe the race" % n)

    # The correction the pit actually makes: the race began 20 minutes ago.
    _, n = press({"isRacing": True, "startTime": start - 1200})
    check("CORRECT: backdating the start of a RUNNING race does not",
          n == 0, "%d command(s) sent" % n)

    # And a genuinely new race, at a new time, after a stop.
    c.post("/api/race", json={"isRacing": False, "startTime": start - 1200})
    body, n = press({"isRacing": True})
    check("START: a later, different race does zero it again",
          n == 1 and body.get("newRace") is True, "%d command(s) sent" % n)

    # A car that cannot be reached.
    def boom():
        raise RuntimeError("no route to Firebase")
    driver_message.send_new_race = boom
    c.post("/api/race", json={"isRacing": False})
    r = c.post("/api/race", json={"isRacing": True})
    body = r.json()
    check("SURVIVES: an unreachable car does not stop the race",
          r.status_code == 200 and body.get("is_racing") is True
          and body.get("carError"),
          "carError=%r" % body.get("carError"))
    return c, start


def check_lap_floor(c):
    """FLOOR: the lap views start at the green flag, and only then."""
    import db
    from Pit_Web import api
    real = db.fetch_laps
    seen = {}

    def spy(conn, *a, **kw):
        seen["since_ts"] = kw.get("since_ts", "not passed")
        return real(conn, *a, **kw)

    def floor_after(is_racing, start):
        with __import__("contextlib").closing(api.rw_conn()) as conn:
            db.save_race_state(conn, is_racing, start)
        seen.clear()
        db.fetch_laps = spy
        try:
            c.get("/api/laps")
        finally:
            db.fetch_laps = real
        return seen.get("since_ts")

    at = floor_after(True, 1789560000.0)
    check("FLOOR: /api/laps starts at the green flag",
          at == 1789560000.0, "bounded at %s" % at)

    # A race that was STOPPED still happened; its laps are still the ones worth
    # looking at afterwards.
    at = floor_after(False, 1789560000.0)
    check("        a stopped race still bounds it",
          at == 1789560000.0, "bounded at %s" % at)

    # And with no race ever started, the whole store, exactly as before.
    at = floor_after(False, None)
    check("        and it is the whole store when none was started",
          at is None, "bounded at %s" % at)


def main():
    print("\nthe green flag zeroes the car - and only the green flag\n")
    t = check_zeroed()
    check_reboot(t)
    check_command_accepted()
    c, _ = check_when_it_fires()
    check_lap_floor(c)
    if FAILED:
        print("\n%d FAILED:" % len(FAILED))
        for f in FAILED:
            print("  - %s" % f)
        return 1
    print("\nAll new-race checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
