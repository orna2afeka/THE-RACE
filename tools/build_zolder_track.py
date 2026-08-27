"""
build_zolder_track.py — bake Circuit Zolder's centreline into the repo
=======================================================================
Fetches the circuit geometry from OpenStreetMap once, normalises it, and writes
zolder_centreline.py at the repo root for track_map.py to import.

    python tools/build_zolder_track.py            # fetch + write
    python tools/build_zolder_track.py --verify   # fetch + write + full report

WHY THIS IS A BUILD STEP AND NOT A RUNTIME FETCH
The pit dashboard draws the circuit map inside a fragment that reruns every two
seconds, on a pit LAN that is deliberately isolated (see the enableCORS /
enableXsrfProtection note in .streamlit/config.toml). A network call on that path
is the worst possible place for one. The circuit has not moved since 1963; fetch
it once, commit the result, and the race-day dashboard needs no network at all.

WHY OSM AND NOT A HAND-DRAWN OUTLINE
The repo has no track geometry of any kind — only the finish line coordinates and
TRACK_LENGTH_METERS. A hand-drawn approximation would not line up with the
satellite basemap the dashboard already renders directly above the vector map,
and two maps of the same corner disagreeing on screen is worse than one map.

WHAT COMES OUT
zolder_centreline.py: ~215 (lat, lon) pairs, index 0 at the finish line, ordered
in the direction of travel, plus provenance (OSM ids, timestamps, raw length) and
a baked LABEL_SIDE tuple. Roughly 5 KB. It is GENERATED — never hand-edit it;
change this script and re-run.

LICENCE
OpenStreetMap data is ODbL 1.0, which requires attribution wherever it is shown.
The generated module carries OSM_ATTRIBUTION and Pit_Dashboard/track_map_view.py
renders it as a permanent caption under the chart.
"""

import argparse
import datetime
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import requests  # noqa: E402

import track  # noqa: E402  (path set up immediately above)

# The sector model, read from the one place that defines it rather than retyped.
# A dev-time tool may reach into Pit_Dashboard/ like this; the RUNTIME geometry
# module (track_map.py) deliberately does not — it takes boundaries as an
# argument and knows nothing about sectors at all.
_PIT = os.path.join(_REPO, "Pit_Dashboard")
if _PIT not in sys.path:
    sys.path.insert(0, _PIT)
from strategy_engine import SECTIONS_INFO  # noqa: E402

# Sector start distances, in order: [0, 600, 1000, ...]. The ninth sector's end
# is the lap length, which is also sector 1's start.
BOUNDARIES = [SECTIONS_INFO[s]["range"][0] for s in sorted(SECTIONS_INFO)]
SECTOR_IDS = sorted(SECTIONS_INFO)

OUT_PATH = os.path.join(_REPO, "zolder_centreline.py")

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "afeka-solar-race/1.0 (pit-wall track map build; contact: team)"

# The circuit as a relation, not a bbox of ways. The relation lists exactly the
# 22 ways that form the racing surface; a bbox query returns 27, picking up the
# Pitlane (way 179267542) and the Safety Car Lane (444633117) as well, which
# would have to be filtered out by name — brittle, and silently wrong the day
# someone renames one.
#
# Fallback if the relation is ever deleted upstream, for the record:
#     [out:json][timeout:60];way["highway"="raceway"](50.98,5.24,51.00,5.27);out geom;
# ...then drop anything whose name contains "Pitlane" or "Safety Car".
OSM_RELATION_ID = 6006460
OVERPASS_QUERY = f"[out:json][timeout:60];rel({OSM_RELATION_ID});way(r);out geom;"

# How far the finish line is allowed to sit from the fetched centreline before we
# refuse to build. Measured today: 2.02 m. A sudden jump means OSM has been
# re-drawn somewhere that matters, and a quietly rotated lap origin would corrupt
# every sector boundary on the map without looking broken.
MAX_FINISH_OFFSET_M = 10.0

# Half-width used to decide which side of the track a sector label goes. Baked so
# the dashboard does no geometry to place nine labels.
LABEL_PROBE_M = 26.0


