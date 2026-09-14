#!/usr/bin/env python3
"""
check_catchup.py - the collector's paged catch-up, with no network and no car
=============================================================================
    python tools/check_catchup.py

Drives collector.catch_up() against a fake Realtime Database and a throwaway
SQLite file, and exits non-zero if it pages wrongly or fails to stop.

WHY THIS IS WORTH A TOOL. Restarting the collector days behind used to ask
RTDB for the whole tail in ONE streamed event. Measured on 2026-09-08, a
12-day gap was a single 166.6 MB event: requests' iter_lines() buffers it as
one line, json.loads() expands it, and only then is anything stored or
logged, so the process sat silent for minutes on a multi-GB working set and
looked hung. catch_up() replaces that with bounded REST pages.

The failure mode that would be WORSE than the bug it fixes is a loop that
never ends. startAt is inclusive, so every page re-delivers its boundary key;
if the termination conditions are wrong the collector spins forever on race
morning and never opens the stream at all. Cases 4 and 5 below are that test.

Checks THIS folder's collector by default. Point it at another copy with
SOLARRACE_ROOT, which is how the race repo's collector gets checked without
adding a file to the race repo:

    SOLARRACE_ROOT="../THE RACE" python tools/check_catchup.py
"""

import io
import os
import sqlite3
import sys
import tempfile

_ROOT = os.environ.get("SOLARRACE_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.abspath(_ROOT)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "Pit_Dashboard"))
print("checking: %s" % _ROOT)

import db                                                        # noqa: E402
import collector                                                 # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print("  %-52s %s" % (name, "OK" if ok else "FAIL"))
    if not ok:
        FAILURES.append(name + ((" - " + detail) if detail else ""))
    elif detail:
        print("      %s" % detail)


class FakeRTDB:
    """Answers orderBy=$key&startAt=..&limitToFirst=N out of an ordered dict.

    Mirrors the real thing in the one way that matters here: startAt is
    INCLUSIVE, so a page always re-delivers the key it started from.
    """

    def __init__(self, keys):
        # Push keys sort lexicographically in chronological order.
        self.data = {k: {"ts": float(i), "motor": {"rpm": i}}
                     for i, k in enumerate(keys)}
        self.requests = 0
        self.limits = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests += 1
        params = params or {}
        limit = int(params.get("limitToFirst", 100))
        self.limits.append(limit)
        start = params.get("startAt")
        keys = sorted(self.data)
        if start:
            start = start.strip('"')
            keys = [k for k in keys if k >= start]      # inclusive
        page = {k: self.data[k] for k in keys[:limit]}
        return _Resp(page)


class _StuckRTDB(FakeRTDB):
    """Always returns the SAME full page, however far the cursor advances.

    Not a real RTDB behaviour -- it is the shape of a bug (a cursor that
    never moves). If catch_up cannot escape this, it cannot be trusted to
    stop at all.
    """

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests += 1
        limit = int((params or {}).get("limitToFirst", 100))
        keys = sorted(self.data)[:limit]
        return _Resp({k: self.data[k] for k in keys})


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def fresh_store():
    path = os.path.join(tempfile.mkdtemp(prefix="catchup_"), "t.db")
    conn = db.get_conn(path)
    db.init_db(conn)
    return conn


def run(keys, start_key, page_size=None, cls=FakeRTDB, max_requests=200):
    """catch_up() against a fake backend. Returns (cursor, stored, backend)."""
    fake = cls(keys)
    real_get, real_token = collector.requests.get, collector.fresh_token
    real_page = collector.CATCHUP_PAGE_SIZE
    if page_size is not None:
        collector.CATCHUP_PAGE_SIZE = page_size

    def guarded(*a, **kw):
        if fake.requests >= max_requests:
            raise AssertionError("catch_up made %d requests: it is not "
                                 "terminating" % fake.requests)
        return fake.get(*a, **kw)

    collector.requests.get = guarded
    collector.fresh_token = lambda creds: "TEST"
    conn = fresh_store()
    try:
        cursor = collector.catch_up(conn, None, start_key)
        return cursor, db.count_samples(conn), fake
    finally:
        collector.requests.get = real_get
        collector.fresh_token = real_token
        collector.CATCHUP_PAGE_SIZE = real_page
        conn.close()


K = ["-K%05d" % i for i in range(1, 1201)]       # 1200 samples, in order

print(__doc__.split("WHY")[0].strip())
print()

print("1. A cold start with a large backlog pages it in")
cursor, stored, fake = run(K, None, page_size=250)
check("every sample stored", stored == 1200, "%d stored" % stored)
check("paged rather than one huge request", fake.requests >= 5,
      "%d requests of %d" % (fake.requests, fake.limits[0]))
check("cursor left at the newest key", cursor == K[-1], "cursor %s" % cursor)
check("request size is bounded", max(fake.limits) == 250)

print("\n2. Resuming mid-way only fetches what is new")
cursor, stored, fake = run(K, K[999], page_size=250)
check("only the tail was stored", stored == 201,
      "%d stored (the boundary key is re-delivered, by design)" % stored)
check("cursor advanced to the newest key", cursor == K[-1])

print("\n3. Already up to date: one request, nothing stored")
cursor, stored, fake = run(K, K[-1], page_size=250)
# It breaks BEFORE the upsert when the only key returned is the boundary
# one, so an up-to-date collector writes nothing at all rather than
# re-storing its own cursor row.
check("nothing written at all", stored == 0, "not even the boundary row")
check("exactly one request", fake.requests == 1, "%d requests" % fake.requests)
check("cursor unchanged", cursor == K[-1])

print("\n4. An empty node terminates immediately")
cursor, stored, fake = run([], None, page_size=250)
check("no samples", stored == 0)
check("one request, then stop", fake.requests == 1, "%d requests" % fake.requests)

print("\n5. A cursor that never advances must NOT loop forever")
try:
    cursor, stored, fake = run(K, K[0], page_size=250, cls=_StuckRTDB,
                               max_requests=50)
    check("terminated instead of spinning", True,
          "%d requests before stopping" % fake.requests)
except AssertionError as exc:
    check("terminated instead of spinning", False, str(exc))

print("\n6. A backlog smaller than one page still lands")
cursor, stored, fake = run(K[:40], None, page_size=250)
check("all 40 stored in one request", stored == 40 and fake.requests == 1,
      "%d stored in %d request(s)" % (stored, fake.requests))

print("\n7. The real page size is what the config says")
import pit_config                                                # noqa: E402
check("collector uses pit_config.CATCHUP_PAGE_SIZE",
      collector.CATCHUP_PAGE_SIZE == pit_config.CATCHUP_PAGE_SIZE
      == 5000, "%d" % collector.CATCHUP_PAGE_SIZE)

print()
if FAILURES:
    print("FAILED (%d):" % len(FAILURES))
    for f in FAILURES:
        print("  - %s" % f)
    sys.exit(1)
print("All catch-up checks passed.")
