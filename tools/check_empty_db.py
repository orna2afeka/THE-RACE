#!/usr/bin/env python3
"""
check_empty_db.py - the dashboard on a freshly archived store, with no car
==========================================================================
    python tools/check_empty_db.py

Serves every read endpoint twice against throwaway SQLite files -- once EMPTY,
once POPULATED -- and exits non-zero if the empty answer is a different SHAPE
from the populated one, or if anything 5xx's.

WHY THIS IS WORTH A TOOL. An empty store is not a rare edge case: it is the
FIRST thing the pit sees after tools/archive_db.py, which is to say on the
morning of every practice day and of the race itself. It is also the one state
nobody develops against, because the laptop you write the code on always has
months of telemetry in it.

Measured on 2026-09-17, minutes after the first archive: /api/history took an
early return for an empty store that omitted count, sampled, downsampled and
tz, answered 200, and the History tab died on
`Cannot read properties of undefined (reading 'toLocaleString')`. A 200 with a
short payload is worse than a 500 -- nothing retries it and nothing logs it,
the tab simply goes white.

So the check is not "does it return 200" but "does it return the SAME KEYS".
A field the UI reads unconditionally must exist in both answers, holding null
where there is no value -- never missing, and never 0, which would be a
reading the car never sent.
"""

import os
import sys
import sqlite3
import tempfile
import warnings

warnings.filterwarnings("ignore")

_ROOT = os.environ.get("SOLARRACE_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "Pit_Dashboard"))

import db                                                        # noqa: E402

FAILED = []


def check(label, ok, detail=""):
    print("  %-52s %s" % (label, "OK" if ok else "FAIL"))
    if detail:
        print("      " + detail)
    if not ok:
        FAILED.append("%s - %s" % (label, detail))
    return ok


def make_store(path, samples):
    """A store with `samples` rows. 0 gives the freshly archived case.

    Every numeric column is filled, so the populated answer exercises the full
    payload: a column left NULL here would go missing from the populated shape
    too and hide the very divergence this is looking for."""
    conn = db.get_conn(path)
    db.init_db(conn)
    if samples:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(telemetry)")]
        skip = {"device_id", "rtdb_key", "device_ts", "ingested_ts",
                "calculated_lap"}
        fill = [c for c in cols if c not in skip]
        names = ["device_id", "rtdb_key", "device_ts", "ingested_ts",
                 "calculated_lap"] + fill
        sql = "INSERT INTO telemetry (%s) VALUES (%s)" % (
            ",".join(names), ",".join("?" * len(names)))
        base = 1789560000.0
        for i in range(samples):
            row = ["solarcar", "-K%06d" % i, base + i * 10.0, base + i * 10.0,
                   1 + i // 20]
            conn.execute(sql, row + [float(20 + (i % 40)) for _ in fill])
        conn.commit()
    conn.close()


def paths(obj, prefix=""):
    """Every key path in a JSON payload, so two answers can be compared by
    STRUCTURE rather than by value. Lists contribute their first element's
    shape: an empty store legitimately has fewer ROWS, and only a differing
    set of FIELDS is a bug."""
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(prefix + k)
            out |= paths(v, prefix + k + ".")
    elif isinstance(obj, list) and obj:
        out |= paths(obj[0], prefix + "[].")
    return out


def main():
    from fastapi.testclient import TestClient
    from Pit_Web import api

    tmp = tempfile.mkdtemp(prefix="emptydb_")
    empty = os.path.join(tmp, "empty.db")
    full = os.path.join(tmp, "full.db")
    make_store(empty, 0)
    make_store(full, 400)

    keys = ",".join(m.key for m in api.HISTORY_CHARTS[:3])
    urls = [
        "/api/config",
        "/api/live",
        "/api/cells",
        "/api/cell_extremes",
        "/api/history?metrics=%s&minutes=60" % keys,
        "/api/history/stats?metrics=%s&minutes=60" % keys,
        "/api/samples",
        "/api/laps",
        "/api/faults",
        "/api/sectors",
        "/api/strategy",
        "/api/export/estimate",
        "/api/export/bounds",
        "/api/trip_reset/ack",
        "/api/cut_lap/ack",
        "/api/strategy/ack",
    ]

    print(__doc__.split("WHY")[0].strip())
    print()

    def serve(path):
        api.DB_PATH = path
        # api.cached() memoises by (endpoint, params) for HEAVY_CACHE_TTL_S and
        # knows nothing about which database answered. Without this the second
        # pass replays the FIRST pass's payloads, both shapes match trivially,
        # and the check passes while the bug it exists to catch is still there
        # -- which is exactly what it did before this line.
        api._cache.clear()
        client = TestClient(api.app, raise_server_exceptions=False)
        out = {}
        for u in urls:
            r = client.get(u)
            body = None
            if r.headers.get("content-type", "").startswith("application/json"):
                try:
                    body = r.json()
                except ValueError:
                    body = None
            out[u] = (r.status_code, body)
        return out

    print("1. Nothing 5xx's on an empty store")
    got_empty = serve(empty)
    for u, (code, _b) in got_empty.items():
        if code >= 500:
            check(u, False, "HTTP %d" % code)
    check("every endpoint answered under 500",
          all(c < 500 for c, _ in got_empty.values()),
          "%d endpoint(s) checked" % len(urls))

    print("\n2. The empty answer has the same SHAPE as the populated one")
    got_full = serve(full)
    for u in urls:
        ec, eb = got_empty[u]
        fc, fb = got_full[u]
        if eb is None or fb is None:
            check(u, ec == fc, "status %d empty vs %d populated" % (ec, fc))
            continue
        missing = paths(fb) - paths(eb)
        # A list that is empty because there are no rows drops its element
        # shape with it, which is honest rather than a divergence.
        missing = {m for m in missing if "[]." not in m}
        # /api/live's `state` is built from last_known, which by design holds
        # only metrics the car has actually reported: a cell voltage absent
        # before the first BMS frame is the same absence as on a car that never
        # sends one, and tools/check_health.py pins that a missing health block
        # must read as healthy rather than faulty. Sparse here is correct, so
        # only the envelope around it is held to the same shape.
        missing -= {m for m in missing if m.startswith("state.")}
        check(u, not missing,
              "missing when empty: %s" % ", ".join(sorted(missing))
              if missing else "")

    print()
    if FAILED:
        print("FAILED (%d):" % len(FAILED))
        for f in FAILED:
            print("  - " + f)
        return 1
    print("All good: the dashboard answers an archived store the same way it")
    print("answers a full one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
