"""
track.py — the circuit: finish line, lap length, and crossing geometry
======================================================================
Shared by the car (lap detection on the Pi) and the pit (track position,
strategy). Sits at the repo root next to drivetrain.py and for the same reason:
TRACK_LENGTH_METERS was previously written out in three separate places —
SolarRace_OS/main.py, Pit_Dashboard/constants.py, and a bare 4000.0 literal in
strategy_engine.get_live_track_status — which is exactly the pattern that let
the speed calculation drift apart between the two dashboards.

WHY THE FINISH LINE NEEDS A SWEPT-SEGMENT TEST, NOT A RADIUS TEST
gpsd emits roughly one fix per second, so at racing speed the car's path is only
sampled every 17 m (60 km/h) to 28 m (100 km/h). A plain "is this fix within R
metres of the line?" test misses the lap whenever the nearest fix lands farther
out than R, i.e. once the fix spacing exceeds 2*sqrt(R^2 - offset^2).

Worked through honestly for R = 25 m and a 5 m lateral offset, that threshold is
a 49 m spacing:

    clean 1 Hz             misses above ~176 km/h  -> never happens on this car
    ONE DROPPED FIX (2 s)  misses above  ~88 km/h  -> happens, and often

So the circle test is not broken by speed — it is broken by a *gap*. Losing the
odd fix is routine for GPS (obstruction, brief loss of lock, a busy CPU delaying
the reader), and a 2 s gap at racing speed doubles the effective spacing and
steps straight over the zone. Losing a lap for that reason would be silent and
essentially unreproducible.

Testing the STRAIGHT SEGMENT between consecutive fixes removes the failure mode
entirely: the path between two fixes is continuous, so a capsule of radius R
swept along it cannot be jumped no matter how large the gap. Two further
benefits: R can stay small (25 m, sized for GPS lateral error plus half the
track width) instead of being inflated to cover sampling, which sharpens
rejection of any adjacent piece of track; and the returned `t` gives the
fraction along the segment where the crossing actually happened, so lap times
can be interpolated rather than snapped to whichever fix landed inside.

    circle test              segment test
    fix1  •                  fix1  •
           ( ) <- missed            \
    fix2      •                      \  <- closest approach caught
                                fix2  •

(LapTracker refuses to build a segment across a gap longer than a few seconds —
past that the straight-line assumption stops being safe on a circuit.)
"""

import math

# --------------------------------------------------------------------------- #
# Circuit Zolder, Belgium
# --------------------------------------------------------------------------- #
FINISH_LINE_LAT = 50.989021980390824
FINISH_LINE_LON = 5.255727395757176

TRACK_LENGTH_METERS = 4000.0

# A crossing only counts as a lap when the distance travelled since the last
# trigger falls in this window. This is the cross-reference that makes the
# trigger trustworthy: GPS alone would fire in the pit lane or on an adjacent
# piece of track, and distance alone drifts.
#
# ⚠️ These bounds are ±5% of a lap, which is TIGHTER than the odometer's own
# calibration: drivetrain.TIRE_DIAMETER_METERS is still a placeholder that has
# never been measured, and a 2% tire error is a 2% distance error. If laps stop
# being detected, check the rejected-crossing log first — LapTracker prints the
# distance it actually measured at each rejected crossing, which tells you
# immediately whether the tire constant is wrong.
LAP_DISTANCE_MIN_M = 3800.0
LAP_DISTANCE_MAX_M = 4200.0

# Capture radius for the swept-segment test. Sized for GPS lateral error plus
# half the track width — NOT for the distance between fixes (see module docs).
FINISH_RADIUS_M = 25.0

# The car must get this far from the line before another crossing can be
# detected. Prevents one slow pass from registering twice; works together with
# the LAP_DISTANCE_MIN_M gate, which already makes a double-count physically
# impossible on a moving car.
FINISH_EXIT_RADIUS_M = 60.0