def _log(msg):
    print(f"[zolder] {msg}")


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def fetch_ways():
    """Every way in the circuit relation, with inline coordinates and node ids."""
    _log(f"querying Overpass for relation {OSM_RELATION_ID} ...")
    # A real User-Agent is required, not optional: Overpass answers the default
    # python-requests one with 406 Not Acceptable. Their usage policy asks for
    # something identifying, so say what this is and who it is for.
    resp = requests.get(OVERPASS_URL, params={"data": OVERPASS_QUERY},
                        headers={"User-Agent": USER_AGENT}, timeout=120)
    resp.raise_for_status()
    payload = resp.json()

    ways = [e for e in payload.get("elements", []) if e.get("type") == "way"]
    if not ways:
        sys.exit("Overpass returned no ways — is the relation still there?")

    stamp = payload.get("osm3s", {}).get("timestamp_osm_base", "")
    _log(f"got {len(ways)} ways (OSM base {stamp or 'unknown'})")
    return ways, stamp


# --------------------------------------------------------------------------- #
# Normalise — every step asserted, never warned about
# --------------------------------------------------------------------------- #
# A half-chained loop still renders as a plausible-looking circuit. You do not
# catch that by eye on a 400-pixel chart, you catch it when a race engineer says
# the car is in sector 6 and it is not. So each step below fails hard.

def chain_ways(ways):
    """Walk the ways into one closed ring. Returns (points, way_order).

    Each way is a fragment of the circuit with its own arbitrary direction. They
    join at shared nodes, and on a closed circuit every junction node belongs to
    exactly two ways — which is the invariant that makes the walk unambiguous.
    """
    ends = {}
    for i, w in enumerate(ways):
        nodes = w["nodes"]
        for node_id in (nodes[0], nodes[-1]):
            ends.setdefault(node_id, []).append(i)

    bad = {n: v for n, v in ends.items() if len(v) != 2}
    assert not bad, (f"{len(bad)} endpoint node(s) are not shared by exactly two "
                     f"ways — the relation is not a simple closed ring: {list(bad)[:5]}")

    used = [False] * len(ways)
    order = [0]
    used[0] = True
    ring = [(p["lat"], p["lon"]) for p in ways[0]["geometry"]]
    open_node = ways[0]["nodes"][-1]

    while len(order) < len(ways):
        nxt = next((i for i in ends[open_node] if not used[i]), None)
        assert nxt is not None, (
            f"walk stranded at node {open_node} after {len(order)}/{len(ways)} ways")
        used[nxt] = True
        order.append(nxt)

        geom = [(p["lat"], p["lon"]) for p in ways[nxt]["geometry"]]
        nodes = ways[nxt]["nodes"]
        if nodes[0] != open_node:          # this way runs the other way round
            geom.reverse()
            nodes = nodes[::-1]
        ring.extend(geom[1:])              # geom[0] duplicates the current end
        open_node = nodes[-1]

    assert all(used), "not every way was consumed"
    assert ring[0] == ring[-1], "the ring does not close on itself"
    return ring[:-1], order                # drop the duplicated closing vertex


def cumulative_metres(latlon):
    """Distance along the ring at each vertex, plus the closing total."""
    xy = [track.to_local_xy(la, lo) for la, lo in latlon]
    cum = [0.0]
    for i in range(1, len(xy)):
        cum.append(cum[-1] + math.hypot(xy[i][0] - xy[i - 1][0],
                                        xy[i][1] - xy[i - 1][1]))
    closing = math.hypot(xy[0][0] - xy[-1][0], xy[0][1] - xy[-1][1])
    return cum + [cum[-1] + closing]


