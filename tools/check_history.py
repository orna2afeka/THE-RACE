#!/usr/bin/env python3
"""
check_history.py - the fast History endpoints answer what the slow ones did
===========================================================================
    python tools/check_history.py

Builds a throwaway store, then holds /api/history and /api/history/stats
against a plain-Python reference computed from every row: same timestamps,
same values, same min/avg/max/now, same counts of what was there and what was
missing. Also checks the one thing that makes them fast -- that the query is
answered by the covering index and never touches the table.

WHY THIS EXISTS. Reading a chart used to mean SELECT *: every column of every
row in the window, including raw_json, which is 4.6 kB of the 4.6 kB and is
not drawn. On the pit's own store the "3 hours" window took 67 s end to end
and the chart sat blank behind a "loading..." while it ran; the stat strip
underneath repeated the same scan every 10 s. The window now asks for the
fifteen columns it draws, thins by stride in SQL, and reads them out of
idx_telemetry_chart -- 0.36 s for the same window, and the numbers below are
how we know it is the same answer and not merely a quicker one.

A SLOWER ANSWER IS STILL AN ANSWER: the fallbacks matter here. An older store
missing a column must serve every other metric (a dash, not a 500), and a
store whose index has not been built yet -- one opened read-only before the
collector has run init_db once -- must return exactly the same numbers, just
without the index. Both are checked.
"""

import os
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")

