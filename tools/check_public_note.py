#!/usr/bin/env python3
"""
check_public_note.py - the line the pit types, and what reaches the public
==========================================================================
    python tools/check_public_note.py

Drives /api/public/note against a throwaway store with the write to Firebase
stubbed. No car, no network, no Firebase.

WHAT THIS IS FOR. docs/index.html can say the two things the system KNOWS -- a
driver change, and the charger -- and nothing about a tyre change, a puncture
or scrutineering. To the families reading it, a car stopped for any of those
looks like a car that has broken. So the pit can type a line and have it in
front of everyone with the URL within 15 s.

It is typed by people with their hands full, read by strangers, and nothing
expires it. That makes these the things that must stay true:

  SHOWS     what was typed is what is published, and it reaches the node.
  ONE LINE  a pasted newline does not become two lines on a phone, and a
            paragraph is cut to what a card can hold.
  HOLDS     re-sending the same words keeps the clock. The page reports how
            long the CAR has been in this state, not when a button was last
            pressed -- and the sync loop re-sends on its own every 15 s.
  CHANGES   different words are a different thing happening, and start their
            own count.
  DOWN      empty text takes it off the page, and the node goes with it when
            there is nothing else on it.
  AGES      a note put up six hours ago is still reported, untouched. The day
            somebody adds a "sensible" timeout, this fails first.
  QUIET     a demo dashboard cannot put anything in front of the public.
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
    tmp = tempfile.mkdtemp(prefix="note_")
    store = os.path.join(tmp, "race.db")
    conn = db.get_conn(store)
    db.init_db(conn)
    conn.close()
    os.environ["SOLARRACE_DB_PATH"] = store

    from fastapi.testclient import TestClient
    from Pit_Web import api
    import driver_message

    sent = []
    driver_message.publish_driver_name = (
        lambda name, changing_since=None, estimate=None, note=None:
        sent.append(note))
    c = TestClient(api.app)

    print("\nthe pit's own line on the public page\n")

    # QUIET first, while publishing is still off: a demo dashboard must not be
    # able to reach the public page at all.
    api.PUBLIC_DRIVER_ENABLED = False
    r = c.post("/api/public/note", json={"text": "Changing tyres"})
    check("QUIET:   a demo dashboard cannot post to the public page",
          r.status_code == 409, "HTTP %s" % r.status_code)
    check("         and it says so rather than pretending it worked",
          c.get("/api/public/note").json().get("note") is None)
    api.PUBLIC_DRIVER_ENABLED = True

    def publish():
        """Run the sync loop as the background thread does, and return the
        note that reached Firebase."""
        api._public_driver_sent = api._NOT_SENT      # force the write
        del sent[:]
        api.sync_public_driver()
        return sent[0] if sent else None

    # SHOWS
    before = time.time()
    posted = c.post("/api/public/note",
                    json={"text": "Changing tyres"}).json().get("note")
    got = c.get("/api/public/note").json().get("note")
    since = (got or {}).get("since")
    check("SHOWS:   the words come back as typed",
          posted == got and got["text"] == "Changing tyres", "%r" % (got,))
    check("         stamped with the pit's clock, not the tablet's",
          since is not None and before - 1 <= since <= time.time() + 1,
          "since=%r" % since)
    check("         and it reaches the node the page reads",
          publish() == {"text": "Changing tyres", "since": since},
          "published %r" % (sent[0] if sent else None,))

    # ONE LINE
    c.post("/api/public/note", json={"text": "  Front  tyre\nand a check  "})
    check("ONE LINE: newlines and double spaces collapse",
          c.get("/api/public/note").json()["note"]["text"] == "Front tyre and a check",
          "%r" % c.get("/api/public/note").json()["note"]["text"])
    c.post("/api/public/note", json={"text": "x" * 500})
    long_text = c.get("/api/public/note").json()["note"]["text"]
    check("         a pasted paragraph is cut to the card's width",
          len(long_text) == api.PUBLIC_NOTE_MAX_LEN, "%d chars" % len(long_text))

    # HOLDS / CHANGES
    c.post("/api/public/note", json={"text": "Changing tyres"})
    first = c.get("/api/public/note").json()["note"]["since"]
    time.sleep(0.05)
    c.post("/api/public/note", json={"text": "Changing tyres"})
    held = c.get("/api/public/note").json()["note"]["since"]
    check("HOLDS:   the same words keep the clock they had",
          held == first,
          "%r then %r - the page says how long the CAR has been like this"
          % (first, held))
    c.post("/api/public/note", json={"text": "Repairs in the pit"})
    moved = c.get("/api/public/note").json()["note"]
    check("CHANGES: different words start their own count",
          moved["text"] == "Repairs in the pit" and moved["since"] > first,
          "%r" % (moved,))

    # DOWN
    c.post("/api/public/note", json={"text": ""})
    check("DOWN:    empty text takes the note off the page",
          c.get("/api/public/note").json()["note"] is None)
    check("         and the node is deleted with nothing else on it",
          publish() is None, "published %r" % (sent[0] if sent else None,))

    # ... but not while a driver change is still flagged: the node carries
    # both, and taking the note down must not take the swap down with it.
    c.post("/api/driver_stint/changing", json={"on": True})
    c.post("/api/public/note", json={"text": "Changing tyres"})
    publish()
    c.post("/api/public/note", json={"text": ""})
    api._public_driver_sent = api._NOT_SENT
    del sent[:]
    api.sync_public_driver()
    stint = c.get("/api/live").json().get("driverStint") or {}
    check("         taking it down leaves the driver change alone",
          (sent[0] if sent else "missing") is None
          and stint.get("changeStartedAt") is not None,
          "note=%r changeStartedAt=%r"
          % (sent[0] if sent else "missing", stint.get("changeStartedAt")))
    c.post("/api/driver_stint/changing", json={"on": False})

    # AGES
    import contextlib
    from Pit_Web.store import save_app_state
    old = time.time() - 6 * 3600
    with contextlib.closing(api.rw_conn()) as conn:
        save_app_state(conn, api.PUBLIC_NOTE_KEY,
                       {"text": "Repairs in the pit", "since": old})
    aged = c.get("/api/public/note").json().get("note") or {}
    check("AGES:    a six-hour-old note is still reported, untouched",
          abs((aged.get("since") or 0) - old) < 1.0,
          "since=%r - if this fails, something grew a timeout and the team "
          "asked for none" % aged.get("since"))

    # And the write itself: the payload the page parses.
    puts, deletes = [], []

    class _Resp:
        def raise_for_status(self):
            pass

    import Pit_Dashboard.driver_message as dm_mod
    dm_mod._token = lambda: "stub"
    dm_mod.requests.put = lambda *a, **kw: puts.append(kw.get("json")) or _Resp()
    dm_mod.requests.delete = lambda *a, **kw: deletes.append(1) or _Resp()
    dm_mod.publish_driver_name(None, note={"text": "Changing tyres",
                                           "since": old})
    check("PAYLOAD: a note with no driver name still writes the node",
          len(puts) == 1 and puts[0].get("note") == "Changing tyres"
          and puts[0].get("noteSince") == old and not deletes,
          "put %r" % (puts[-1] if puts else None,))
    dm_mod.publish_driver_name(None, note=None)
    check("         and nothing at all deletes it",
          len(deletes) == 1, "%d delete(s)" % len(deletes))

    print()
    if FAILED:
        print("FAILED:")
        for f in FAILED:
            print("  - " + f)
        return 1
    print("All public-note checks passed. Nothing was published anywhere.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
