#!/usr/bin/env python3
"""
check_lap_split.py - a double lap the pit has split, and the lap distance
=======================================================================
    python tools/check_lap_split.py

No car, no store: the lap builder's split step and the distance rule are both
pure, so they are driven with made-up laps.

  SPLIT   a missed cut publishes two laps as one (Zolder, 2026-09-20 02:07:
          "lap 114", 629 s, 7760 m). The stored split must turn it into two
          laps that ADD UP to what the car published, keep the car's entry for
          the second half, drop its distance_suspect flag, and leave every
          other lap alone. A split for a lap that is not in the list does
          nothing. The first half is the pit's (lap_source "restored"), which
          is what makes fetch_laps number the laps after it correctly.
  DISTANCE  a person cuts every lap, so the car's lap distance runs until
          somebody presses Cut lap. The pit shows that number AS IT IS -- it is
          never pinned (that is how it read 3999 m for a whole lap) and the
          readout is never folded -- while the POSITION the map, the sector and
          the target speed use is folded the way the car folds it. Past the
          400 m a late press can take, it says a cut was missed. And the car
          itself must ship with both automatic cutters off.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_ROOT, os.path.join(_ROOT, "Pit_Dashboard")):
    if p not in sys.path:
        sys.path.insert(0, p)

import db                                                        # noqa: E402

FAILED = []


def check(label, ok, detail=""):
    print("  %-58s %s" % (label, "OK" if ok else "FAIL"))
    if detail:
        print("       " + detail)
    if not ok:
        FAILED.append(label)


def lap(n, seq, ts, t, m, wh, flags=()):
    return {"lap": n, "seq": seq, "finished_ts": ts, "lap_time_s": t,
            "distance_m": m, "energy_wh": wh, "regen_wh": 10.0,
            "lap_source": "manual", "kind": "start", "flags": list(flags),
            "stopped_s": 0.0}


laps = [lap(113, 26, 1000.0, 296.8, 4050.0, 98.2),
        lap(114, 27, 5479.0, 629.3, 7760.0, 176.4, ["manual_end", "distance_suspect"]),
        lap(116, 28, 5789.0, 310.0, 4060.0, 90.6)]
split = {"seq": 27, "finished_ts": 5479.0,
         "a": {"finished_ts": 5171.5, "lap": 114, "kind": "out", "lap_time_s": 321.8,
               "distance_m": 3715.0, "energy_wh": 84.3, "regen_wh": 4.0},
         "b": {"lap_time_s": 307.5, "distance_m": 4045.0, "energy_wh": 92.1, "regen_wh": 6.0}}

print("SPLIT")
out = db._apply_splits([dict(x, flags=list(x["flags"])) for x in laps], [split])
out.sort(key=lambda x: x["finished_ts"])
check("three laps become four", len(out) == 4, "%d laps" % len(out))
a = next(x for x in out if x["seq"] is None)
b = next(x for x in out if x["seq"] == 27)
check("the halves add up to what the car published",
      abs(a["lap_time_s"] + b["lap_time_s"] - 629.3) < 0.05
      and abs(a["energy_wh"] + b["energy_wh"] - 176.4) < 0.05,
      "%.1f s, %.1f Wh" % (a["lap_time_s"] + b["lap_time_s"], a["energy_wh"] + b["energy_wh"]))
check("the second half keeps the car's entry and finish time",
      b["finished_ts"] == 5479.0 and b["lap_source"] == "manual")
check("      and loses distance_suspect, gains split_by_pit",
      "distance_suspect" not in b["flags"] and "split_by_pit" in b["flags"]
      and "manual_end" in b["flags"], str(b["flags"]))
check("the first half is the pit's, ends at the line, before the second",
      a["lap_source"] == "restored" and a["flags"] == ["split_by_pit"]
      and a["finished_ts"] < b["finished_ts"] and a["kind"] == "out")
others = [x for x in out if x["seq"] in (26, 28)]
check("every other lap is untouched",
      others == [x for x in laps if x["seq"] in (26, 28)])
same = db._apply_splits([dict(x, flags=list(x["flags"])) for x in laps],
                        [dict(split, seq=99)])
check("a split for a lap that is not listed does nothing", same == laps)
late = db._apply_splits([dict(x, flags=list(x["flags"])) for x in laps], [split],
                        since_ts=5300.0)
check("a window that starts after the line still corrects the second half",
      len(late) == 3 and next(x for x in late if x["seq"] == 27)["distance_m"] == 4045.0)

print("DISTANCE")
from Pit_Web import api                                          # noqa: E402
import constants as C                                            # noqa: E402
import track                                                     # noqa: E402
L, G = C.TRACK_LENGTH_METERS, api.LAP_LATE_CUT_GRACE_M


def shown(raw_m):
    """The rule build_live applies, restated: (position, readout, overrun)."""
    return raw_m % L, raw_m, (raw_m - L if raw_m >= L + G else None)


# The rule lives in build_live, which needs a store to call. What is checked
# here is that build_live still CONTAINS it, and that the rule does what the
# comment there says -- so an edit to one without the other fails.
src = open(os.path.join(_ROOT, "Pit_Web", "api.py"), encoding="utf-8").read()
check("build_live folds the position and serves the raw readout",
      "lap_dist = raw_m % C.TRACK_LENGTH_METERS" in src
      and "lap_dist_raw = raw_m" in src
      and '"lapDistanceRawM"' in src and '"lapOverrunM": lap_overrun_m' in src)
check("and pins nothing, anywhere",
      "C.TRACK_LENGTH_METERS - 1.0)" not in src)
check("a normal lap is shown as it is", shown(2500.0) == (2500.0, 2500.0, None))
check("past a lap the READOUT keeps counting and the position folds",
      shown(L + 150.0) == (150.0, L + 150.0, None)
      and shown(7750.0)[:2] == (7750.0 - L, 7750.0), "7750 m -> %s" % (shown(7750.0),))
check("a late press is not called a missed one; a missed one is",
      shown(L + G - 1.0)[2] is None and shown(L + G)[2] == G)
check("the car cuts no lap by itself: both switches are off",
      track.CUT_LAP_ON_GATE is False and track.CUT_LAP_ON_DISTANCE is False)

if FAILED:
    print("\n%d FAILED: %s" % (len(FAILED), FAILED))
    sys.exit(1)
print("\nAll lap-split checks passed.")
