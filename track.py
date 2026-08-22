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
