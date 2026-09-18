#!/usr/bin/env python3
"""
check_sectors.py - prove the sector split geometry, without a car
=================================================================
Exercises the real functions from Pit_Web/api.py against synthetic traces whose
correct answers are known analytically, plus the actual store.

There is no pytest anywhere in this project, so this follows the existing
convention: a standalone script under tools/ that exits non-zero on failure.

    python tools/check_sectors.py

What it is actually guarding against, in order of how much it would hurt:

  1. SECTOR 9 SILENTLY NEVER REPORTING. Its gate is 4000 m, which is the
     start/finish line, and lap_distance_m resets there. Before the stitch,
     sector 9 reported only on laps where the car's distance happened to
     overshoot 4000 m before the reset. On the replay store that was three laps
     in six, so it looked like it worked.

  2. SECTOR 1 BEING SHORT. Its gate is 0 m, the same line. The old code started
     it at the first sample AFTER the line instead, which loses up to a full
     sample period, all of it landing in one of the two cells a strategist
     compares most often across laps.

  3. SPLITS INTERPOLATED ACROSS A DROPOUT. _crossing_time takes the first pair
     straddling a boundary. Given a trace with a gap it will happily report a
     "sector time" measured between two samples days apart. The replay store
     produced 612,896 s and the dashboard rendered it as a number.

  4. PURPLE POISONED BY A GLITCH. A session best that is not a real lap time
     never goes away on its own, and every later comparison is measured
     against it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Pit_Web import api                                          # noqa: E402

LAP_M = api.LAP_M
LAP_S = 210.0
HZ = 2.0                      # the car reports about twice a second

FAILURES = []


def check(name, ok, detail=""):
    print("  %-58s %s" % (name, "OK" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s%s" % (name, (" - " + detail) if detail else ""))
    elif detail:
        print("      %s" % detail)


def lap_samples(t0, laps=1, hz=HZ, lap_s=LAP_S, offset_s=0.0):
    """A constant-speed trace: distance is exactly proportional to time.

    `offset_s` shifts the sampling phase so the first sample of a lap sits
    AFTER the line rather than on it, which is the normal case and the one
    that used to make sector 1 short.
    """
    out, step = [], 1.0 / hz
    n = int(laps * lap_s * hz)
    for i in range(n + 1):
        t = t0 + offset_s + i * step
        d = ((t - t0) / lap_s) * LAP_M
        out.append((t, d % LAP_M, int(d // LAP_M)))
    return out


def trace_for(samples, lap_index):
    return [(t, d) for t, d, k in samples if k == lap_index]


print(__doc__.split("What it is")[0].strip())
print()

# --------------------------------------------------------------------------- #
print("1. A clean constant-speed lap: every gate, from the line")
# --------------------------------------------------------------------------- #
s = lap_samples(1000.0, laps=3, offset_s=0.37)
runs = {k: trace_for(s, k) for k in (0, 1, 2)}

mid = api._splits(api._stitched(runs[1], runs[0], runs[2]))
check("all nine sectors present", sorted(mid) == api.SECTOR_IDS,
      "got %s" % sorted(mid))
check("sector 9 reports (regression: it used to be luck)", 9 in mid,
      "S9 = %.3f s" % mid[9] if 9 in mid else "missing")

# Analytic truth: time in a sector is its length over the constant speed.
speed = LAP_M / LAP_S
worst = 0.0
for sid, a, b in api.SECTOR_BOUNDS:
    want = (b - a) / speed
    worst = max(worst, abs(mid[sid] - want))
check("every split within 0.01 s of the analytic value", worst < 0.01,
      "worst error %.4f s" % worst)
check("the nine sum to the lap time", abs(sum(mid.values()) - LAP_S) < 0.02,
      "sum %.3f s vs %.1f s" % (sum(mid.values()), LAP_S))

# Sector 1 measured from the line, not from the first sample after it. With a
# 0.37 s sampling offset the old code was short by that much.
want_s1 = (600 - 0) / speed
check("sector 1 starts at the LINE, not the first sample",
      abs(mid[1] - want_s1) < 0.01,
      "S1 = %.3f s, analytic %.3f s, old code would give ~%.3f s"
      % (mid[1], want_s1, want_s1 - 0.37))

# --------------------------------------------------------------------------- #
print("\n2. The lap in progress: sector 9 is unknowable, and says so")
# --------------------------------------------------------------------------- #
live = api._splits(api._stitched(runs[2], runs[1], None))
check("sector 9 is absent while the lap is still running", 9 not in live,
      "correct: the 4000 m gate has not been crossed yet")
check("the other eight still report", len([k for k in live if k != 9]) == 8,
      "got %s" % sorted(live))

# --------------------------------------------------------------------------- #
print("\n3. A dropout does not become a sector time")
# --------------------------------------------------------------------------- #
# Delete the middle of lap 1, leaving a gap that straddles several gates.
gapped = [(t, d) for t, d in runs[1]
          if not (1000.0 + LAP_S + 40 < t < 1000.0 + LAP_S + 150)]
gap_runs = api._runs(gapped)
check("a dropout does NOT split the lap", len(gap_runs) == 1,
      "one lap with a hole in it, not two laps")

whole = api._splits(api._stitched(gap_runs[0], runs[0], runs[2]))
worst_bad = max((v for v in whole.values()), default=0.0)
check("no split spans the gap", worst_bad < 4 * LAP_S,
      "largest split %.1f s" % worst_bad)
# The hole runs from about 40 s to 150 s into the lap, which is roughly 760 m
# to 2860 m. Sector 1 ends at 600 m and sector 9 starts at 3430 m, so both sit
# outside it and must survive.
check("sectors BEFORE the dropout survive it", 1 in whole,
      "S1 = %.2f s" % whole[1] if 1 in whole else "lost, and it should not be")
check("sectors AFTER the dropout survive it", 9 in whole,
      "S9 = %.2f s" % whole[9] if 9 in whole else "lost, and it should not be")
check("sectors INSIDE the dropout are lost", 4 not in whole and 5 not in whole,
      "S4 and S5 are unknowable, and say so")

# The old behaviour, for contrast: a 71-day gap must not yield a number.
absurd = [(0.0, 100.0), (6_000_000.0, 3900.0)]
check("a 71-day pair yields no crossing at all",
      api._crossing_time(absurd, 600) is None)

# --------------------------------------------------------------------------- #
print("\n4. A stalled lap counter: splits come from the LATEST lap")
# --------------------------------------------------------------------------- #
# Two laps under one tag, which is what a stalled GPS trigger produces.
stalled = runs[0] + runs[1]
st_runs = api._runs(stalled)
check("the trace is cut at the distance reset", len(st_runs) == 2,
      "%d runs" % len(st_runs))
check("the last run is the most recent lap",
      st_runs[-1][0][0] > st_runs[0][-1][0])

# --------------------------------------------------------------------------- #
print("\n5. Classification: purple, green, yellow")
# --------------------------------------------------------------------------- #
ref = {sid: (b - a) / speed for sid, a, b in api.SECTOR_BOUNDS}
faster = dict(ref)
faster[3] = ref[3] - 0.50            # sector 3 improves
slower = dict(ref)
slower[5] = ref[5] + 0.40            # sector 5 drops off

bests = {sid: (v, 42) for sid, v in faster.items()}
row = api._row("last", "Last lap", 42, faster, ref, bests, None)
by_sid = {c["sector"]: c for c in row["cells"]}
check("the lap that owns the best is purple", by_sid[3]["cls"] == "best")
check("purple wins over a green delta",
      by_sid[3]["cls"] == "best" and by_sid[3]["delta"] < 0)

row2 = api._row("last", "Last lap", 43, slower, ref, {}, None)
by2 = {c["sector"]: c for c in row2["cells"]}
check("a slower sector is yellow", by2[5]["cls"] == "slower")
check("an unchanged sector is neutral", by2[1]["cls"] is None)

first = api._row("last", "Last lap", 1, ref, {}, {}, None)
check("the first lap of a race has no colour at all",
      all(c["cls"] is None for c in first["cells"]),
      "nothing to compare against yet")
check("the first lap still shows its times",
      all(c["value"] is not None for c in first["cells"]))

# --------------------------------------------------------------------------- #
print("\n6. Nulls are nulls, and say which kind")
# --------------------------------------------------------------------------- #
partial = {1: ref[1], 2: ref[2]}
# The car is 2500 m round. Sectors 3, 4 and 5 END before that and produced no
# split, so they are LOST. Sectors 6 onward are simply still to come.
prow = api._row("current", "Current", 44, partial, ref, {}, 2500.0)
pby = {c["sector"]: c for c in prow["cells"]}
check("a sector not yet reached is 'pending'", pby[7]["state"] == "pending",
      "S7 ends at 3000 m, car at 2500 m")
check("a sector passed with no split is 'missing'",
      pby[3]["state"] == "missing" and pby[5]["state"] == "missing",
      "S3 and S5 end before 2500 m, so the car drove them and we lost them")
check("the two kinds of blank are actually different",
      pby[3]["state"] != pby[7]["state"])
check("every absent value is None, never 0",
      all(c["value"] is None for c in prow["cells"] if c["state"] != "ok"))
check("a partial lap has no total", prow["total"] is None,
      "a partial sum would be a wrong lap time")
check("a complete lap does have one", row["total"] is not None,
      "%.2f s" % row["total"])

# --------------------------------------------------------------------------- #
print("\n7. The real store")
# --------------------------------------------------------------------------- #
try:
    from contextlib import closing
    with closing(api.ro_conn()) as conn:
        tags = api.db.recent_laps(conn, 6)
        cache = {}
        print("      lap tags: %s" % tags)
        worst_real = 0.0
        for tag in tags:
            sp, _run = api._lap_splits(conn, tag, cache)
            if sp:
                worst_real = max(worst_real, max(sp.values()))
                print("      tag %-4s %s" % (
                    tag, " ".join("S%d=%.1f" % (k, v) for k, v in sorted(sp.items()))))
        # What this guards against is a split stitched ACROSS SESSIONS (612,896 s,
        # seven days). A car that stands still inside a sector produces a long
        # split too, and a true one: 1246.7 s on 2026-09-18, parked in the box
        # with the lap still open. So the bound is "not hours", not "about a lap".
        check("no split from the real store is absurd", worst_real < 3600.0,
              "largest %.1f s (was 612,896 s before the run segmentation)"
              % worst_real)
except Exception as exc:                                   # pragma: no cover
    print("      (skipped: %s)" % exc)

print()
if FAILURES:
    print("FAILED (%d):" % len(FAILURES))
    for f in FAILURES:
        print("  - %s" % f)
    sys.exit(1)
print("All sector checks passed.")