# If GPS is healthy but no crossing has been seen this far past a full lap,
# detection has failed (a closed circuit cannot be 400 m long). Force the lap so
# counting continues, and tag it so the pit can see the GPS trigger missed.
ODOMETER_FORCE_LAP_M = 4400.0

# --------------------------------------------------------------------------- #
# The finish GATE
# --------------------------------------------------------------------------- #
# A lap is one FORWARD passage of a line across the circuit, not a visit to a
# point. The line spans the racing surface AND the pit lane, because that is
# what the organisers' timing loop does: a car that leaves its box and drives
# down the pit lane past the line has completed a lap.
#
# Measured against the OSM geometry (tools/check_gate.py re-measures it):
#
#     pit lane centre     12-14 m to the RIGHT of the track centreline here
#     opposing track      79 m to the RIGHT, running the other way (s ~ 1640 m)
#
# so the gate reaches 30 m right (pit lane + its width + GPS error) and stops
# 49 m short of the opposing section. A gate long enough to touch that section
# would be crossed BACKWARDS once a lap. 20 m left covers half the track width
# plus GPS error; there is nothing but grandstand on that side.
#
# Direction of travel across the line, degrees clockwise from north. Baked
# rather than imported because track_map imports this module; check_gate.py
# fails if it drifts from the centreline's own tangent at s = 0.
FINISH_HEADING_DEG = 236.713
GATE_LEFT_M = 20.0
GATE_RIGHT_M = 30.0

# Surveyed gate ends, (lat, lon), LEFT end then RIGHT end as the driver sees
# them. None = derive the gate from FINISH_LINE_LAT/LON and the numbers above.
#
# CHECKED ON SITE, 2026-09-18. A pin dropped on the painted start/finish line at
# the pit wall (50.9891047, 5.2556404) lands 0.0 m along the track from
# FINISH_LINE_LAT/LON and 11 m to its right — on the derived gate, between the
# track and the pit lane, exactly where the wall is. So the derived gate stands
# and these stay None. The same survey put our box 35-40 m PAST the line
# (garage front 50.9890066, 5.2551452 .. 50.9889811, 5.2550858, 21 m right):
# coming in, the car passes the gate in the pit lane BEFORE it stops.
GATE_LEFT_LATLON = None
GATE_RIGHT_LATLON = None

# No car laps 4 km in a minute (240 km/h), so two counted crossings closer
# together than this are one crossing seen twice.
MIN_LAP_TIME_S = 60.0

# With a live odometer a counted crossing also needs this much distance behind
# it. Deliberately HALF a lap and not the 3800 m window: the gate's direction
# test is what rejects false crossings now, so this only has to stop a car that
# shuffles around the line, and must never reject a real lap because the tire
# constant is a few percent out.
MIN_LAP_DISTANCE_M = 2000.0

_EARTH_RADIUS_M = 6371008.8


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def to_local_xy(lat, lon):
    """Project a lat/lon to metres on a flat plane centred on the finish line.

    Equirectangular projection. Over the ±200 m that matters here its error is
    sub-millimetre, and unlike a full geodesic it is cheap enough to run on
    every GPS fix. The origin IS the finish line, so hypot(x, y) is the distance
    to it and no second haversine call is needed.
    """
    lat0 = math.radians(FINISH_LINE_LAT)
    x = math.radians(lon - FINISH_LINE_LON) * _EARTH_RADIUS_M * math.cos(lat0)
    y = math.radians(lat - FINISH_LINE_LAT) * _EARTH_RADIUS_M
    return (x, y)