def turn_positions(ways, latlon):
    """Distance-along-lap of each OSM-tagged turn, keyed by its ref (T1, T2...).

    Located by finding the ring vertex nearest each turn way's MIDPOINT, rather
    than by tracking offsets through the walk. That bookkeeping is easy to get
    subtly wrong and, worse, has to be redone differently after a reversal —
    whereas "which vertex is this corner nearest" is the same question whichever
    way round the ring runs, so this survives the reversal below unchanged.

    Used to VALIDATE the direction of travel: OSM tags the corners in racing
    order, so a backwards walk yields descending positions.
    """
    cum = cumulative_metres(latlon)
    xy = [track.to_local_xy(la, lo) for la, lo in latlon]
    at = {}
    for w in ways:
        ref = ((w.get("tags") or {}).get("ref") or "").upper()
        if not (ref.startswith("T") and ref[1:].isdigit()):
            continue
        geom = w["geometry"]
        mid = geom[len(geom) // 2]
        mx, my = track.to_local_xy(mid["lat"], mid["lon"])
        best = min(range(len(xy)),
                   key=lambda i: (xy[i][0] - mx) ** 2 + (xy[i][1] - my) ** 2)
        at[ref] = cum[best]
    return at


def rotate_to_finish(latlon):
    """Put the finish line at index 0, inserting a vertex exactly there.

    NOT nearest-vertex snapping. Zolder's start/finish straight is a single 218 m
    OSM segment, so the closest existing VERTEX is 43.9 m from the finish line
    while the perpendicular foot on that segment is 2.0 m from it. Snapping would
    put the lap origin 44 m out and drag every one of the nine sector boundaries
    along with it.
    """
    xy = [track.to_local_xy(la, lo) for la, lo in latlon]
    finish = track.to_local_xy(track.FINISH_LINE_LAT, track.FINISH_LINE_LON)

    best = (float("inf"), 0, 0.0)
    for i in range(len(xy)):
        j = (i + 1) % len(xy)
        d, t = _point_segment(finish, xy[i], xy[j])
        if d < best[0]:
            best = (d, i, t)

    dist, i, t = best
    assert dist <= MAX_FINISH_OFFSET_M, (
        f"finish line is {dist:.1f} m from the fetched centreline (limit "
        f"{MAX_FINISH_OFFSET_M} m) — the OSM geometry has moved, or "
        f"track.FINISH_LINE_LAT/LON is wrong. Refusing to guess a lap origin.")

    j = (i + 1) % len(latlon)
    # Interpolate in lat/lon so the stored source of truth stays lat/lon.
    a, b = latlon[i], latlon[j]
    pin = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))

    # pin, then everything from the far end of its segment round to the near end:
    # p_j .. p_last, p_0 .. p_i. NOT latlon[:j+1] for that tail — that re-includes
    # p_j, which duplicates a vertex and inserts a phantom segment right across
    # the circuit. It measured as +88 m on a 4001 m lap, which is exactly the
    # kind of error that still draws a plausible-looking track.
    rotated = [pin] + latlon[j:] + latlon[:j]
    # If the projection landed exactly on an existing vertex, do not keep both.
    if _same_point(rotated[0], rotated[1]):
        rotated.pop(1)
    if _same_point(rotated[0], rotated[-1]):
        rotated.pop()
    return rotated, dist


def _point_segment(p, a, b):
    """(distance, t) from point p to segment a->b, t clamped into [0, 1]."""
    abx, aby = b[0] - a[0], b[1] - a[1]
    den = abx * abx + aby * aby
    if den == 0.0:
        return math.hypot(p[0] - a[0], p[1] - a[1]), 0.0
    t = max(0.0, min(1.0, ((p[0] - a[0]) * abx + (p[1] - a[1]) * aby) / den))
    return math.hypot(p[0] - (a[0] + t * abx), p[1] - (a[1] + t * aby)), t


def _same_point(a, b, eps=1e-9):
    return abs(a[0] - b[0]) < eps and abs(a[1] - b[1]) < eps


def _descending(turns, refs):
    """Do the tagged turns run backwards around the ring?

    The ring has no start yet, so the sequence of turn positions wraps exactly
    once whichever direction it runs: T1..T16 ascending still shows one big drop
    where the loop closes. Counting steps and taking the majority is therefore
    the right test, not `seq == sorted(seq)` — that one calls a correctly-ordered
    lap "backwards" purely because of the wrap, which is what it did here.
    """
    seq = [turns[r] for r in refs]
    back = sum(1 for a, b in zip(seq, seq[1:]) if b < a)
    return back > len(seq) // 2


