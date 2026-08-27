"""
track_map.py — plan-view geometry for Circuit Zolder
=====================================================
Turns the baked centreline (zolder_centreline.py) into the things a map needs:
a point for any distance-along-lap, the polyline split into coloured runs, tick
marks at the boundaries, and a "are we actually at Zolder?" test.

Lives beside track.py, and for the same reason: these are facts about the
circuit, shared rather than owned by whatever draws them. There is no Streamlit,
pandas, plotly or network here, so it can be exercised headless —

    python track_map.py

— and a driver-HUD mini-map could import it unchanged.

IT KNOWS NOTHING ABOUT SECTORS, DELIBERATELY.
split_at() and boundary_ticks() take boundaries as an argument. The nine-sector
model belongs to the pit (strategy_engine.SECTIONS_INFO) and the driver HUD does
not have it, so baking it in here would make this module unusable by half its
audience and would put a pit concept in a file about tarmac.

WHY METRES AND NOT LAT/LON
Everything below is in track.to_local_xy metres, origin at the finish line.
cos(50.989 deg) = 0.629, so a degree of longitude at Zolder is not quite
two-thirds of a degree of latitude: plotted raw, the circuit's 1133 x 1320 m
footprint renders as 713 x 1320 and every corner radius is wrong. Projecting
once here is cheaper and less error-prone than remembering to fix the aspect
ratio at each call site — and hypot(x, y) is then the distance to the finish
line for free.
"""

import bisect
import math

from track import (FINISH_LINE_LAT, FINISH_LINE_LON, TRACK_LENGTH_METERS,
                   haversine_metres, to_local_xy)
from zolder_centreline import CENTRELINE_LATLON, LABEL_SIDE, OSM_ATTRIBUTION

__all__ = [
    "CENTRELINE_XY", "CUM_M", "BOUNDS_XY", "LABEL_SIDE", "OSM_ATTRIBUTION",
    "position_at_distance", "split_at", "boundary_ticks", "is_at_zolder",
    "ZOLDER_GEOFENCE_M",
]

# How close a REAL fix has to be to the finish line before we will believe the
# car is at Zolder.
#
# The circuit's own footprint is about 1133 x 1320 m and its farthest point is
# ~950 m from the finish line, so 1 km already contains the track — but nothing
# of the paddock, the access roads or the field the trailer is parked in. 5 km
# contains the whole venue and everything a race weekend touches.
#
# The value is not delicate. It only has to separate "at the circuit" from "in
# Israel", and those are 3,000 km apart — six hundred times this radius. Erring
# generous is the right direction: the damaging failure is refusing to show a
# real position, not showing a real position from the car park.
ZOLDER_GEOFENCE_M = 5000.0

# The centreline in metres, and the distance along the lap at each vertex.
#
# CUM_M is RESCALED so its last entry is exactly TRACK_LENGTH_METERS. The OSM
# centreline measures 4001.34 m against the team's 4000.0 model — 1.34 m over a
# lap, 0.034 %, about a quarter of a pixel on this chart and far inside one GPS
# fix's error. (Zolder's homologated 4011 m is a third number and a different
# quantity again: it is measured along the ideal racing line, not the centre.)
#
# The rescale is not really about accuracy at that size. It is so that
# position_at_distance(4000) lands on exactly position_at_distance(0), which is
# what makes the loop close and the S1 tick sit on the finish line to the bit.
#
# Do NOT instead move the dashboard onto 4001.34: TRACK_LENGTH_METERS is
# load-bearing for lap detection on the Pi, the sector strip, the sector-time
# splits and every generated velocity profile.
CENTRELINE_XY = tuple(to_local_xy(la, lo) for la, lo in CENTRELINE_LATLON)


def _cumulative():
    cum = [0.0]
    for i in range(1, len(CENTRELINE_XY)):
        ax, ay = CENTRELINE_XY[i - 1]
        bx, by = CENTRELINE_XY[i]
        cum.append(cum[-1] + math.hypot(bx - ax, by - ay))
    ax, ay = CENTRELINE_XY[-1]
    bx, by = CENTRELINE_XY[0]
    cum.append(cum[-1] + math.hypot(bx - ax, by - ay))     # the closing segment
    scale = TRACK_LENGTH_METERS / cum[-1]
    return tuple(c * scale for c in cum)


CUM_M = _cumulative()

# (x0, x1, y0, y1) padded, for a fixed plot range. Computed here so the chart
# never runs an autorange pass and — more useful — so its axes are byte-identical
# on every redraw, which is what makes a two-second refresh look like a moving
# dot rather than a flickering chart.
_PAD_M = 40.0
BOUNDS_XY = (
    min(p[0] for p in CENTRELINE_XY) - _PAD_M,
    max(p[0] for p in CENTRELINE_XY) + _PAD_M,
    min(p[1] for p in CENTRELINE_XY) - _PAD_M,
    max(p[1] for p in CENTRELINE_XY) + _PAD_M,
)


def position_at_distance(d_m):
    """(x, y) in metres for a distance-along-lap. Wraps, so any float is valid.

    The single primitive behind the car marker, the boundary ticks and the run
    splitting — deliberately, so there is one interpolation in this file and not
    three subtly different ones.
    """
    d = float(d_m) % TRACK_LENGTH_METERS
    i = bisect.bisect_right(CUM_M, d) - 1
    i = max(0, min(i, len(CENTRELINE_XY) - 1))
    span = CUM_M[i + 1] - CUM_M[i]
    t = 0.0 if span <= 0 else (d - CUM_M[i]) / span
    ax, ay = CENTRELINE_XY[i]
    bx, by = CENTRELINE_XY[(i + 1) % len(CENTRELINE_XY)]
    return ax + t * (bx - ax), ay + t * (by - ay)


