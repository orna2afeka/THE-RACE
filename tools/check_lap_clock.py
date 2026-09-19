#!/usr/bin/env python3
"""
check_lap_clock.py - the wall's lap clock answers a press before the car does
=============================================================================
    python tools/check_lap_clock.py

No browser, no network, no database: _lap_clock() is called directly with a
stubbed app-state store, and ws_live is driven with the fake socket from
check_live_cadence.py.

THE BUG THIS EXISTS FOR. Cut lap and Restart lap both end the lap on the car,
and the one number an engineer presses those buttons to watch is the lap clock
on the wall going back to 0:00. It used to get there only when the car said so
-- command up to Firebase, down to the car over LTE, applied, and back inside
the next telemetry sample -- and on a bad link that measured four to five
seconds, all of it spent counting the lap that had just been ended. The last
two seconds of the wait were the pit's own: the change was already in SQLite
and sat there until the next scheduled push.

So the press is stamped in the pit (api.LAP_DATUM_KEY) and the screens are
woken (api.nudge_live). Neither may cost the car its authority:

  HANDOVER   the pit's datum is dropped the instant a sample TAKEN AFTER the
             press arrives, so the wall can never disagree with the car for
             longer than one sample. THIS IS THE WHOLE SAFETY ARGUMENT for
             showing a press before it has been confirmed.
  HOLD       a hold pressed before the re-datum belongs to the lap that ended
             and must not park the new clock.
  DARK CAR   a car that goes quiet across a press freezes the wall on 0:00 --
             it must never count on from a press nothing confirmed. A store
             with NO samples is a different thing (a pit that has never heard
             from the car, not a car that has not answered) and the press is
             ignored: the press outlives a restart, the data does not.
  NO STORM   the wake is one-shot, bounded by LIVE_MIN_PUSH_S, and leaves no
             events behind when a screen disconnects.

Run it against a build without the datum and the first check FAILS.
"""

import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Pit_Web import api                                          # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print("  %-44s %s  %s" % (name, "OK  " if ok else "FAIL", detail))
    if not ok:
        FAILURES.append("%s - %s" % (name, detail))


# --------------------------------------------------------------------------- #
# 1-7: the clock itself.
# --------------------------------------------------------------------------- #
_STATE = {}
api.load_app_state = lambda conn, key: _STATE.get(key)


def clock(age, state, **app_state):
    """_lap_clock() with `age` seconds since the newest sample."""
    _STATE.clear()
    _STATE.update(app_state)
    return api._lap_clock(None, state, 5, age)


def clock_checks():
    now = time.time()

    c = clock(3.0, {"lap_started_ts": now - 180},
              lap_clock_datum={"atS": now - 0.2})
    check("a press beats the car's older datum",
          c["source"] == "pit" and abs(c["startedAt"] - (now - 0.2)) < 0.01,
          "counts from the press, not from %.0f s ago" % 180)

    c = clock(0.5, {"lap_started_ts": now - 1.0},
              lap_clock_datum={"atS": now - 3.0})
    check("a newer sample takes the clock back",
          c["source"] == "car", "source=%s" % c["source"])

    c = clock(0.5, {"stopwatch_s": 12.0}, lap_clock_datum={"atS": now - 3.0})
    check("the shared stopwatch takes it back too",
          c["source"] == "car" and c["atSampleS"] == 12.0,
          "source=%s at %s s" % (c["source"], c["atSampleS"]))

    c = clock(1.0, {"lap_started_ts": now - 60})
    check("no press, nothing changes",
          c["source"] == "car" and abs(c["atSampleS"] - 59.0) < 0.01,
          "%.0f s into the lap" % c["atSampleS"])

    c = clock(3.0, {"lap_started_ts": now - 180},
              lap_clock_datum={"atS": now - 2.0},
              lap_clock_hold={"heldAt": now - 1.0})
    check("a hold after the press parks the new clock",
          c["heldAt"] is not None, "heldAt=%s" % c["heldAt"])

    c = clock(3.0, {"lap_started_ts": now - 180},
              lap_clock_datum={"atS": now - 1.0},
              lap_clock_hold={"heldAt": now - 9.0})
    check("a hold from before it is released",
          c["heldAt"] is None, "heldAt=%s" % c["heldAt"])

    c = clock(45.0, {"lap_started_ts": now - 300},
              lap_clock_datum={"atS": now - 5.0})
    check("a car quiet since the press freezes on 0:00",
          c["source"] == "pit" and c["atSampleS"] == 0.0,
          "atSampleS=%s" % c["atSampleS"])

    c = clock(None, {}, lap_clock_datum={"atS": now - 5.0})
    check("a store with no samples ignores the press",
          c["startedAt"] is None and c["source"] is None,
          "source=%s" % c["source"])


# --------------------------------------------------------------------------- #
# 8-11: the wake. Driven with check_live_cadence.py's fake socket rather than a
# second copy of it.
# --------------------------------------------------------------------------- #
def _cadence_helpers():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "check_live_cadence.py")
    with open(path, encoding="utf-8") as f:
        src = f.read().split("async def run(")[0]
    ns = {"__file__": path}
    exec(compile(src, path, "exec"), ns)
    return ns


async def wake_checks():
    ns = _cadence_helpers()
    api.build_live = ns["_stub_build_live"]
    api.ro_conn = lambda: ns["_Conn"]()
    api.FAST_TICK_S = 5.0        # a long tick, so a wake is unmistakable
    api.LIVE_MIN_PUSH_S = 0.05
    api._live_loop = asyncio.get_running_loop()

    screens = [ns["FakeWS"]() for _ in range(3)]
    tasks = [asyncio.create_task(api.ws_live(w)) for w in screens]
    await asyncio.sleep(0.3)                     # each sends its first payload
    before = [len(w.sent) for w in screens]

    # The press, on a plain thread -- exactly where FastAPI serves the button.
    pressed = time.monotonic()
    threading.Thread(target=api.nudge_live).start()
    await asyncio.sleep(0.4)
    after = [len(w.sent) for w in screens]
    check("every screen pushes on a press, not just one",
          all(a == b + 1 for a, b in zip(after, before)),
          "%s -> %s" % (before, after))

    # FakeWS timestamps each send against its own t0.
    woke_in = min(w.t0 + w.sent[-1][0] for w in screens) - pressed
    check("and within the floor, not at the next tick",
          woke_in < 1.0,
          "%.0f ms after the press (the tick is %.0f s)"
          % (woke_in * 1000, api.FAST_TICK_S))

    steady = [len(w.sent) for w in screens]
    await asyncio.sleep(0.6)
    check("the wake is one-shot, not a storm",
          all(len(w.sent) == s for w, s in zip(screens, steady)),
          "%s -> %s" % (steady, [len(w.sent) for w in screens]))

    for w in screens:
        await w.inbox.put(ns["CLOSE"])
    await asyncio.sleep(0.2)
    for t in tasks:
        t.cancel()
    check("no wake events left behind on disconnect",
          len(api._live_wakers) == 0, "%d left" % len(api._live_wakers))


def main():
    print("\nthe wall's lap clock, against a press the car has not answered\n")
    clock_checks()
    asyncio.run(wake_checks())
    if FAILURES:
        print("\n%d FAILED:" % len(FAILURES))
        for f in FAILURES:
            print("  - %s" % f)
        return 1
    print("\nAll lap-clock checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