def label_sides(latlon, cum, scale):
    """Which side of the track each sector label sits on: +1 left, -1 right.

    Zolder doubles back on itself twice, so "always left" drops labels on top of
    a different part of the circuit. Pick, per boundary, whichever side is
    further from the ring as a whole.
    """
    xy = [track.to_local_xy(la, lo) for la, lo in latlon]
    sides = []
    for b in BOUNDARIES:
        px, py = _interp(xy, cum, scale, b)
        ax, ay = _interp(xy, cum, scale, b - 4.0)
        bx, by = _interp(xy, cum, scale, b + 4.0)
        tx, ty = bx - ax, by - ay
        n = math.hypot(tx, ty) or 1.0
        nx, ny = -ty / n, tx / n
        best, pick = -1.0, 1
        for side in (1, -1):
            q = (px + LABEL_PROBE_M * side * nx, py + LABEL_PROBE_M * side * ny)
            d = min(_point_segment(q, xy[i], xy[(i + 1) % len(xy)])[0]
                    for i in range(len(xy)))
            if d > best:
                best, pick = d, side
        sides.append(pick)
    return sides


def _interp(xy, cum, scale, d):
    """(x, y) at distance d along the ring, using the pre-scale cum array."""
    total = cum[-1]
    raw = (d / scale) % total
    lo, hi = 0, len(cum) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if cum[mid] <= raw:
            lo = mid
        else:
            hi = mid
    span = cum[lo + 1] - cum[lo]
    t = 0.0 if span == 0 else (raw - cum[lo]) / span
    a, b = xy[lo], xy[(lo + 1) % len(xy)]
    return a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])


# --------------------------------------------------------------------------- #
# Emit
# --------------------------------------------------------------------------- #
_TEMPLATE = '''"""
zolder_centreline.py — GENERATED, DO NOT EDIT BY HAND
======================================================
Circuit Zolder's centreline, baked from OpenStreetMap by
tools/build_zolder_track.py. Re-run that script to change anything here.

    Circuit centreline (c) OpenStreetMap contributors, ODbL 1.0
    OSM relation {relation}, {way_count} ways, fetched {fetched}

Index 0 is the finish line at track.FINISH_LINE_LAT/LON; the order is the
direction of travel. The measured centreline is {raw_length:.2f} m — see
track_map.CUM_M for why it is rescaled to track.TRACK_LENGTH_METERS.
"""

OSM_RELATION_ID = {relation}
OSM_WAY_IDS = {way_ids}
OSM_TIMESTAMP = "{osm_stamp}"
BUILT_UTC = "{built}"
RAW_LENGTH_M = {raw_length:.4f}
FINISH_OFFSET_M = {finish_offset:.4f}

OSM_ATTRIBUTION = (
    "Circuit centreline \\u00a9 OpenStreetMap contributors, ODbL 1.0 \\u00b7 "
    "OSM relation {relation}, fetched {fetched}"
)

# Which side of the track each of the nine sector labels sits on, in sector
# order: +1 is left of the direction of travel, -1 is right. Precomputed because
# the circuit doubles back on itself and a naive "always left" would drop labels
# onto a different part of the lap.
LABEL_SIDE = {label_side}

# (lat, lon), index 0 = the finish line, in the direction of travel.
CENTRELINE_LATLON = (
{points}
)
'''


