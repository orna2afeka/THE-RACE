#!/usr/bin/env python3
"""
check_driver_change.py - the swap the pit flags, and everything that clears it
==============================================================================
    python tools/check_driver_change.py

Drives the driver-stint endpoints against a throwaway store, with the write to
Firebase stubbed. No car, no network, no Firebase.

WHAT THIS IS FOR. The system knew when a driver change ENDED -- "Driver changed
- reset timer" starts the next stint -- and never when one began. So while the
car sat in the box being swapped, the public page showed the previous driver
and a car that was not moving, with nothing to say why: a broken car, to the
families the page is for. The pit now says when a swap starts, and that single
instant drives the badge on docs/index.html and the pill on the pit wall.

NOTHING EXPIRES IT, BY DECISION. The team asked for a flag that stays up until
they take it down, so there is no timeout anywhere -- and that makes the ways
it CAN be cleared the whole safety story. Every one of these must stay true:

  STARTS    the press stamps the server's clock, and says so to every screen.
  HOLDS     pressing it again does not restart the count. The number on the
            wall is the age of the SWAP, not of the last press.
  ENDS      "Driver changed" clears it. In a normal stop that is the only
            other press, so the flag cannot outlive the swap by accident.
  CANCELS   on=false clears it, for the stop that turned out not to be a swap.
  UNDO      undoing a mis-clicked "Driver changed" brings the swap back: the
            crew was mid-change, and the undo says the change did not happen.
  GREEN     a new race clears it. A flag from a previous race must not open
            the next one with the car already in the pits.
  PUBLISHED the (name, since) pair reaches Firebase, and a swap flagged before
            anybody typed a name still publishes -- the node is only deleted
            when there is neither a name nor a swap.
  WALL      pit_wall.py reads the same instant back out of app_state, so the
            TV in the garage and the page at home cannot disagree.
  AGES      an old flag is still reported, untouched. This is the one this
            file exists to keep honest: the day someone adds a "sensible"
            timeout, it fails here first.
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


def _client():
    """A dashboard on an empty store, with the car and Firebase stubbed."""
    import db
    tmp = tempfile.mkdtemp(prefix="swap_")
    store = os.path.join(tmp, "race.db")
    conn = db.get_conn(store)
    db.init_db(conn)
    conn.close()
    os.environ["SOLARRACE_DB_PATH"] = store

    from fastapi.testclient import TestClient
    from Pit_Web import api
    import driver_message

    driver_message.send_new_race = lambda: {"id": 1}
    return TestClient(api.app), store


def check_presses(c):
    """STARTS / HOLDS / ENDS / CANCELS / UNDO."""
    c.post("/api/race", json={"isRacing": True})

    before = time.time()
    st = c.post("/api/driver_stint/changing", json={"on": True}).json()
    since = st.get("changeStartedAt")
    check("STARTS: the press stamps the server's clock",
          since is not None and before - 1 <= since <= time.time() + 1,
          "changeStartedAt=%r" % since)

    again = c.post("/api/driver_stint/changing", json={"on": True}).json()
    check("HOLDS:  pressing it twice does not restart the count",
          again.get("changeStartedAt") == since,
          "%r then %r - the wall shows the age of the swap, not of the press"
          % (since, again.get("changeStartedAt")))

    done = c.post("/api/driver_stint", json={"driver": "Noa"}).json()
    check("ENDS:   \"Driver changed\" clears it",
          done.get("changeStartedAt") is None and done.get("driver") == "Noa",
          "changeStartedAt=%r, driver=%r"
          % (done.get("changeStartedAt"), done.get("driver")))

    back = c.post("/api/driver_stint/undo", json={}).json()
    check("UNDO:   undoing that mis-click brings the swap back",
          back.get("changeStartedAt") == since,
          "changeStartedAt=%r (was %r)" % (back.get("changeStartedAt"), since))

    off = c.post("/api/driver_stint/changing", json={"on": False}).json()
    check("CANCELS: on=false clears it",
          off.get("changeStartedAt") is None,
          "changeStartedAt=%r" % off.get("changeStartedAt"))
    return since


def check_green_flag(c):
    """GREEN: a new race does not open with a swap left over from the last."""
    c.post("/api/driver_stint/changing", json={"on": True})
    c.post("/api/race", json={"isRacing": False})
    body = c.post("/api/race", json={"isRacing": True}).json()
    st = body.get("driverStint") or {}
    check("GREEN:  a new race starts with no swap flagged",
          st.get("changeStartedAt") is None,
          "changeStartedAt=%r" % st.get("changeStartedAt"))


def check_ages(c):
    """AGES: an old flag is reported as it stands. Nothing times it out."""
    import contextlib
    from Pit_Web import api
    from Pit_Web.store import save_app_state

    old = time.time() - 6 * 3600
    with contextlib.closing(api.rw_conn()) as conn:
        st = api.load_app_state(conn, api.DRIVER_STINT_KEY) or {}
        st["change_started_at"] = old
        save_app_state(conn, api.DRIVER_STINT_KEY, st)

    st = c.get("/api/live").json().get("driverStint") or {}
    reported = st.get("changeStartedAt")
    check("AGES:   a six-hour-old flag is still reported, untouched",
          reported is not None and abs(reported - old) < 1.0,
          "changeStartedAt=%r - if this fails, something grew a timeout and "
          "the team asked for none" % reported)
    return old


def check_published():
    """PUBLISHED: what reaches Firebase, including a swap with no name yet."""
    import contextlib
    from Pit_Web import api
    from Pit_Web.store import save_app_state
    import driver_message

    sent = []
    driver_message.publish_driver_name = \
        lambda name, changing_since=None, estimate=None, note=None: (  # estimate: check_public_estimate.py, note: check_public_note.py
            sent.append((name, changing_since)))
    api.PUBLIC_DRIVER_ENABLED = True

    def publish(driver, since):
        global_state = {"driver": driver, "change_started_at": since,
                        "started_at": time.time(), "stint": 1}
        with contextlib.closing(api.rw_conn()) as conn:
            save_app_state(conn, api.DRIVER_STINT_KEY, global_state)
        api._public_driver_sent = api._NOT_SENT      # force the write
        del sent[:]
        api.sync_public_driver()
        return sent[0] if sent else None

    now = time.time()
    check("PUBLISHED: name and swap travel together",
          publish("Noa", now) == ("Noa", now), "sent %r" % (sent or None))
    check("           a swap before anybody typed a name still publishes",
          publish(None, now) == (None, now), "sent %r" % (sent or None))
    check("           no name and no swap deletes the node",
          publish(None, None) == (None, None),
          "sent %r - publish_driver_name deletes on an empty name"
          % (sent or None))

    # And the node itself: the delete only happens with neither.
    puts, deletes = [], []

    class _Resp:
        def raise_for_status(self):
            pass

    import Pit_Dashboard.driver_message as dm_mod
    dm_mod._token = lambda: "stub"
    dm_mod.requests.put = lambda *a, **kw: puts.append(kw.get("json")) or _Resp()
    dm_mod.requests.delete = lambda *a, **kw: deletes.append(1) or _Resp()
    dm_mod.publish_driver_name(None, changing_since=now)
    check("           the write says changing:true with no name",
          len(puts) == 1 and puts[0].get("changing") is True
          and not deletes and "name" not in puts[0],
          "put %r, %d delete(s)" % (puts[-1] if puts else None, len(deletes)))
    dm_mod.publish_driver_name(None, changing_since=None)
    check("           and only an empty pair deletes it",
          len(deletes) == 1, "%d delete(s)" % len(deletes))


def check_wall(store, since):
    """WALL: the TV reads the same instant the public page is showing."""
    sys.path.insert(0, os.path.join(_ROOT, "tools"))
    import db
    import pit_wall

    conn, _mode = db.get_conn_ro(store)
    try:
        read = pit_wall.Feed._driver_change(conn)
    finally:
        conn.close()
    check("WALL:   pit_wall reads the same instant back",
          read is not None and abs(read - since) < 1.0,
          "wall %r, store %r" % (read, since))

    # A database with no app_state at all -- a demo seed, or one older than the
    # web dashboard. No opinion, no exception, no blank pit wall.
    tmp = tempfile.mkdtemp(prefix="swapbare_")
    bare = os.path.join(tmp, "bare.db")
    import sqlite3
    sqlite3.connect(bare).close()
    conn, _mode = db.get_conn_ro(bare)
    try:
        ok = pit_wall.Feed._driver_change(conn) is None
    finally:
        conn.close()
    check("        a store with no app_state says nothing, quietly", ok)


def main():
    print(__doc__.split("=\n")[1].strip().splitlines()[0])
    print()
    c, store = _client()
    check_presses(c)
    check_green_flag(c)
    old = check_ages(c)
    check_wall(store, old)
    check_published()

    print()
    if FAILED:
        print("%d FAILED" % len(FAILED))
        for f in FAILED:
            print("  - " + f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