def haversine_metres(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres. Used for checks and diagnostics."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def segment_min_distance(p1, p2):
    """Closest approach of the segment p1->p2 to the origin (the finish line).

    Returns (distance_m, t) where t in [0, 1] is how far along the segment the
    closest point lies — 0 at p1, 1 at p2. t is what lets a caller interpolate
    the moment of crossing between two GPS timestamps rather than attributing
    the lap to whichever fix happened to land inside the zone.

    Standard point-to-segment projection, clamped to the segment so a line that
    passes near the origin only *beyond* its endpoints is correctly reported at
    its nearer endpoint instead of at the infinite line's foot.
    """
    x1, y1 = p1
    x2, y2 = p2
    dx, dy = x2 - x1, y2 - y1
    seg_sq = dx * dx + dy * dy
    if seg_sq <= 0.0:                      # p1 == p2: degenerate, it's a point
        return math.hypot(x1, y1), 0.0
    t = -(x1 * dx + y1 * dy) / seg_sq
    t = max(0.0, min(1.0, t))
    cx, cy = x1 + t * dx, y1 + t * dy
    return math.hypot(cx, cy), t


def distance_to_finish(lat, lon):
    """Metres from a lat/lon to the finish line."""
    return math.hypot(*to_local_xy(lat, lon))


def _gate_frame():
    """(right_end_xy, left_unit, forward_unit, length_m, finish_offset_m).

    `left_unit` points along the gate from its right end to its left end;
    `forward_unit` is the direction a racing car crosses it. `finish_offset_m`
    is where the finish point sits along the gate, so a crossing's lateral can
    be reported relative to the track centreline rather than to a gate end.
    """
    if GATE_LEFT_LATLON and GATE_RIGHT_LATLON:
        ax, ay = to_local_xy(*GATE_LEFT_LATLON)
        bx, by = to_local_xy(*GATE_RIGHT_LATLON)
        length = math.hypot(ax - bx, ay - by)
        ex, ey = (ax - bx) / length, (ay - by) / length
        fx, fy = ey, -ex                  # left rotated -90 deg = forward
        return (bx, by), (ex, ey), (fx, fy), length, -(bx * ex + by * ey)
    h = math.radians(FINISH_HEADING_DEG)
    fx, fy = math.sin(h), math.cos(h)     # heading is clockwise from north
    ex, ey = -fy, fx                      # forward rotated +90 deg = left
    b = (-GATE_RIGHT_M * ex, -GATE_RIGHT_M * ey)
    return b, (ex, ey), (fx, fy), GATE_LEFT_M + GATE_RIGHT_M, GATE_RIGHT_M


_GATE = _gate_frame()


def gate_coords(p):
    """(along_m, lateral_m) of a local-xy point in the gate's own frame.

    along_m   > 0 past the line, < 0 before it, in the racing direction
    lateral_m > 0 left of the finish point, < 0 right of it (the pit side)
    """
    (bx, by), (ex, ey), (fx, fy), _length, finish_w = _GATE
    dx, dy = p[0] - bx, p[1] - by
    return dx * fx + dy * fy, dx * ex + dy * ey - finish_w


def segment_gate_intersection(p1, p2):
    """Does the path p1->p2 cross the finish gate?

    Returns None, or (t, lateral_m, sign):

    t          fraction along p1->p2 where it meets the line, so the moment,
               the odometer and the energy AT the line can be interpolated
               instead of being taken from whichever fix came after it
    lateral_m  where across the gate, left-positive from the finish point
    sign       +1 crossed in the racing direction, -1 crossed backwards

    A point exactly on the line belongs to the far side, so a path that ends on
    the line and the next one that starts on it cannot both report a crossing.
    """
    u1, v1 = gate_coords(p1)
    u2, v2 = gate_coords(p2)
    if (u1 < 0.0) == (u2 < 0.0):
        return None
    t = u1 / (u1 - u2)
    lateral = v1 + t * (v2 - v1)
    _b, _e, _f, length, finish_w = _GATE
    if not (-finish_w <= lateral <= length - finish_w):
        return None
    return t, lateral, (1 if u2 >= 0.0 else -1)


# --------------------------------------------------------------------------- #
# Self-check:  python3 track.py
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    print(f"Finish line: {FINISH_LINE_LAT}, {FINISH_LINE_LON}")
    print(f"Lap: {TRACK_LENGTH_METERS:.0f} m, "
          f"accept {LAP_DISTANCE_MIN_M:.0f}-{LAP_DISTANCE_MAX_M:.0f} m")
    print(f"Capture radius {FINISH_RADIUS_M:.0f} m, "
          f"re-arm beyond {FINISH_EXIT_RADIUS_M:.0f} m\n")

    # Projection sanity: 0.001 deg of latitude is ~111.2 m anywhere.
    d = haversine_metres(FINISH_LINE_LAT, FINISH_LINE_LON,
                         FINISH_LINE_LAT + 0.001, FINISH_LINE_LON)
    print(f"  0.001 deg latitude   = {d:7.2f} m   (expect ~111.2)")
    print(f"  projection agrees    = "
          f"{distance_to_finish(FINISH_LINE_LAT + 0.001, FINISH_LINE_LON):7.2f} m")

    # The case this module exists for: 100 km/h with ONE DROPPED FIX, so the
    # gap is 2 s / 55.6 m and no fix lands inside the 25 m zone.
    lat_per_m = 1.0 / 111_320.0

    def _pass(spacing_m, label, offset_m=5.0):
        prev = None
        circle_hits = segment_hits = 0
        print(f"\n  {label} ({spacing_m:.1f} m between fixes, "
              f"{offset_m:.0f} m lateral offset):")
        # Phase the samples so none lands on the line — the realistic case.
        for i in range(-2, 3):
            along = (i + 0.5) * spacing_m
            lat = FINISH_LINE_LAT + offset_m * lat_per_m
            lon = FINISH_LINE_LON + (along * lat_per_m
                                     / math.cos(math.radians(FINISH_LINE_LAT)))
            xy = to_local_xy(lat, lon)
            point_d = math.hypot(*xy)
            seg_d = point_d if prev is None else segment_min_distance(prev, xy)[0]
            circle_hits += point_d <= FINISH_RADIUS_M
            segment_hits += seg_d <= FINISH_RADIUS_M
            print(f"    {along:+7.1f} m along: point {point_d:6.1f} m"
                  f"   segment {seg_d:6.1f} m")
            prev = xy
        print(f"    -> circle {circle_hits} hit(s), segment {segment_hits} hit(s)")
        return circle_hits, segment_hits

    _pass(27.8, "clean 1 Hz at 100 km/h")
    c, s = _pass(55.6, "100 km/h with ONE DROPPED FIX (2 s gap)")
    print(f"\n    the dropped fix is the real failure: circle={c} (lap lost), "
          f"segment={s} (lap caught)")

    # The gate. Paths are built in the gate's own frame and mapped back, so
    # these hold for a surveyed gate as well as the derived one.
    (_bx, _by), (_ex, _ey), (_fx, _fy), _len, _fw = _GATE

    def _xy(along, lateral):
        w = lateral + _fw
        return (_bx + w * _ex + along * _fx, _by + w * _ey + along * _fy)

    print(f"\n  gate: {_len:.0f} m long, finish point {_fw:.0f} m from its "
          f"right end, heading {FINISH_HEADING_DEG:.1f} deg")
    cases = [
        ("racing line, forward",      (-30, 3),   (25, 3),    +1),
        ("pit lane, forward",         (-10, -14), (8, -14),   +1),
        ("pushed backwards",          (6, -14),   (-6, -14),  -1),
        ("stops short of the line",   (-40, 0),   (-2, 0),    None),
        ("opposing track, 79 m right", (30, -79),  (-30, -79), None),
        ("grandstand side, 35 m left", (-30, 35),  (30, 35),   None),
    ]
    for label, a, b, want in cases:
        hit = segment_gate_intersection(_xy(*a), _xy(*b))
        got = hit[2] if hit else None
        assert got == want, (label, hit)
        detail = (f"t={hit[0]:.3f} lateral={hit[1]:+.1f} m" if hit else "no crossing")
        print(f"    {label:28s} -> {detail}")
    t, lateral, _sign = segment_gate_intersection(_xy(-30, 3), _xy(10, 3))
    assert abs(t - 0.75) < 1e-9 and abs(lateral - 3.0) < 1e-9
    print("    gate OK")