def emit(path, latlon, ways, order, stamp, raw_length, finish_offset, sides):
    pts = "\n".join(f"    ({la:.7f}, {lo:.7f})," for la, lo in latlon)
    body = _TEMPLATE.format(
        relation=OSM_RELATION_ID,
        way_count=len(ways),
        way_ids=repr(tuple(ways[i]["id"] for i in order)),
        osm_stamp=stamp,
        built=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        fetched=datetime.date.today().isoformat(),
        raw_length=raw_length,
        finish_offset=finish_offset,
        label_side=repr(tuple(sides)),
        points=pts,
    )
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    return len(body)


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verify", action="store_true",
                    help="print the full geometry report after building")
    args = ap.parse_args()

    ways, stamp = fetch_ways()
    ring, order = chain_ways(ways)
    _log(f"chained {len(ways)} ways into {len(ring)} vertices, ring closes")

    cum = cumulative_metres(ring)
    raw_length = cum[-1]
    _log(f"centreline measures {raw_length:.2f} m")

    # Direction of travel, taken from OSM's own corner numbering rather than
    # guessed from winding order. The ring is a loop, so "increasing" is only
    # meaningful once we ignore the single wrap-around step; count how many
    # consecutive pairs go backwards and let the majority decide.
    turns = turn_positions(ways, ring)
    refs = sorted(turns, key=lambda r: int(r[1:]))
    if refs and _descending(turns, refs):
        _log("turn refs run backwards -> reversing to the racing direction")
        ring.reverse()
        turns = turn_positions(ways, ring)
    assert not refs or not _descending(turns, refs), (
        "OSM turn refs are not monotonic along the ring in either direction - "
        "the chaining is wrong, or the relation now contains a spur")
    _log(f"direction confirmed from {len(refs)} tagged turns ({refs[0]}..{refs[-1]})"
         if refs else "no turn refs found - direction not validated")

    ring, finish_offset = rotate_to_finish(ring)
    _log(f"rotated to the finish line (perpendicular offset {finish_offset:.2f} m)")

    cum = cumulative_metres(ring)
    raw_length = cum[-1]
    scale = track.TRACK_LENGTH_METERS / raw_length
    # Re-locate the turns now the ring has an origin. The pre-rotation values
    # were measured from wherever the walk happened to start, which is fine for
    # the direction test above and meaningless in the report below.
    turns = turn_positions(ways, ring)
    assert abs(raw_length - 4001.0) < 50.0, (
        f"centreline came out {raw_length:.1f} m after rotation — a rotation that "
        f"changes the length has duplicated or dropped vertices")

    sides = label_sides(ring, cum, scale)

    size = emit(OUT_PATH, ring, ways, order, stamp, raw_length, finish_offset, sides)
    _log(f"wrote {OUT_PATH} ({len(ring)} points, {size / 1024:.1f} KB)")

    if args.verify:
        _report(ring, cum, raw_length, scale, finish_offset, turns)


def _report(ring, cum, raw_length, scale, finish_offset, turns):
    print()
    print("  vertices              ", len(ring))
    print(f"  measured length        {raw_length:.2f} m")
    print(f"  rescaled to            {track.TRACK_LENGTH_METERS:.1f} m "
          f"(x{scale:.6f}, {abs(raw_length - track.TRACK_LENGTH_METERS):.2f} m over a lap)")
    print(f"  finish-line offset     {finish_offset:.2f} m")
    steps = [cum[i + 1] - cum[i] for i in range(len(cum) - 1)]
    print(f"  vertex spacing         min {min(steps):.2f} m, "
          f"median {sorted(steps)[len(steps) // 2]:.2f} m, max {max(steps):.2f} m")
    print()
    print("  OSM turn positions vs the team's sector model")
    print("  (recorded for the day real Zolder laps exist - the map colours from")
    print("   SECTIONS_INFO regardless, so it agrees with the sector strip)")
    spans = list(zip(SECTOR_IDS, BOUNDARIES,
                     BOUNDARIES[1:] + [track.TRACK_LENGTH_METERS]))
    for ref in sorted(turns, key=lambda r: int(r[1:])):
        d = turns[ref] * scale
        sec = next((f"S{s}" for s, lo, hi in spans if lo <= d < hi), "?")
        model = SECTIONS_INFO.get(int(sec[1:]), {}).get("name", "") if sec != "?" else ""
        print(f"    {ref:<4} {d:7.1f} m   falls in {sec} ({model})")


if __name__ == "__main__":
    main()
