#!/usr/bin/env python3
"""
check_wall_feed.py - the pit wall page reads nothing the pit wall never sends
=============================================================================
    python tools/check_wall_feed.py

Reads Pit_Dashboard/wall.html, collects every field it takes out of the
snapshot, and fails if the feed in tools/pit_wall.py does not serve it.

THE BUG THIS EXISTS FOR. wall.html read `s.stopwatch_s` -- the shared
stopwatch, the whole point of which is that the driver, the pit and the TV
count the same lap the same way. pit_wall.FIELDS never selected the column, so
the page got `undefined` every second of its life and quietly took its
fallback branch instead: the TV subtracted two of the Pi's own wall-clock
stamps while the dashboard followed a different rule. Two clocks agreeing by
coincidence, which is the failure the shared stopwatch was built to end.

Nothing caught it. check_fields() in pit_wall.py checks the other direction --
a FIELD the DATABASE cannot supply -- and a page reading a field nobody sends
looks exactly like a car that is not reporting: a dash. The page cannot even
tell the difference, and neither can anyone standing in front of it.

So this asks the question from the page's side, with no browser and no
database: what does the page read, and does the server put it in the payload?
The live feed is exercised against a throwaway empty store (the state the pit
is in every race morning, after tools/archive_db.py) and the demo feed as
well, because the demo drives the same page and drops fields of its own.
"""

import os
import re
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")

_ROOT = os.environ.get("SOLARRACE_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "Pit_Dashboard"),
           os.path.join(_ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import db                                                        # noqa: E402
import pit_wall                                                  # noqa: E402

# The page this serves. An argument overrides it, which is how the check is
# shown to work: run it against a copy of the page from before the clock was
# shared and it reports stopwatch_s, exactly as it should have done all along.
PAGE = (sys.argv[1] if len(sys.argv) > 1
        else os.path.join(_ROOT, "Pit_Dashboard", "wall.html"))

# The functions where `s` is the snapshot. Everywhere else in the page `s` is
# a sector, a count of seconds or a scratch variable, and `s.id` or `s.color`
# is not a question about the feed.
SNAPSHOT_FUNCS = ("render", "renderRace")

FAILED = []


def check(label, ok, detail=""):
    print("  %-52s %s" % (label, "OK" if ok else "FAIL"))
    if detail:
        print("      " + detail)
    if not ok:
        FAILED.append("%s - %s" % (label, detail))
    return ok


def function_body(src, name):
    """The text of top-level `function name()`, to the next one.

    Every function in the generated page starts at column 0, so the next line
    beginning "function " ends this one. Crude, and it does not need to be
    anything else: the alternative is a JavaScript parser to answer a question
    about which names follow a dot.
    """
    start = src.find("\nfunction %s(" % name)
    if start < 0:
        return None
    rest = src[start + 1:]
    end = rest.find("\nfunction ")
    return rest if end < 0 else rest[:end]


def fields_read(body):
    """Every snapshot field `body` reads, however it reaches for it."""
    names = set(re.findall(r"\bs\.([A-Za-z_][A-Za-z0-9_]*)", body))
    names |= set(re.findall(r"\bsnap\.([A-Za-z_][A-Za-z0-9_]*)", body))
    # put(id, s, "field", fmt) and isCarried(s, "field") name the field in a
    # string, which the dotted patterns above cannot see.
    names |= set(re.findall(r"\bput\(\s*\"[^\"]*\"\s*,\s*s\s*,\s*\"([A-Za-z0-9_]+)\"",
                            body))
    names |= set(re.findall(r"\bisCarried\(\s*s\s*,\s*\"([A-Za-z0-9_]+)\"", body))
    return names


def served():
    """(live keys, demo keys) -- what each feed actually puts in the payload.

    The live one runs against an EMPTY throwaway store, which is the hardest
    case for this question: every value is null and every key must still be
    there, because a key that only appears once the car has been round is a
    key the page cannot rely on.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "empty.db")
        conn = db.get_conn(path)
        db.init_db(conn)
        conn.close()
        live = pit_wall.Feed(path).read_once()
    return set(live), set(pit_wall.DemoFeed().get())


# Served by neither feed's normal payload, and read by the page on purpose:
# `error` is attached only when a read fails, and the page's whole job in that
# moment is to say so.
ALWAYS_ALLOWED = {"error"}


def main():
    print(__doc__.strip().splitlines()[0])
    print()
    src = open(PAGE, encoding="utf-8").read()
    live, demo = served()
    check("the live feed answered an empty store", "error" not in live,
          "" if "error" not in live else "read_once returned an error")

    read = set()
    for name in SNAPSHOT_FUNCS:
        body = function_body(src, name)
        if not check("wall.html still has %s()" % name, body is not None):
            continue
        read |= fields_read(body)
    check("the page reads something at all", len(read) > 5,
          "%d fields" % len(read))

    # Either feed serving it is enough: the page is driven by one or the
    # other, never both, and `demo` is the flag that says which.
    known = live | demo | ALWAYS_ALLOWED
    missing = sorted(n for n in read if n not in known)
    check("every field the page reads is served", not missing,
          "not in the payload: " + ", ".join(missing) if missing else "")

    # The demo drives the same page with no database at all. A field it omits
    # is not a failure -- it has no GPS and no charger to report -- but it is
    # worth naming, because a tile that is dead ONLY in the demo is how a tile
    # wired to the wrong field went unnoticed here before.
    demo_gaps = sorted(n for n in read if n in live and n not in demo)
    if demo_gaps:
        print("  note: the demo feed does not send " + ", ".join(demo_gaps))

    print()
    if FAILED:
        print("FAILED:")
        for f in FAILED:
            print("  - " + f)
        return 1
    print("The pit wall page and the pit wall feed agree on every field.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