_ROOT = os.environ.get("SOLARRACE_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "Pit_Dashboard")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import db                                                        # noqa: E402
from metrics import HISTORY_CHARTS, value_from_row               # noqa: E402

FAILED = []
ROWS = 900
DEVICE = "solarcar"


def check(label, ok, detail=""):
    print("  %-52s %s" % (label, "OK" if ok else "FAIL"))
    if detail:
        print("      " + detail)
    if not ok:
        FAILED.append("%s - %s" % (label, detail))
    return ok


def make_store(path, with_index=True, drop_col=None):
    """A store of ROWS samples, two seconds apart.

    Speed is left NULL on every fifth row, because "missing" is a number the
    stat strip prints and a reading the car never sent must never be counted
    or averaged as a zero.
    """
    conn = db.get_conn(path)
    db.init_db(conn)
    if not with_index:
        conn.execute("DROP INDEX IF EXISTS idx_telemetry_chart")
    if drop_col:
        # A store older than the column, which is what every store is the day
        # a metric is added. The index has to go first: SQLite will not drop a
        # column another object names.
        conn.execute("DROP INDEX IF EXISTS idx_telemetry_chart")
        conn.execute("ALTER TABLE telemetry DROP COLUMN %s" % drop_col)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(telemetry)")]
    skip = {"device_id", "rtdb_key", "device_ts", "ingested_ts",
            "calculated_lap", "mms_vehicle_speed_kmh", "raw_json"}
    fill = [c for c in cols if c not in skip]
    names = (["device_id", "rtdb_key", "device_ts", "ingested_ts",
              "calculated_lap", "mms_vehicle_speed_kmh"] + fill)
    sql = "INSERT INTO telemetry (%s) VALUES (%s)" % (
        ",".join(names), ",".join("?" * len(names)))
    base = 1789560000.0
    for i in range(ROWS):
        speed = None if i % 5 == 0 else float(30 + (i % 37))
        conn.execute(sql, ["solarcar", "-K%06d" % i, base + i * 2.0,
                           base + i * 2.0, 1 + i // 60, speed]
                     + [float(10 + (i % 23)) for _ in fill])
    conn.commit()
    conn.close()


def reference(path, chosen, start_ts=None):
    """What the old code said: every row in the window, read in Python."""
    conn, _ = db.get_conn_ro(path)
    rows = db.fetch_samples(conn, start_ts=start_ts)
    conn.close()
    series = {m.key: [value_from_row(r, m) for r in rows] for m in chosen}
    stats = {}
    for m in chosen:
        clean = [v for v in series[m.key] if v is not None]
        stats[m.key] = {
            "min": min(clean) if clean else None,
            "avg": (sum(clean) / len(clean)) if clean else None,
            "max": max(clean) if clean else None,
            "now": clean[-1] if clean else None,
            "samples": len(clean), "missing": len(series[m.key]) - len(clean),
        }
    return [r["device_ts"] for r in rows], series, stats


def close(a, b, tol=1e-9):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def main():
    print(__doc__.strip().splitlines()[0])
    print()
    from Pit_Web import api                                      # noqa: E402

    # 1 -- the invariant that keeps the fast path fast.
    missing = [m.source for m in HISTORY_CHARTS
               if m.source not in db.CHART_COLUMNS]
    check("every charted metric is in db.CHART_COLUMNS", not missing,
          "not covered by idx_telemetry_chart: " + ", ".join(missing)
          if missing else "%d columns" % len(db.CHART_COLUMNS))

    chosen = list(HISTORY_CHARTS)
    with tempfile.TemporaryDirectory() as tmp:
        cases = [(True, None, "with index"),
                 (False, None, "no index"),
                 (False, "mms_motor_ohms", "old store, no Motor Sensor column")]
        for n, (with_index, drop_col, tag) in enumerate(cases):
            path = os.path.join(tmp, "s%d.db" % n)
            make_store(path, with_index, drop_col)
            api.DB_PATH = path
            api._cache.clear()

            # 2 -- the chart, unthinned, against every row.
            ref_t, ref_series, ref_stats = reference(path, chosen)
            got = api._history(chosen, None, None, None, 200000, 10 ** 9)
            check("history: same timestamps (%s)" % tag,
                  got["t"] == [api._iso(t) for t in ref_t],
                  "%d vs %d points" % (len(got["t"]), len(ref_t)))
            bad = [m.key for m in chosen
                   if got["series"][m.key] != ref_series[m.key]]
            check("history: same values, every metric (%s)" % tag, not bad,
                  "differ: " + ", ".join(bad) if bad else "%d metrics" % len(chosen))
            check("history: sampled/count/downsampled (%s)" % tag,
                  got["sampled"] == ROWS and got["count"] == ROWS
                  and got["downsampled"] is False,
                  "sampled=%s count=%s" % (got["sampled"], got["count"]))

            # 3 -- the stat strip, over every sample and not the thinned set.
            api._cache.clear()
            stats = {s["key"]: s for s in
                     api._history_stats(chosen, None, None, None)["stats"]}
            wrong = []
            for m in chosen:
                a, b = ref_stats[m.key], stats[m.key]
                for f in ("min", "avg", "max", "now"):
                    if not close(a[f], b[f]):
                        wrong.append("%s.%s %r vs %r" % (m.key, f, a[f], b[f]))
                for f in ("samples", "missing"):
                    if a[f] != b[f]:
                        wrong.append("%s.%s %r vs %r" % (m.key, f, a[f], b[f]))
            check("stats: same figures as reading every row (%s)" % tag,
                  not wrong, "; ".join(wrong[:3]))
            check("stats: the NULL speeds counted as missing (%s)" % tag,
                  stats["Speed"]["missing"] == ROWS // 5,
                  "missing=%s of %d rows" % (stats["Speed"]["missing"], ROWS))

            # 4 -- thinning still lands on real samples, newest included.
            api._cache.clear()
            thin = api._history(chosen, None, None, None, 200000, 100)
            speeds = set(ref_series["Speed"])
            check("thinned: every point is a real reading (%s)" % tag,
                  all(v in speeds for v in thin["series"]["Speed"]),
                  "%d points" % thin["count"])
            check("thinned: the newest sample survives (%s)" % tag,
                  thin["t"][-1] == api._iso(ref_t[-1]) and thin["downsampled"],
                  thin["t"][-1])

        # 5 -- and it is the index doing the work.
        path = os.path.join(tmp, "s0.db")
        conn, _ = db.get_conn_ro(path)
        cols = ["device_ts", *db.CHART_COLUMNS]
        sql = ("SELECT %s FROM (SELECT %s, ROW_NUMBER() OVER "
               "(ORDER BY device_ts DESC) AS _rn FROM telemetry "
               "WHERE device_id = ? ORDER BY device_ts DESC LIMIT 1000) "
               "WHERE (_rn - 1) %% ? = 0 ORDER BY device_ts ASC"
               % (", ".join(cols), ", ".join(cols)))
        plan = " | ".join(r[3] for r in
                          conn.execute("EXPLAIN QUERY PLAN " + sql, (DEVICE, 4)))
        conn.close()
        check("the chart query never touches the table",
              "COVERING INDEX idx_telemetry_chart" in plan, plan[:120])

    print()
    if FAILED:
        print("FAILED:")
        for f in FAILED:
            print("  - " + f)
        return 1
    print("The fast history path returns what reading every row returned.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
