#!/usr/bin/env python3
"""
check_track.py - prove the circuit map puts turns and sectors where they are
=============================================================================
The map once drew "Chicane 15,16" on the start/finish straight and "Turn 12" on
a straight, because the positions were typed in and nothing compared them with
the tarmac. This compares them.

    python tools/check_track.py

  1. Every turn in TURN_START_TRACK_M is the start of a real bend on the OSM
     centreline: the heading changes after it, and not (in the same direction)
     just before it.
  2. With DOC_TO_TRACK_OFFSET_M applied, every turn the sector document lists
     for a sector (strategy_engine.Sections) starts inside that sector.
     Prints the whole window of offsets for which that holds.
  3. The map builder draws exactly those positions.

Exits non-zero on failure, like the other tools/check_*.py scripts.
"""

import math
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_REPO, os.path.join(_REPO, "Pit_Dashboard"), os.path.join(_REPO, "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

import track                                                      # noqa: E402
import track_map                                                  # noqa: E402
from strategy_engine import (DOC_TO_TRACK_OFFSET_M, SECTIONS_INFO,  # noqa: E402
                             Sections, TRACK_LANDMARKS, TURN_START_TRACK_M)
import build_zolder_animation as bza                              # noqa: E402

L = track.TRACK_LENGTH_METERS
FAILURES = []

# A bend must turn the car at least this much in the 40 m after its start...
MIN_TURN_DEG = 10.0
# ...and a start placed late, inside the bend, shows up as same-direction
# turning in the 25 m before it. Allowed: this much, or 40 % of the bend.
MAX_EARLY_DEG = 10.0


def check(name, ok, detail=""):
    print("  %-62s %s" % (name, "OK" if ok else "FAIL"))
    if detail:
        print("      %s" % detail)
    if not ok:
        FAILURES.append(name)


def heading_change(a, b):
    """Signed degrees the direction of travel turns from a to b (+ = left)."""
    def hdg(d):
        ax, ay = track_map.position_at_distance(d - 5)
        bx, by = track_map.position_at_distance(d + 5)
        return math.atan2(by - ay, bx - ax)
    x = hdg(b) - hdg(a)
    return math.degrees((x + math.pi) % (2 * math.pi) - math.pi)


def in_range(d, lo, hi):
    """d inside [lo, hi) on the lap, with wrapping."""
    d, lo, hi = d % L, lo % L, hi % L
    return lo <= d < hi if lo < hi else (d >= lo or d < hi)


print("1. Every turn start is where a real bend begins")
for turn, start in sorted(TURN_START_TRACK_M.items()):
    after = heading_change(start, start + 40)
    sign = 1.0 if after > 0 else -1.0
    before = heading_change(start - 25, start) * sign
    ok = (abs(after) >= MIN_TURN_DEG
          and before <= max(MAX_EARLY_DEG, 0.4 * abs(after)))
    check("T%-2d starts at %4d m" % (turn, start), ok,
          "turns %+.0f deg (%s) in the next 40 m, %+.0f deg same way in the 25 m before"
          % (after, "left" if after > 0 else "right", before))

print("\n2. Each turn is inside the sector the document puts it in")
pairs = [(seg["segment_id"], t) for seg in Sections for t in seg["turns"]]
for sid, turn in pairs:
    lo, hi = SECTIONS_INFO[sid]["range"]
    lo_t, hi_t = lo - DOC_TO_TRACK_OFFSET_M, hi - DOC_TO_TRACK_OFFSET_M
    start = TURN_START_TRACK_M[turn]
    check("T%-2d in S%d" % (turn, sid), in_range(start, lo_t, hi_t),
          "starts %d m; S%d is %.0f-%.0f m on the track (%d-%d m in the document)"
          % (start, sid, lo_t % L, hi_t % L, lo, hi))

good = [o for o in range(0, 400)
        if all(in_range(TURN_START_TRACK_M[t],
                        SECTIONS_INFO[s]["range"][0] - o,
                        SECTIONS_INFO[s]["range"][1] - o) for s, t in pairs)]
window = "%d-%d m" % (good[0], good[-1]) if good else "none"
check("offset %.0f m is inside the window that fits every turn"
      % DOC_TO_TRACK_OFFSET_M, DOC_TO_TRACK_OFFSET_M in good,
      "offsets that fit: %s" % window)

print("\n3. The map draws those positions")
data = bza.build_data()
drawn = {s["id"]: s["start"] for s in data["sectors"]}
want = {sid: (SECTIONS_INFO[sid]["range"][0] - DOC_TO_TRACK_OFFSET_M) % L
        for sid in SECTIONS_INFO}
check("sector starts", all(abs(drawn[s] - want[s]) < 0.01 for s in want),
      ", ".join("S%d %.0f" % (s, drawn[s]) for s in sorted(drawn)))
by_name = {lm["name"]: lm["dist"] for lm in data["landmarks"]}
bad = [lm["name"] for lm in TRACK_LANDMARKS
       if lm.get("turn") is not None
       and abs(by_name.get(lm["name"], -1) - TURN_START_TRACK_M[lm["turn"]]) > 0.01]
check("turn labels sit on their turn starts", not bad,
      "misplaced: %s" % bad if bad else "")
drawn_turns = {t["n"]: t["dist"] for t in data["turns"]}
check("all 16 turns are numbered on the map",
      sorted(drawn_turns) == list(range(1, 17))
      and all(drawn_turns[n] == TURN_START_TRACK_M[n] for n in drawn_turns),
      "drawn: %s" % sorted(drawn_turns))

print()
if FAILURES:
    print("FAILED (%d):" % len(FAILURES))
    for f in FAILURES:
        print("  - %s" % f)
    sys.exit(1)
print("All track checks passed.")
