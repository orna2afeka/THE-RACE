#!/usr/bin/env python3
"""
check_gate.py - prove the finish gate is crossed once a lap, forwards, by the
track AND by the pit lane, and by nothing else
=============================================================================
The car counts a lap each time its GPS path crosses track.py's gate in the
racing direction. That is only right if the gate is drawn right, and a gate is
easy to draw wrong: Zolder's start straight has another straight 79 m beside it
running the other way, and a gate long enough to touch it is crossed BACKWARDS
every lap.

    python tools/check_gate.py

  1. The gate's heading is the centreline's own tangent at the line.
  2. Walking the whole centreline crosses the gate exactly once, forwards.
  3. Walking the pit lane crosses it exactly once, forwards.
  4. Every other part of the lap stays clear of the gate by a GPS error's worth.

Run it after changing anything in track.py's gate block, and ALWAYS after
entering surveyed gate ends. Exits non-zero on failure, like the other
tools/check_*.py scripts.
"""

import math
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import track                                                      # noqa: E402
import track_map                                                  # noqa: E402

# A fix on any other piece of tarmac must not be able to reach the gate.
MIN_CLEARANCE_M = 25.0
MAX_HEADING_ERROR_DEG = 3.0
# Centreline within this far of the line ALONG the lap is the start straight.
SAME_STRAIGHT_M = 200.0

FAILURES = []


def check(ok, text):
    print(f"  {'ok  ' if ok else 'FAIL'}  {text}")
    if not ok:
        FAILURES.append(text)


def crossings(points, closed):
    out = []
    n = len(points)
    for i in range(n if closed else n - 1):
        hit = track.segment_gate_intersection(points[i], points[(i + 1) % n])
        if hit:
            out.append((i, hit))
    return out


def main():
    (bx, by), (ex, ey), (fx, fy), length, finish_w = track._GATE
    surveyed = bool(track.GATE_LEFT_LATLON and track.GATE_RIGHT_LATLON)
    print(f"gate: {length:.1f} m, {length - finish_w:.1f} m left / "
          f"{finish_w:.1f} m right of the finish point "
          f"({'SURVEYED ends' if surveyed else 'derived from FINISH_HEADING_DEG'})")

    # 1. heading
    tx, ty = track_map.tangent_at(0.0)
    err = math.degrees(math.acos(max(-1.0, min(1.0, tx * fx + ty * fy))))
    check(err <= MAX_HEADING_ERROR_DEG,
          f"gate faces the racing direction: {err:.2f} deg off the centreline "
          f"tangent ({math.degrees(math.atan2(tx, ty)) % 360:.2f} deg)")

    # 2. the lap
    lap = crossings(track_map.CENTRELINE_XY, closed=True)
    check(len(lap) == 1 and lap[0][1][2] == +1,
          f"the lap crosses the gate once, forwards: "
          f"{[(i, h[2]) for i, h in lap]} (segment index, direction)")
    if lap:
        check(abs(lap[0][1][1]) <= 5.0,
              f"... {abs(lap[0][1][1]):.2f} m from the finish point")

    # 3. the pit lane
    if track_map.PIT_ZONE_ENABLED:
        pit = crossings(track_map.PITLANE_XY, closed=False)
        check(len(pit) == 1 and pit[0][1][2] == +1,
              f"the pit lane crosses the gate once, forwards: "
              f"{[(i, h[2]) for i, h in pit]}")
        if pit:
            lateral = pit[0][1][1]
            room = min(lateral + finish_w, (length - finish_w) - lateral)
            check(room >= 10.0,
                  f"... {abs(lateral):.1f} m {'right' if lateral < 0 else 'left'} "
                  f"of the finish point, {room:.1f} m inside the gate's end")
    else:
        print("  --    no zolder_pitlane.py: pit-lane crossing not checked")

    # 4. clearance from everything else
    ends = ((bx, by), (bx + length * ex, by + length * ey))
    n = len(track_map.CENTRELINE_XY)
    L = track.TRACK_LENGTH_METERS
    worst = (math.inf, None)
    for i in range(n):
        # The straight the gate stands on is not "another piece of track".
        near = min(track_map.CUM_M[i], L - track_map.CUM_M[i + 1])
        if near < SAME_STRAIGHT_M:
            continue
        a, b = track_map.CENTRELINE_XY[i], track_map.CENTRELINE_XY[(i + 1) % n]
        # segment-to-segment distance: the gate is short and straight, so
        # sampling it every metre is exact enough and obviously correct.
        steps = max(1, int(length))
        for k in range(steps + 1):
            gx = ends[0][0] + (ends[1][0] - ends[0][0]) * k / steps
            gy = ends[0][1] + (ends[1][1] - ends[0][1]) * k / steps
            d = track_map._nearest_on_segment(gx, gy, a[0], a[1], b[0], b[1])[0]
            if d < worst[0]:
                worst = (d, i)
    check(worst[0] >= MIN_CLEARANCE_M,
          f"nearest OTHER piece of track is {worst[0]:.1f} m from the gate "
          f"(segment {worst[1]}, s = {track_map.CUM_M[worst[1]]:.0f} m; "
          f"need >= {MIN_CLEARANCE_M:.0f})")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED")
        return 1
    print("gate OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