def split_at(boundaries):
    """Split the lap into runs between `boundaries`, in order.

    Returns [(start_m, end_m, xs, ys), ...], one per interval, wrapping the last
    boundary back round to the first.

    Each run INCLUDES both its endpoints, and those endpoints are computed once
    and shared with the neighbouring runs rather than interpolated twice. Doing
    it per-run instead leaves the two boundary points differing in the last float
    bit, which Plotly draws as a one-pixel notch at every boundary — a rendering
    artefact that does not reproduce on a different screen and costs an afternoon
    to chase.
    """
    bounds = sorted(float(b) % TRACK_LENGTH_METERS for b in boundaries)
    if not bounds:
        return []
    pins = {b: position_at_distance(b) for b in bounds}

    runs = []
    for k, start in enumerate(bounds):
        end = bounds[(k + 1) % len(bounds)]
        span_end = end if end > start else end + TRACK_LENGTH_METERS

        xs, ys = [pins[start][0]], [pins[start][1]]
        for i, c in enumerate(CUM_M[:-1]):
            if start < c < span_end:
                xs.append(CENTRELINE_XY[i][0])
                ys.append(CENTRELINE_XY[i][1])
            elif start < c + TRACK_LENGTH_METERS < span_end:   # past the wrap
                xs.append(CENTRELINE_XY[i][0])
                ys.append(CENTRELINE_XY[i][1])
        xs.append(pins[end][0])
        ys.append(pins[end][1])
        runs.append((start, end, xs, ys))
    return runs


def tangent_at(d_m, chord_m=4.0):
    """Unit direction of travel at a distance-along-lap.

    Sampled across a +/-chord_m chord rather than from the adjacent vertices,
    because vertex spacing here runs from 1.7 m to 218 m: on the start/finish
    straight the neighbouring vertices are useless, and inside a hairpin they
    disagree with each other. A short chord always gives a sensible local
    tangent.
    """
    ax, ay = position_at_distance(d_m - chord_m)
    bx, by = position_at_distance(d_m + chord_m)
    dx, dy = bx - ax, by - ay
    n = math.hypot(dx, dy) or 1.0
    return dx / n, dy / n


def boundary_ticks(boundaries, half_len_m=14.0):
    """Perpendicular gate marks across the track at each boundary.

    Returns [(distance_m, (x0, y0), (x1, y1), (nx, ny)), ...] — the two ends of
    the tick and the unit normal, so a caller can hang a label off the same
    normal without recomputing it.

    28 m across by default: roughly 2.5x the track width, so it reads as a
    marshal's gate rather than a hair on the line.
    """
    ticks = []
    for b in boundaries:
        px, py = position_at_distance(b)
        tx, ty = tangent_at(b)
        nx, ny = -ty, tx                      # left of travel
        ticks.append((b,
                      (px - half_len_m * nx, py - half_len_m * ny),
                      (px + half_len_m * nx, py + half_len_m * ny),
                      (nx, ny)))
    return ticks


def is_at_zolder(lat, lon, radius_m=ZOLDER_GEOFENCE_M):
    """Is this position at the circuit?

    CALLERS MUST CHECK has_gps FIRST. The pit dashboard substitutes the Zolder
    paddock (50.9895, 5.2568) whenever the car reports no fix, and that
    placeholder is 172 m from the finish line — comfortably inside any sensible
    geofence. Asking this question before checking whether the position is real
    means a dead GPS paints a confident car on the grid, which is the worst
    answer available.

    haversine, not to_local_xy: track.py scopes its equirectangular projection to
    "the +/-200 m that matters here", and this is asked of points 3,000 km away.
    """
    if lat is None or lon is None:
        return False
    return haversine_metres(float(lat), float(lon),
                            FINISH_LINE_LAT, FINISH_LINE_LON) <= radius_m


def distance_from_zolder_m(lat, lon):
    """Great-circle metres from the finish line, or None. For captions."""
    if lat is None or lon is None:
        return None
    return haversine_metres(float(lat), float(lon),
                            FINISH_LINE_LAT, FINISH_LINE_LON)


# ---------------------------------------------------------------------------
# Self-check — run this after regenerating zolder_centreline.py:
#
#     python track_map.py
#
# It proves the loop closes, the origin is the finish line, and the interpolator
# agrees with the stored vertices. If any of it fails, the baked file is bad and
# the map will be silently wrong rather than visibly broken.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"vertices          {len(CENTRELINE_XY)}")
    print(f"lap length        {CUM_M[-1]:.6f} m "
          f"(TRACK_LENGTH_METERS = {TRACK_LENGTH_METERS})")

    x0, y0 = position_at_distance(0.0)
    xl, yl = position_at_distance(TRACK_LENGTH_METERS)
    print(f"start/end gap     {math.hypot(xl - x0, yl - y0):.9f} m")

    fx, fy = to_local_xy(FINISH_LINE_LAT, FINISH_LINE_LON)
    print(f"origin vs finish  {math.hypot(x0 - fx, y0 - fy):.3f} m")

    worst = max(
        math.hypot(*(a - b for a, b in zip(position_at_distance(CUM_M[i]),
                                           CENTRELINE_XY[i])))
        for i in range(len(CENTRELINE_XY)))
    print(f"interp vs vertex  {worst:.9f} m (worst of {len(CENTRELINE_XY)})")

    w = BOUNDS_XY[1] - BOUNDS_XY[0]
    h = BOUNDS_XY[3] - BOUNDS_XY[2]
    print(f"bounding box      {w:.0f} x {h:.0f} m")
    print(f"attribution       {OSM_ATTRIBUTION}")
