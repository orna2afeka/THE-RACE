#!/usr/bin/env python3
"""
check_sandbox.py - a dashboard off the pit's store cannot reach the car
=======================================================================
    python tools/check_sandbox.py

Presses every button the web UI has, twice: once with the backend pointed at a
throwaway store (the demo dashboard) and once at the pit's own (race day). The
HTTP verbs inside driver_message are replaced with recorders, so NOTHING is
ever sent -- this check needs no network, no credentials and no car.

WHAT THIS IS FOR. Pointing the backend at demo_telemetry.db changes which
SQLite file it READS. It does nothing whatever to Firebase, which has exactly
one /lap_command, one /driver_command and one /strategy_command -- one car.
Before api.car_link() existed, every send button on the demo dashboard reached
the real car:

    Start race -> new_race, which zeroes the car's laps, distance, energy and
                  rewrites its checkpoint file.
    Send to car -> the car changes the speed profile it is flying.
    Cut lap / Set lap / trip reset / stopwatch / driver messages -> all live.

AND THAT REACHED THE PUBLIC. docs/index.html, published by GitHub Pages, plots
/public/live, which the CAR writes. A demo that resets the car resets what
every spectator is watching. So the boundary is not a nicety for the demo's
sake; it is what keeps a practice run off the public page.

THE TWO HALVES ARE EQUALLY LOAD-BEARING. A gate that also blocks the real
dashboard is a pit wall that cannot call its driver in, so the second half
below is not a formality.
"""

import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "Pit_Dashboard")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

FAILED = []


def check(label, ok, note=""):
    print("  %-46s %s" % (label, "OK  " if ok else "FAIL"))
    if note:
        print("       %s" % note)
    if not ok:
        FAILED.append(label.strip())


class _Resp:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return None


def _recorder(calls, verb):
    def f(url, *a, **kw):
        calls.append((verb, str(url).split("firebasedatabase.app")[-1].split("?")[0],
                      kw.get("json")))
        return _Resp()
    return f


def _install_recorder(driver_message):
    """Replace driver_message's HTTP verbs. Returns the list they append to."""
    calls = []
    driver_message.requests = type("R", (), {
        v: staticmethod(_recorder(calls, v.upper()))
        for v in ("put", "post", "patch", "delete", "get")
    })()
    driver_message._token = lambda: "FAKE-TOKEN-NEVER-MINTED"
    return calls


# Every control on the dashboard that ends in a Firebase call, as
# (label, callable-factory). Built lazily because `api` must be imported only
# after SOLARRACE_DB_PATH is set.
def _buttons(api):
    return [
        ("Strategy > Send to car", lambda: api.api_strategy_select(api.StrategyBody(key=api.C.STRATEGIES[0]["key"]))),
        ("Strategy > ack poll", lambda: api.api_strategy_ack()),
        ("Driver message > send", lambda: api.api_driver_message(api.MessageBody(category="PIT", value="BOX"))),
        ("Driver message > clear", lambda: api.api_clear_message()),
        ("Trip reset", lambda: api.api_trip_reset()),
        ("Trip reset > ack poll", lambda: api.api_trip_reset_ack()),
        ("Cut lap", lambda: api.api_cut_lap()),
        ("Cut lap > ack poll", lambda: api.api_cut_lap_ack()),
        ("Lap > set", lambda: api.api_lap_set(api.LapSetBody(lap=42))),
        ("Lap > restart", lambda: api.api_lap_restart()),
        ("Lap > stopwatch", lambda: api.api_lap_stopwatch(api.StopwatchBody(action="reset"))),
        ("Lap > hold", lambda: api.api_lap_hold(api.LapHoldBody(hold=True))),
    ]


def _press(fn, calls):
    """Press one control and return the outbound calls it made.

    A refusal (HTTPException) is an outcome this check reads off the call
    list, so it is swallowed. An AttributeError or a TypeError is NOT: that
    means the button was named wrong here, and silently reporting it as "this
    control does not reach the car" would be exactly the false all-clear this
    file exists to prevent.
    """
    before = len(calls)
    try:
        fn()
    except (AttributeError, TypeError):
        raise
    except Exception:                                        # noqa: BLE001
        pass
    return calls[before:]


def check_demo_store_is_sealed():
    """DEMO: no button reaches Firebase when the store is not the pit's own."""
    import db
    tmp = tempfile.mkdtemp(prefix="sandbox_")
    store = os.path.join(tmp, "demo.db")
    conn = db.get_conn(store)
    db.init_db(conn)
    conn.close()
    os.environ["SOLARRACE_DB_PATH"] = store

    from Pit_Web import api
    import driver_message
    calls = _install_recorder(driver_message)

    check("DEMO: the backend knows it is off the real store",
          api.DEMO_STORE is True, "api.DEMO_STORE = %r" % api.DEMO_STORE)
    check("      and never publishes the driver name",
          api.PUBLIC_DRIVER_ENABLED is False)

    leaked = []
    for label, fn in _buttons(api):
        made = _press(fn, calls)
        if made:
            leaked.append("%s -> %s %s" % (label, made[0][0], made[0][1]))
    check("      no control reaches the car",
          not leaked, "\n       ".join(leaked) if leaked else "12 controls, 0 outbound calls")

    # The green flag needs its own press: api_race only commands the car when
    # a race actually STARTS, so a store with one already running proves
    # nothing. This is the single most destructive command in the system.
    api.api_race_reset()
    made = _press(lambda: api.api_race(api.RaceBody(isRacing=True)), calls)
    check("      the GREEN FLAG does not zero the car",
          not made, "sent %s" % (made[0][1] if made else "nothing"))

    body = api.api_race(api.RaceBody(isRacing=True))
    check("      and the refusal is reported, not hidden",
          body.get("carLinkDisabled") is True,
          "carLinkDisabled=%r" % body.get("carLinkDisabled"))
    return calls, driver_message


def check_real_store_still_commands(calls, driver_message):
    """RACE DAY: the same buttons, on the pit's own store, all still send.

    Imported fresh with SOLARRACE_DB_PATH cleared. Nothing here writes: the
    recorder is still installed, and the race clock is not touched, so this
    can be run during a race without consequence.
    """
    os.environ.pop("SOLARRACE_DB_PATH", None)
    import importlib
    import Pit_Web.api
    api = importlib.reload(Pit_Web.api)
    # reload() rebuilt the module object; driver_message is a separate module
    # and keeps the recorder, but re-install it so the call list is this
    # half's own and a leak here cannot be read as one from the demo half.
    calls = _install_recorder(driver_message)

    check("RACE: the backend knows it IS the pit",
          api.DEMO_STORE is False, "api.DEMO_STORE = %r" % api.DEMO_STORE)

    silent = []
    for label, fn in _buttons(api):
        if not _press(fn, calls):
            silent.append(label)
    check("      every control still reaches the car",
          not silent, "silent: %s" % ", ".join(silent) if silent
          else "12 controls, 12 outbound calls")


def main():
    print("\na dashboard off the pit's store cannot reach the car\n")
    calls, dm = check_demo_store_is_sealed()
    print()
    check_real_store_still_commands(calls, dm)
    if FAILED:
        print("\n%d FAILED:" % len(FAILED))
        for f in FAILED:
            print("  - %s" % f)
        return 1
    print("\nAll sandbox checks passed. Nothing was sent to the car.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
