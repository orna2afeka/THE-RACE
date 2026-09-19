"""
build_zolder_animation.py — bake the presentation circuit animation
====================================================================
Generates Pit_Dashboard/zolder_animation.html: a single self-contained page
that draws Circuit Zolder and drives a car round it. This is the FORMAL piece —
the one shown to sponsors, faculty and the team — not part of the race
dashboard and never on the critical path of a session.

    python tools/build_zolder_animation.py            # write the page
    python tools/build_zolder_animation.py --verify   # write it and report

WHY THIS IS GENERATED AND NOT HAND-WRITTEN
The hand-written version of this page carried its own copies of the track
length, the nine sectors, the turn landmarks and the centreline. Four facts the
repo already owns, retyped into a file nobody would think to update — so the
day a sector boundary moves, the demo keeps confidently showing the old one to
an audience. Everything below is read from the same modules the pit dashboard
reads:

    track.py                 lap length, finish line
    track_map.py             centreline geometry, sector splits, gate ticks
    zolder_centreline.py     OSM provenance and attribution, label sides
    strategy_engine.py       SECTIONS_INFO (the nine sectors), TRACK_LANDMARKS
    Pit_Dashboard/constants  SECTION_NAMES (what each sector is called)
    profiles/dor_280s.csv    the 280 s baseline lap the demo car actually drives

A dev-time tool may reach into Pit_Dashboard/ like this; tools/build_zolder_
track.py already does, and for the same reason.

WHAT THE OUTPUT DEPENDS ON AT RUNTIME: NOTHING
No CDN, no fonts that must load, no network. The previous version pulled
anime.js from cdnjs and Rajdhani from Google Fonts — on the isolated pit LAN,
or a venue with captive-portal wifi, the font silently falls back (fine) and
anime.js does not load at all (not fine: the car never moves, which is the
entire demo). Motion here is a plain requestAnimationFrame integrator, so the
page works from a USB stick on a laptop in flight mode.

THE CAR IS DRAWN INSIDE THE SVG
The hand-written version positioned the car as an HTML <div> at a percentage of
the container. That only lands on the track while the container's aspect ratio
exactly matches the viewBox's: any other shape letterboxes the SVG and the dot
drifts off into the grass. Inside the SVG it is in track coordinates and cannot
come apart from the track no matter what the page does around it.
"""

import argparse
import csv
import datetime
import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
_PIT = os.path.join(_REPO, "Pit_Dashboard")
if _PIT not in sys.path:
    sys.path.insert(0, _PIT)

import track                                                  # noqa: E402
import track_map                                              # noqa: E402
from zolder_centreline import (BUILT_UTC, OSM_ATTRIBUTION,    # noqa: E402
                               OSM_RELATION_ID, OSM_TIMESTAMP)
from strategy_engine import (DOC_TO_TRACK_OFFSET_M,           # noqa: E402
                             SECTIONS_INFO, TRACK_LANDMARKS,
                             TURN_START_TRACK_M)
from constants import DATA_STALE_AFTER_S, SECTION_NAMES       # noqa: E402
# How long a displayed position stays "live". limits.py owns it, and the pit's
# own API reads the same constant -- two answers to "is this position current"
# is how the wall and the dashboard end up disagreeing in front of the crew.
from limits import GPS_LIVE_MAX_AGE_S                         # noqa: E402

# BOTH generated pages live in docs/, and neither is application code.
#
# GitHub Pages can publish from a repo's docs/ folder and still serve it at the
# SITE ROOT, so docs/index.html is reachable at the bare project address --
# family get a link with no path on it -- while the repo root stays free of
# loose .html. The presentation page rides along in the same folder because it
# is the same kind of thing: a web page about the car, not part of the pit app
# it used to sit inside.
_SITE = os.path.join(_REPO, "docs")
OUT_PATH = os.path.join(_SITE, "zolder_animation.html")
SPECTATOR_PATH = os.path.join(_SITE, "index.html")

# The pit wall is NOT in docs/. docs/ is what GitHub Pages publishes, and this
# page carries pack voltage, temperatures and true GPS -- everything the
# spectator page is starved of precisely because that one is public. It lives
# beside the dashboard and is served by tools/pit_wall.py on the pit LAN only.
WALL_PATH = os.path.join(_REPO, "Pit_Dashboard", "wall.html")
PROFILES_DIR = os.path.join(_REPO, "profiles")
PROFILE_PATH = os.path.join(_REPO, "profiles", "dor_280s.csv")

SECTOR_IDS = sorted(SECTIONS_INFO)


def track_m(doc_m):
    """A sector-document distance, as metres from the track.py finish line."""
    return (float(doc_m) - DOC_TO_TRACK_OFFSET_M) % track.TRACK_LENGTH_METERS


# Where each sector starts ON THE MAP, in sector order. Deliberately NOT shifted
# by the document offset: S1 starts on the start/finish line, where the team
# expects to see it, even though that leaves a few turns just outside the
# sector the document lists them in. Only the 210 s profile lookup keeps the
# offset (docOffset below).
BOUNDARIES = [float(SECTIONS_INFO[s]["range"][0]) % track.TRACK_LENGTH_METERS
              for s in SECTOR_IDS]


def landmark_track_m(lm):
    """Where a landmark is drawn: its turn's measured start, else its document
    distance shifted into the car's frame. None for the finish line, which has
    its own gate."""
    if float(lm["dist_m"]) % track.TRACK_LENGTH_METERS == 0.0:
        return None
    if lm.get("turn") is not None:
        return float(TURN_START_TRACK_M[lm["turn"]])
    return track_m(lm["dist_m"])

# ── The palette ───────────────────────────────────────────────────────────── #
# Nine distinct hues, one per sector, deliberately NOT the pit dashboard's
# three-colour risk palette (SECTION_COLORS in constants.py). Those three
# colours mean "this corner is dangerous" and they belong on the wall the
# engineers read during a session. This page is a different job: nobody is
# making a call off it, and what it needs to do is let a viewer follow the car
# from one named sector to the next, which three repeated colours cannot do —
# S7 and S8 would be the same green.
SECTOR_PALETTE = {
    1: "#f87171", 2: "#fb923c", 3: "#fbbf24", 4: "#34d399", 5: "#2dd4bf",
    6: "#38bdf8", 7: "#818cf8", 8: "#a78bfa", 9: "#e879f9",
}

CAR_COLOR = "#00e5ff"

# ── Geometry, in metres, because the SVG user unit IS one metre ───────────── #
# Everything below is track_map's local-metre frame with y flipped (SVG counts
# y downward), shifted so the drawing starts at 0,0. Font sizes and stroke
# widths are therefore also in metres: the 12 m track ribbon really is twelve
# metres wide, and a 26 m label is about two car lengths tall. Picking these in
# ground units instead of pixels is what keeps them in proportion when the page
# is shown on a phone and on a projector.
# The drawing is fitted to the tarmac plus the labels and turn badges around
# it; this is the breathing room added once they are all accounted for.
PAD_M = 40.0
TRACK_CASING_M = 17.0
TRACK_CORE_M = 11.0
GATE_HALF_M = 17.0
FINISH_HALF_M = 24.0
SECTOR_LABEL_OFFSET_M = 46.0
# The car marker, and it has to win against the sector it is standing on. At
# 13 m it was barely wider than the 17 m track casing and drawn in cyan --
# which is very close to S6 (#38bdf8) and S7 (#818cf8), so for two of the nine
# sectors the car all but vanished into the track. Bigger, and sat on a dark
# disc (see MAP_SVG) so the separation no longer depends on the colour
# underneath it at all.
CAR_RADIUS_M = 20.0
CAR_HALO_SCALE = 1.38     # dark disc under the car, as a multiple of its radius
CAR_PULSE_SCALE = 2.4     # how far the ping travels before it fades out
TRAIL_M = 170.0       # how much track the car's tail covers
SECTOR_FONT_M = 30.0
# Numbered turn badges, one per ETCR turn, like the circles on the ETCR map.
TURN_BADGE_R_M = 15.0
TURN_BADGE_FONT_M = 17.0
TURN_BADGE_OFFSETS_M = (34.0, 62.0, 90.0)   # tried nearest first


def _svg_xy(x, y, ox, oy):
    """One local-metre point in SVG user units (y flipped, origin at 0,0)."""
    return (round(x - ox, 1), round(oy - y, 1))


def _clearance(px, py):
    """How far a point is from the nearest bit of tarmac, in metres.

    Used to decide which side of the track a turn badge goes on. The obvious
    rule — push it away from the middle of the circuit — is wrong at Zolder,
    because the lap doubles back on itself twice: at Turn 7 and at the final
    chicane the "outside" of the local corner is the INSIDE of the circuit as a
    whole, and a centroid test puts the label straight across the other half of
    the lap. Maximising clearance instead asks the question that actually
    matters, which is "where is there room for this text".
    """
    return min(math.hypot(px - cx, py - cy)
               for cx, cy in track_map.CENTRELINE_XY)


def _read_profile():
    """The 210 s baseline lap: [(distance_m, speed_kmh), ...], ~40 m apart.

    This is what makes the demo lap worth watching. A car advanced at a
    constant speed goes round in a bland circle; driven by the real profile it
    brakes for the chicane, crawls through the hairpins and pulls away up the
    hill, so the sector colours and the speed readout tell the same story a
    real lap does. The file is the team's own baseline strategy, not a shape
    invented for the animation.
    """
    rows = []
    with open(PROFILE_PATH, encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            rows.append((float(row["d(m)"]), float(row["V(km/h)"])))
    rows.sort()
    # Every 4th sample: 10 m spacing is finer than anything visible here, and
    # the full file would quadruple the page for no difference on screen.
    thinned = rows[::4]
    if thinned[-1][0] < rows[-1][0]:
        thinned.append(rows[-1])
    return thinned


def _all_profile_lap_seconds():
    """{key: modelled lap seconds} for every profile in profiles/.

    Read from the CSVs rather than restated here, so the pit wall's "target"
    and the file the car is actually following can never drift apart. The car
    names its choice in `active_strategy`, which is the same key as the
    filename, so the wall looks the target up by that name and shows nothing
    at all if the car is following something this build has never seen.
    """
    out = {}
    if not os.path.isdir(PROFILES_DIR):
        return out
    for name in sorted(os.listdir(PROFILES_DIR)):
        if not name.endswith(".csv"):
            continue
        path = os.path.join(PROFILES_DIR, name)
        last = None
        try:
            with open(path, encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    last = float(row["Time(s)"])
        except (OSError, KeyError, ValueError):
            continue
        if last:
            out[name[:-4]] = round(last, 2)
    return out


def _profile_lap_seconds():
    """Modelled lap time from the profile's own Time(s) column."""
    last = None
    with open(PROFILE_PATH, encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            last = float(row["Time(s)"])
    return last


def _centroid(points):
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))


def build_data():
    """Everything the page needs, in SVG user units. Pure apart from reading
    the profile CSV, so --verify can report on it without writing anything."""
    centre_local = _centroid(track_map.CENTRELINE_XY)

    # -- pass 1: everything in local metres, and how far it all reaches ------ #
    # Bounds start as the tarmac and grow to contain each label, so the
    # finished viewBox is exactly the drawing and no more.
    x0, x1, y0, y1 = track_map.BOUNDS_XY
    lo_x, hi_x, lo_y, hi_y = x0, x1, y0, y1

    def grow(px, py):
        nonlocal lo_x, hi_x, lo_y, hi_y
        lo_x, hi_x = min(lo_x, px), max(hi_x, px)
        lo_y, hi_y = min(lo_y, py), max(hi_y, py)

    # Turn names and speeds are not drawn on the map (the numbered badges are);
    # the page still needs them for its "next turn" readout.
    landmarks = sorted(
        ({"name": str(lm["name"]), "speed": lm.get("max_speed"), "dist": d}
         for lm in TRACK_LANDMARKS
         for d in [landmark_track_m(lm)] if d is not None),
        key=lambda l: l["dist"])

    ticks = track_map.boundary_ticks(BOUNDARIES, GATE_HALF_M)
    slabels_local = []
    for (dist, _a, _b, normal), sector_id, side in zip(ticks, SECTOR_IDS,
                                                       track_map.LABEL_SIDE):
        px, py = track_map.position_at_distance(dist)
        lx = px + SECTOR_LABEL_OFFSET_M * side * normal[0]
        ly = py + SECTOR_LABEL_OFFSET_M * side * normal[1]
        slabels_local.append((sector_id, lx, ly))
        grow(lx, ly)

    # -- turn badges: every ETCR turn gets its number beside the track ------- #
    # Placed after the sector labels so they can keep clear of them: each badge
    # takes the spot (either side, a few distances out) furthest from tarmac,
    # other badges and sector labels.
    avoid = [(lx, ly) for _sid, lx, ly in slabels_local]
    turns_local = []
    for number, start in sorted(TURN_START_TRACK_M.items()):
        px, py = track_map.position_at_distance(start)
        tx, ty = track_map.tangent_at(start)
        nx, ny = -ty, tx
        best = None
        for off in TURN_BADGE_OFFSETS_M:
            for sign in (1.0, -1.0):
                bx, by = px + off * sign * nx, py + off * sign * ny
                room = min([_clearance(bx, by) - TURN_BADGE_R_M]
                           + [math.hypot(bx - ax, by - ay) - TURN_BADGE_R_M
                              for ax, ay in avoid])
                # Nearer is better as long as there is real room.
                score = min(room, 14.0) - off * 0.05
                if best is None or score > best[0]:
                    best = (score, bx, by)
        _, bx, by = best
        avoid.extend([(bx, by)])
        turns_local.append({"n": number, "dist": float(start),
                            "px": px, "py": py, "bx": bx, "by": by})
        grow(bx - TURN_BADGE_R_M, by - TURN_BADGE_R_M)
        grow(bx + TURN_BADGE_R_M, by + TURN_BADGE_R_M)

    lo_x -= PAD_M
    hi_x += PAD_M
    lo_y -= PAD_M
    hi_y += PAD_M

    # -- pass 2: into SVG user units (one unit = one metre, y flipped) ------- #
    ox, oy = lo_x, hi_y
    width, height = hi_x - lo_x, hi_y - lo_y

    line = [_svg_xy(x, y, ox, oy) for x, y in track_map.CENTRELINE_XY]
    cum = [round(c, 2) for c in track_map.CUM_M]

    # split_at returns runs sorted by start distance; name each run by its
    # start rather than by position so the order can never mislabel one.
    id_at = {b: sid for b, sid in zip(BOUNDARIES, SECTOR_IDS)}
    sectors = []
    for seg_start, seg_end, xs, ys in sorted(
            track_map.split_at(BOUNDARIES), key=lambda r: id_at[r[0]]):
        sector_id = id_at[seg_start]
        pts = [_svg_xy(x, y, ox, oy) for x, y in zip(xs, ys)]
        sectors.append({
            "id": sector_id,
            "name": SECTION_NAMES.get(sector_id, "Sector %d" % sector_id),
            "start": seg_start, "end": seg_end,
            "color": SECTOR_PALETTE[sector_id],
            "d": "M " + " L ".join("%s,%s" % (px, py) for px, py in pts),
        })

    gates = []
    for dist, a, b, _normal in ticks:
        ax, ay = _svg_xy(a[0], a[1], ox, oy)
        bx, by = _svg_xy(b[0], b[1], ox, oy)
        gates.append({"dist": dist, "x1": ax, "y1": ay, "x2": bx, "y2": by})

    labels = []
    for sector_id, lx, ly in slabels_local:
        sx, sy = _svg_xy(lx, ly, ox, oy)
        # The label at a boundary names the sector STARTING there -- 0 m is S1,
        # 600 m is S2 -- the same convention the pit dashboard's map used.
        labels.append({"text": "S%d" % sector_id, "x": sx, "y": sy,
                       "color": SECTOR_PALETTE[sector_id]})

    # The finish line is a gate too, but it is also a timing point, so it gets
    # its own longer white mark rather than one of the grey ones.
    _d, fa, fb, _n = track_map.boundary_ticks([0.0], FINISH_HALF_M)[0]
    fa_x, fa_y = _svg_xy(fa[0], fa[1], ox, oy)
    fb_x, fb_y = _svg_xy(fb[0], fb[1], ox, oy)

    turns = []
    for t in turns_local:
        x1, y1 = _svg_xy(t["px"], t["py"], ox, oy)
        bx, by = _svg_xy(t["bx"], t["by"], ox, oy)
        turns.append({"n": t["n"], "dist": t["dist"],
                      "x1": x1, "y1": y1, "x": bx, "y": by})

    # The projection, baked, so a page can put a GPS fix in SVG units without
    # re-deriving anything. SVG user units ARE local metres with y flipped
    # (see _svg_xy), and track.to_local_xy is equirectangular about the finish
    # line, so the whole transform is two multiplies and two subtractions:
    #
    #     x_svg = (lon - lon0) * mPerDegLon - ox
    #     y_svg = oy - (lat - lat0) * mPerDegLat
    #
    # Written out rather than shipping a formula the page has to agree with:
    # this is the SAME to_local_xy the car's lap trigger uses, so the dot and
    # the finish-line test can never drift apart.
    deg = math.radians(1.0)
    geo = {
        "lat0": track.FINISH_LINE_LAT,
        "lon0": track.FINISH_LINE_LON,
        "mPerDegLat": round(deg * track._EARTH_RADIUS_M, 4),
        "mPerDegLon": round(deg * track._EARTH_RADIUS_M
                            * math.cos(math.radians(track.FINISH_LINE_LAT)), 4),
        "ox": round(ox, 3),
        "oy": round(oy, 3),
    }

    return {
        "viewBox": "0 0 %.0f %.0f" % (width, height),
        "geo": geo,
        "trackLength": track.TRACK_LENGTH_METERS,
        # ZERO NOW, and DOC_TO_TRACK_OFFSET_M is no longer read here. It
        # existed because the demo drove 210s.xlsx, which counts from the sector
        # document's zero ~120 m before the car's finish line. The demo now
        # drives profiles/dor_280s.csv, built from a lap the car logged, whose
        # distances already count from the car's own zero — so shifting it would
        # put the demo car's speed 120 m out of phase with the map.
        # strategy_engine.DOC_TO_TRACK_OFFSET_M still converts the document's
        # turn distances in track_m(); only the profile lookup stops using it.
        "docOffset": 0.0,
        "line": line,
        "cum": cum,
        "sectors": sectors,
        "gates": gates,
        "sectorLabels": labels,
        "finish": {"x1": fa_x, "y1": fa_y, "x2": fb_x, "y2": fb_y},
        "landmarks": landmarks,
        "turns": turns,
        "profile": [[round(d, 1), round(v, 2)] for d, v in _read_profile()],
        "profileLapSeconds": round(_profile_lap_seconds(), 2),
        "carColor": CAR_COLOR,
        "attribution": OSM_ATTRIBUTION,
        "style": {
            "casing": TRACK_CASING_M, "core": TRACK_CORE_M,
            "car": CAR_RADIUS_M, "trail": TRAIL_M,
            "carHalo": CAR_HALO_SCALE, "carPulse": CAR_PULSE_SCALE,
            "sFont": SECTOR_FONT_M,
            "turnR": TURN_BADGE_R_M, "turnFont": TURN_BADGE_FONT_M,
        },
    }


# --------------------------------------------------------------------------- #
# The pages. TWO come out of this generator, sharing one map renderer:
#
#   Pit_Dashboard/zolder_animation.html   the formal/presentation piece. Drives
#                                         itself round the 210 s profile. Shown
#                                         to sponsors and the team; never live.
#   index.html                            the SPECTATOR page, at the repo root
#                                         so GitHub Pages serves it as the site
#                                         root. Live from Firebase, or honestly
#                                         says it is not.
#
# They share MAP_JS and BASE_CSS. The map is the expensive, fiddly part — the
# projection, the sector splits, the label placement — and having it exist
# twice is how the two would end up disagreeing about where Turn 12 is.
# --------------------------------------------------------------------------- #

# The database the car publishes to. Read from SolarRace_OS/main.py's own URL
# rather than retyped: this is the fourth place that string would otherwise
# live, and a spectator page pointed at the wrong database shows nothing with
# no error anyone would notice.
DB_URL = "https://solar-race-telemetry-default-rtdb.europe-west1.firebasedatabase.app"
PUBLIC_PATH = "public/live"
RACE_PATH = "public/race"
DRIVER_PATH = "public/driver"      # written by Pit_Dashboard/driver_message.py
# Who else is watching. Every open page refreshes its own key under
# VIEWERS_PATH; Pit_Dashboard/collector.py counts the fresh ones and publishes
# the total to VIEWERS_COUNT_PATH, which is all a page ever reads. See the
# comment on the sweeper there for why the counting is not done in the browser.
VIEWERS_PATH = "public/viewers"
VIEWERS_COUNT_PATH = "public/viewers_count"
VIEWER_BEAT_MS = 20000             # how often a page says it is still here
VIEWER_POLL_MS = 15000             # how often it asks for the total

# The official classification, embedded rather than scraped. The standings are
# Time Service B.V.'s product and arrive over SignalR, and their robots.txt
# disallows /lt and /signalr outright -- so reading the feed and re-serving the
# numbers ourselves is off the table. Framing the page they publish is the
# sanctioned route, and the one the race organiser itself takes.
#
# Note this is the PROVIDER's url, not the ESC wrapper page. The ESC page sets
# frame-ancestors to solarlogs.be and its own domain, so framing that would be
# refused; it is itself only an iframe around this address.
TIMING_URL = "https://livetiming.getraceresults.com/zolder"
TIMING_CREDIT = "Time Service B.V."

# How long after the car's last sample the page stops claiming to be live. The
# car publishes once a second; 20 s is twenty missed updates, which is a real
# outage and not a bad moment on a mobile network.
STALE_AFTER_S = 20

# Past this, the spectator page does not show the car's last reading at all:
# every number goes to a dash and the car leaves the map. /public/live keeps the
# last snapshot forever, so without this a page opened days after a test run
# showed that run's lap, distance and battery. Judged on the CAR's own sample
# time, so it also catches a snapshot that arrives the moment the page opens.
# Ten minutes is far beyond any viewer's clock skew or a mid-race outage worth
# riding out.
OLD_AFTER_S = 600

BASE_CSS = """
  :root {
    --bg: #06090f;
    --panel: rgba(15, 23, 42, 0.82);
    --line: #334155;
    --dim: #94a3b8;
    --text: #e2e8f0;
    --accent: #00e5ff;
    --good: #34d399;
    --warn: #fbbf24;
    --bad: #f87171;
  }
  * { box-sizing: border-box; }
  /* REQUIRED, and not redundant, and it lives HERE so that every page built
     from this file gets it.

     The browser hides [hidden] with a USER-AGENT rule, and ANY author rule
     beats a user-agent one -- so a later `display:` on the same element
     silently defeats el(...).hidden. It did exactly that to `.pill { display:
     inline-flex }`, and the pit wall spent its life showing the orange
     DEMO - NOT LIVE DATA badge over real race telemetry: precisely the failure
     the badge exists to prevent, and one that teaches the team to ignore it.

     The rule was written once already, in the spectator page's own CSS block,
     where the wall could not benefit from it. Anything scoped to one page
     cannot fix a bug the pages share. */
  [hidden] { display: none !important; }
  html, body {
    margin: 0; padding: 0; height: 100%; width: 100%;
    background-color: var(--bg);
    background-image: radial-gradient(#16202f 1px, transparent 1px);
    background-size: 26px 26px;
    color: var(--text);
    font-family: 'Rajdhani', 'Segoe UI Semibold', 'DIN Alternate',
                 system-ui, -apple-system, sans-serif;
  }
  .eyebrow {
    font-size: 0.78rem; letter-spacing: 4px; text-transform: uppercase;
    color: var(--dim); font-weight: 600;
  }
  h1 {
    margin: 4px 0 0; font-size: 1.5rem; font-weight: 700;
    letter-spacing: 2px; text-transform: uppercase; line-height: 1.1;
  }
  h1 span { color: var(--accent); }
  .label {
    font-size: 0.68rem; letter-spacing: 2.5px; text-transform: uppercase;
    color: var(--dim); font-weight: 600;
  }
  /* A badge beside a label: CHARGING on the battery, DRIVER CHANGE on the
     driver. Filled, not outlined, so it reads at a glance on a phone held at
     arm's length and on a TV across the garage -- neither audience is
     studying the page.

     IT LIVES HERE, in the shared block, because it did not once: the charging
     badge was written straight into docs/index.html and Pit_Dashboard/wall.html
     after they were generated, in two slightly different versions, and this
     generator never learned about either. Re-running it deleted the badge from
     both pages. Anything the pages share belongs in this file or it does not
     survive the next build.

     Safe to set `display` here only because [hidden] above is !important; see
     that note before copying this. */
  .chg {
    display: inline-block; margin-left: 7px; padding: 2px 7px;
    border-radius: 999px; font-size: 0.6rem; letter-spacing: 1.5px;
    font-weight: 700; color: #0b1220; background: var(--good);
    vertical-align: 1px;
  }
  /* The swap. Cyan rather than green: charging is good news for the car, a
     driver change is neither good nor bad, it is just what is happening. */
  .chg.swap { background: var(--accent); }
  .value { font-size: 1.75rem; font-weight: 700; font-variant-numeric: tabular-nums; }
  .value.small { font-size: 1.15rem; line-height: 1.25; }
  .unit { font-size: 0.85rem; color: var(--dim); margin-left: 5px;
          letter-spacing: 2px; font-weight: 600; }
  .sub { font-size: 0.85rem; color: var(--dim); font-weight: 600;
         font-variant-numeric: tabular-nums; margin-top: 2px; }
  /* -- the map ---------------------------------------------------------- */
  #map { position: relative; min-width: 0; min-height: 0; }
  svg { width: 100%; height: 100%; display: block; }
  .gate { stroke: #64748b; stroke-width: 2.6; }
  .finish { stroke: #ffffff; stroke-width: 5; }
  .turn-tick { stroke: #94a3b8; stroke-width: 2; }
  .turn-badge { fill: #0f172a; stroke: #94a3b8; stroke-width: 2.2; }
  .turn-num { fill: #e2e8f0; font-weight: 700; text-anchor: middle;
              dominant-baseline: central; }
  .s-label { font-weight: 700; letter-spacing: 1px;
             text-anchor: middle; dominant-baseline: middle; }
  /* -- legend ----------------------------------------------------------- */
  #legend {
    position: absolute; right: 18px; bottom: 16px;
    background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
    padding: 12px 16px; backdrop-filter: blur(8px);
    box-shadow: 0 12px 34px rgba(0, 0, 0, 0.55);
    display: grid; grid-template-columns: repeat(3, auto); gap: 7px 20px;
    font-size: 0.8rem; letter-spacing: 0.6px;
  }
  .leg { display: flex; align-items: center; gap: 8px; color: #cbd5e1; }
  .leg b { font-weight: 700; min-width: 20px; }
  .leg .swatch { width: 9px; height: 9px; border-radius: 50%; flex: 0 0 auto;
                 box-shadow: 0 0 6px currentColor; }
  .leg.on { color: #ffffff; }
  .leg.on .swatch { transform: scale(1.5); }
  /* -- lap strip -------------------------------------------------------- */
  #progress {
    position: relative; display: flex; height: 11px; margin-top: 7px;
    border-radius: 6px; overflow: hidden; background: #0f172a;
  }
  #progress .seg { height: 100%; opacity: 0.42; }
  #progress .seg.on { opacity: 1; }
  #progress-mark {
    position: absolute; top: -3px; width: 3px; height: 17px; left: 0;
    background: #ffffff; border-radius: 2px;
    box-shadow: 0 0 8px rgba(255, 255, 255, 0.85);
  }
"""

MAP_SVG = """
    <svg id="svg" preserveAspectRatio="xMinYMid meet">
      <defs>
        <filter id="glow" x="-60%" y="-60%" width="220%" height="220%">
          <feGaussianBlur stdDeviation="9" result="b"/>
          <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
      </defs>
      <g id="g-casing"></g>
      <g id="g-sectors"></g>
      <g id="g-gates"></g>
      <g id="g-turns"></g>
      <g id="g-slabels"></g>
      <path id="trail" fill="none" stroke-linecap="round"></path>
      <!-- Four circles, drawn outward-in, because a single bright dot on a
           nine-colour track is only reliably visible on some of it:
             car-pulse  a ping that expands and fades, so the eye is caught by
                        MOVEMENT and does not have to find the dot first
             car-halo   a dark disc the car sits on. This is what makes the
                        marker independent of the sector underneath it -- the
                        car used to be cyan on a sky-blue S6 and an indigo S7
             car        the car colour itself, with the glow
             car-core   a white centre, the one thing on the map that is pure
                        white, so the exact position is unambiguous -->
      <circle id="car-pulse" r="0" fill="none">
        <animate id="car-ping-r" attributeName="r" dur="1.9s"
                 repeatCount="indefinite" calcMode="spline"
                 keyTimes="0;1" keySplines="0.2 0.6 0.4 1"/>
        <animate attributeName="opacity" values="0.6;0" dur="1.9s"
                 repeatCount="indefinite"/>
      </circle>
      <circle id="car-halo" r="0"></circle>
      <circle id="car" r="0" filter="url(#glow)"></circle>
      <circle id="car-core" r="0" fill="#ffffff"></circle>
    </svg>
"""

MAP_JS = r"""
"use strict";
const NS = "http://www.w3.org/2000/svg";
const svg = document.getElementById("svg");
svg.setAttribute("viewBox", DATA.viewBox);

const el = (id) => document.getElementById(id);
const mk = (tag, attrs) => {
  const n = document.createElementNS(NS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
};

// One casing path under everything: the sectors are drawn as separate coloured
// cores on top, and without a continuous dark ribbon beneath them the joins
// between sectors show as notches.
el("g-casing").appendChild(mk("path", {
  d: DATA.sectors.map(s => s.d).join(" "),
  fill: "none", stroke: "#1e293b", "stroke-width": DATA.style.casing,
  "stroke-linecap": "round", "stroke-linejoin": "round",
}));

DATA.sectors.forEach(s => {
  el("g-sectors").appendChild(mk("path", {
    d: s.d, fill: "none", stroke: s.color, "stroke-width": DATA.style.core,
    "stroke-linecap": "round", "stroke-linejoin": "round",
  }));
});

DATA.gates.forEach(g => {
  if (g.dist === 0) return;                 // the finish line is drawn below
  el("g-gates").appendChild(mk("line", {
    x1: g.x1, y1: g.y1, x2: g.x2, y2: g.y2, class: "gate",
  }));
});
el("g-gates").appendChild(mk("line", {
  x1: DATA.finish.x1, y1: DATA.finish.y1,
  x2: DATA.finish.x2, y2: DATA.finish.y2, class: "finish",
}));

// Every ETCR turn by number, as on the ETCR track map.
(DATA.turns || []).forEach(t => {
  const g = el("g-turns");
  g.appendChild(mk("line", { x1: t.x1, y1: t.y1, x2: t.x, y2: t.y,
                             class: "turn-tick" }));
  g.appendChild(mk("circle", { cx: t.x, cy: t.y, r: DATA.style.turnR,
                               class: "turn-badge" }));
  const n = mk("text", { x: t.x, y: t.y, class: "turn-num",
                         "font-size": DATA.style.turnFont });
  n.textContent = t.n;
  g.appendChild(n);
});

DATA.sectorLabels.forEach(s => {
  const t = mk("text", { x: s.x, y: s.y, class: "s-label", fill: s.color,
                         "font-size": DATA.style.sFont });
  t.textContent = s.text;
  el("g-slabels").appendChild(t);
});

const car = el("car");
const CAR_R = DATA.style.car;
car.setAttribute("r", CAR_R);
car.setAttribute("fill", DATA.carColor);
el("car-core").setAttribute("r", CAR_R * 0.34);
// The dark disc under the car. Near-opaque rather than solid so the track
// still reads faintly through it and the marker looks like it is ON the
// circuit rather than punched through it.
el("car-halo").setAttribute("r", CAR_R * (DATA.style.carHalo || 1.38));
el("car-halo").setAttribute("fill", "rgba(3, 7, 13, 0.82)");
el("car-halo").setAttribute("stroke", "rgba(255, 255, 255, 0.28)");
el("car-halo").setAttribute("stroke-width", CAR_R * 0.07);
// The ping. Sized here rather than in the markup because the radii are in
// track metres and only DATA knows the scale.
const pulse = el("car-pulse");
pulse.setAttribute("stroke", DATA.carColor);
pulse.setAttribute("stroke-width", CAR_R * 0.16);
el("car-ping-r").setAttribute(
  "values", (CAR_R * 1.05) + ";" + (CAR_R * (DATA.style.carPulse || 2.4)));
el("trail").setAttribute("stroke-width", DATA.style.core);

const legend = el("legend");
if (legend) {
  DATA.sectors.forEach(s => {
    const d = document.createElement("div");
    d.className = "leg";
    d.id = "leg-" + s.id;
    d.innerHTML = '<span class="swatch" style="background:' + s.color +
                  ';color:' + s.color + '"></span><b>S' + s.id + '</b> ' + s.name;
    legend.appendChild(d);
  });
}

// Sector widths are the real lengths, S9 included: its range ends at 0 m (the
// finish line is both the last boundary and the first), so its length has to be
// taken the long way round or it comes out negative.
const progress = el("progress");
if (progress) {
  DATA.sectors.forEach(s => {
    const len = ((s.end - s.start) % DATA.trackLength + DATA.trackLength)
                % DATA.trackLength || DATA.trackLength;
    const d = document.createElement("div");
    d.className = "seg";
    d.id = "seg-" + s.id;
    d.style.background = s.color;
    d.style.width = (len / DATA.trackLength * 100) + "%";
    progress.insertBefore(d, el("progress-mark"));
  });
}

// The same model as track_map.position_at_distance: walk the baked cumulative
// distances and interpolate between the two vertices that straddle it. Doing it
// off the SVG path length instead would drift.
function posAt(d) {
  const L = DATA.trackLength, cum = DATA.cum, line = DATA.line;
  d = ((d % L) + L) % L;
  let lo = 0, hi = cum.length - 1;
  while (lo < hi - 1) {
    const mid = (lo + hi) >> 1;
    if (cum[mid] <= d) lo = mid; else hi = mid;
  }
  const span = cum[lo + 1] - cum[lo];
  const t = span > 0 ? (d - cum[lo]) / span : 0;
  const a = line[lo], b = line[(lo + 1) % line.length];
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
}

// A GPS fix in SVG user units. DATA.geo is track.to_local_xy with the same
// y-flip and origin the map was baked with, so this lands on the drawn track
// rather than near it.
function geoToSvg(lat, lon) {
  const g = DATA.geo;
  return [(lon - g.lon0) * g.mPerDegLon - g.ox,
          g.oy - (lat - g.lat0) * g.mPerDegLat];
}

// How far round the lap a point is, by projecting it onto the centreline: the
// nearest point on the nearest segment, then that segment's baked distance.
//
// This is what makes the map GPS-truth rather than odometer-truth. The car's
// lap_distance_m is a distance SINCE A DATUM, so a stale datum (a Pi restarted
// mid-lap, a trip reset in the garage) puts the marker in the wrong corner
// while GPS knows exactly where the car is. Returns metres, or null if there
// is no usable fix.
function trackDistAt(lat, lon) {
  if (lat == null || lon == null) return null;
  const [px, py] = geoToSvg(lat, lon);
  const line = DATA.line, cum = DATA.cum;
  let best = null;
  for (let i = 0; i < line.length - 1; i++) {
    const a = line[i], b = line[i + 1];
    const vx = b[0] - a[0], vy = b[1] - a[1];
    const len2 = vx * vx + vy * vy;
    const t = len2 > 0
      ? Math.max(0, Math.min(1, ((px - a[0]) * vx + (py - a[1]) * vy) / len2))
      : 0;
    const dx = px - (a[0] + vx * t), dy = py - (a[1] + vy * t);
    const d2 = dx * dx + dy * dy;
    if (best == null || d2 < best[0]) best = [d2, cum[i] + (cum[i + 1] - cum[i]) * t];
  }
  return best == null ? null : ((best[1] % DATA.trackLength) + DATA.trackLength)
                               % DATA.trackLength;
}

function sectorAt(d) {
  const L = DATA.trackLength;
  d = ((d % L) + L) % L;
  // S9 ends on the finish line (end 0 m), so its start > end: allow wrapping.
  for (const s of DATA.sectors) {
    if (s.start < s.end ? (d >= s.start && d < s.end)
                        : (d >= s.start || d < s.end)) return s;
  }
  return DATA.sectors[DATA.sectors.length - 1];
}

// The next named corner ahead, wrapping past the finish line. TRACK_LANDMARKS
// is the pit's own list, so the name and the speed are the ones the strategy
// engine uses -- not a caption invented for a web page.
function nextLandmark(d) {
  const L = DATA.trackLength, lms = DATA.landmarks;
  if (!lms.length) return null;
  d = ((d % L) + L) % L;
  for (const lm of lms) if (lm.dist > d) return [lm, lm.dist - d];
  return [lms[0], L - d + lms[0].dist];
}

// The tail behind the car, rebuilt each frame from the real centreline so it
// hugs the corners instead of cutting across them.
function trailPath(d) {
  const step = DATA.style.trail / 7;
  const pts = [];
  for (let k = 7; k >= 0; k--) pts.push(posAt(d - step * k));
  return "M " + pts.map(p => p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" L ");
}

// hh:mm:ss in the viewer's timezone, for marking WHEN something happened.
function fmtClockTime(epochS) {
  if (epochS == null || !isFinite(epochS)) return "—";
  const d = new Date(epochS * 1000), p = n => String(n).padStart(2, "0");
  return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}

function fmtAgeShort(sec) {
  if (sec == null || !isFinite(sec)) return "—";
  const s = Math.floor(sec);
  if (s < 90) return s + "s";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m";
  const h = Math.floor(m / 60);
  return h < 24 ? h + "h " + (m % 60) + "m" : Math.floor(h / 24) + "d";
}

// Rounded to tenths ONCE, then split -- see the wall's fmtLapTime. Splitting
// first and rounding the remainder showed a 239.96 s lap as "3:60.0", and on
// this page it would sit there until the car finished the next one.
function fmtTime(s) {
  if (s == null) return "—";
  const tenths = Math.round(Math.max(0, s) * 10);
  const r = (tenths % 600) / 10;
  return Math.floor(tenths / 600) + ":" + (r < 10 ? "0" : "") + r.toFixed(1);
}

function fmtClock(s) {
  if (s == null) return "—";
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h + "h " + (m < 10 ? "0" : "") + m + "m";
}

let lastSector = null;
// Moves the car and everything that follows it. Returns the sector it is in, so
// the caller can label it without repeating the lookup.
function paintMap(dist, fixXY) {
  // The marker sits on the true GPS point when there is one, and on the
  // centreline at `dist` when there is not. Everything that follows it -- the
  // trail, the sector, the progress bar -- is a distance concept and stays on
  // `dist`, which the live pages already derive from the same fix.
  const [x, y] = fixXY || posAt(dist);
  // All four circles of the marker move together. The pulse is animating its
  // own `r` in SMIL while this sets its centre; the two do not collide.
  ["car", "car-core", "car-halo", "car-pulse"].forEach(id => {
    const n = el(id);
    if (!n) return;
    n.setAttribute("cx", x); n.setAttribute("cy", y);
  });

  const s = sectorAt(dist);
  el("trail").setAttribute("stroke", s.color);
  el("trail").setAttribute("d", trailPath(dist));
  el("trail").setAttribute("opacity", "0.55");

  if (progress) {
    // The bar is laid out S1..S9 from the start/finish line.
    const L = DATA.trackLength;
    el("progress-mark").style.left = ((dist % L + L) % L / L * 100) + "%";
  }
  if (s.id !== lastSector) {
    DATA.sectors.forEach(o => {
      const n = el("leg-" + o.id);
      if (n) n.classList.toggle("on", o.id === s.id);
      const g = el("seg-" + o.id);
      if (g) g.classList.toggle("on", o.id === s.id);
    });
    lastSector = s.id;
  }
  return s;
}
"""

ICON_LINK = """<!-- The car, drawn as a side silhouette: the green body, the bubble
     canopy and the line of track under it. Inline SVG rather than a
     file, so each page stays one self-contained document that works
     from a USB stick with no second request to fail on venue wifi.
     Flat, not gradient: at 16px a gradient only bands. -->
<link rel="icon" href="data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2064%2064%22%3E%3Crect%20width%3D%2264%22%20height%3D%2264%22%20rx%3D%2214%22%20fill%3D%22%230b1220%22%2F%3E%3Cpath%20d%3D%22M3%2043%20C5%2037%2011%2033%2019%2032%20C23%2021%2039%2019%2045%2030%20C53%2031%2060%2034%2061%2038%20C62%2041%2060%2043%2056%2043%20L5%2043%20C3.6%2043%203%2043.4%203%2043%20Z%22%20fill%3D%22%232fd45f%22%2F%3E%3Cpath%20d%3D%22M21.5%2031%20C25%2022%2037%2021%2042%2029%20Z%22%20fill%3D%22%23cdf3ff%22%2F%3E%3Ccircle%20cx%3D%2217%22%20cy%3D%2243%22%20r%3D%225.6%22%20fill%3D%22%230b1220%22%2F%3E%3Ccircle%20cx%3D%2217%22%20cy%3D%2243%22%20r%3D%222.4%22%20fill%3D%22%232fd45f%22%2F%3E%3Ccircle%20cx%3D%2246%22%20cy%3D%2243%22%20r%3D%224%22%20fill%3D%22%230b1220%22%2F%3E%3Ccircle%20cx%3D%2246%22%20cy%3D%2243%22%20r%3D%221.7%22%20fill%3D%22%232fd45f%22%2F%3E%3Crect%20x%3D%224%22%20y%3D%2248.5%22%20width%3D%2256%22%20height%3D%222.4%22%20rx%3D%221.2%22%20fill%3D%22%2322406e%22%2F%3E%3C%2Fsvg%3E">
<meta name="theme-color" content="#06090f">"""

FONT_LINK = """<!-- Progressive enhancement only, and deliberately NOT render-blocking: these
     pages get opened on hostile wifi, and a plain stylesheet link holds first
     paint until the request resolves. Loaded as print media and promoted on
     load, the page draws immediately in the fallback face. -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;600;700&display=swap"
      rel="stylesheet" media="print" onload="this.media='all'">
<noscript><link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;600;700&display=swap" rel="stylesheet"></noscript>"""

BANNER = """<!--
  GENERATED FILE - DO NOT EDIT BY HAND.
  Written by tools/build_zolder_animation.py; re-run that to change anything
  here, or your edit is gone the next time anyone does.

  The circuit, the nine sectors, the turn names and their speeds are read from
  the modules the pit dashboard itself uses - track.py, track_map.py,
  strategy_engine.py - so these pages cannot quietly disagree with the rest of
  the system about where a sector starts or how fast a corner is taken.
-->"""


# --------------------------------------------------------------------------- #
# Page 1 - the presentation piece
# --------------------------------------------------------------------------- #
DEMO_TEMPLATE = r"""<!DOCTYPE html>
__BANNER__
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Circuit Zolder &mdash; Afeka Solar &amp; Electric Racing</title>
__ICON__
__FONT_LINK__
<style>
__BASE_CSS__
  html, body { overflow: hidden; }
  #stage { height: 100%; width: 100%; display: grid;
           grid-template-columns: minmax(260px, 22%) 1fr; gap: 8px; }
  #rail { padding: 26px 18px 18px 30px; display: flex; flex-direction: column;
          gap: 22px; min-width: 0; }
  .readout { border-left: 3px solid var(--accent); padding-left: 16px; }
  .readout .label { font-size: 0.72rem; letter-spacing: 3px; }
  .readout .value { font-size: 4.4rem; line-height: 0.95;
                    text-shadow: 0 0 26px rgba(0, 229, 255, 0.18); }
  .readout .unit { font-size: 1.2rem; color: var(--accent); }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px 12px; }
  #sector-name { color: var(--accent); }
  .spacer { flex: 1 1 auto; }
  .foot { font-size: 0.7rem; color: #64748b; line-height: 1.55;
          border-top: 1px solid #1e293b; padding-top: 10px; }
  #speedctl {
    position: absolute; left: 16px; top: 16px; display: flex; gap: 6px;
    align-items: center; background: var(--panel); border: 1px solid var(--line);
    border-radius: 8px; padding: 6px 10px; font-size: 0.72rem;
    letter-spacing: 2px; color: var(--dim); text-transform: uppercase;
    font-weight: 600;
  }
  #speedctl button { font: inherit; letter-spacing: 1px; color: var(--dim);
    cursor: pointer; background: transparent; border: 1px solid var(--line);
    border-radius: 5px; padding: 3px 9px; }
  #speedctl button.on { color: #06090f; background: var(--accent);
                        border-color: var(--accent); }
  @media (max-width: 900px) {
    #stage { grid-template-columns: 1fr; grid-template-rows: auto 1fr; }
    #rail { padding: 16px 16px 0; }
    .readout .value { font-size: 3rem; }
    #legend { grid-template-columns: repeat(2, auto); font-size: 0.72rem; }
  }
</style>
</head>
<body>
<div id="stage">
  <div id="rail">
    <div>
      <div class="eyebrow">Afeka Solar &amp; Electric Racing</div>
      <h1>Circuit <span>Zolder</span></h1>
    </div>
    <div class="readout">
      <div class="label">Speed</div>
      <div class="value"><span id="speed">&mdash;</span><span class="unit">KM/H</span></div>
    </div>
    <div class="grid2">
      <div><div class="label">Lap distance</div>
        <div class="value"><span id="dist">&mdash;</span><span class="unit">M</span></div></div>
      <div><div class="label">Lap</div><div class="value"><span id="lap">&mdash;</span></div></div>
      <div><div class="label">Lap time</div><div class="value"><span id="laptime">&mdash;</span></div></div>
      <div><div class="label">Last lap</div><div class="value"><span id="lastlap">&mdash;</span></div></div>
    </div>
    <div>
      <div class="label">Sector</div>
      <div class="value small" id="sector-name">&mdash;</div>
    </div>
    <div>
      <div class="label">Next</div>
      <div class="value small" id="next-name">&mdash;</div>
      <div class="sub" id="next-sub">&mdash;</div>
    </div>
    <div>
      <div class="label">Lap progress</div>
      <div id="progress"><div id="progress-mark"></div></div>
      <div class="sub" id="progress-sub">&mdash;</div>
    </div>
    <div class="spacer"></div>
    <div class="foot" id="foot"></div>
  </div>
  <div id="map">
__MAP_SVG__
    <div id="legend"></div>
    <div id="speedctl"><span>Demo</span></div>
  </div>
</div>
<script>
const DATA = __DATA__;
</script>
<script>
__MAP_JS__

el("foot").innerHTML =
  DATA.attribution +
  "<br>Demo lap driven by the team's base velocity profile, built from a lap " +
  "the car logged at Zolder &mdash; lap " +
  DATA.profileLapSeconds.toFixed(1) + " s.";

function speedAt(d) {
  const p = DATA.profile, L = DATA.trackLength;
  d += DATA.docOffset;                // 0: the profile counts from the car's own zero
  d = ((d % L) + L) % L;
  let lo = 0, hi = p.length - 1;
  while (lo < hi - 1) {
    const mid = (lo + hi) >> 1;
    if (p[mid][0] <= d) lo = mid; else hi = mid;
  }
  const span = p[lo + 1][0] - p[lo][0];
  const t = span > 0 ? (d - p[lo][0]) / span : 0;
  return p[lo][1] + (p[lo + 1][1] - p[lo][1]) * t;
}

let external = false, dist = 0, lapNo = 1, lapStart = 0, lastLap = null;
let demoClock = 0, rate = 1, target = null, shownSpeed = null;

function paint(speed) {
  const s = paintMap(dist);
  el("dist").textContent = Math.round(dist).toLocaleString();
  el("speed").textContent = speed == null ? "—" : speed.toFixed(0);
  el("lap").textContent = lapNo;
  el("sector-name").textContent = "S" + s.id + " · " + s.name;
  el("lastlap").textContent = fmtTime(lastLap);
  el("progress-sub").textContent =
    (dist / DATA.trackLength * 100).toFixed(0) + "% of " +
    DATA.trackLength.toLocaleString() + " m";
  const nx = nextLandmark(dist);
  if (nx) {
    el("next-name").textContent = nx[0].name;
    el("next-sub").textContent = Math.round(nx[1]).toLocaleString() + " m ahead" +
      (nx[0].speed == null ? "" : " · max " + nx[0].speed + " km/h");
  }
}

let prev = null;
function frame(now) {
  const dt = prev == null ? 0 : Math.min(0.25, (now - prev) / 1000);
  prev = now;
  let speed;
  if (external) {
    if (target != null) {
      let delta = target - dist;
      const L = DATA.trackLength;
      if (delta < -L / 2) delta += L;
      if (delta > L / 2) delta -= L;
      dist = ((dist + delta * Math.min(1, dt * 4)) % L + L) % L;
    }
    speed = shownSpeed;
  } else {
    demoClock += dt * rate;
    speed = speedAt(dist);
    dist += (speed / 3.6) * dt * rate;
    if (dist >= DATA.trackLength) {
      dist -= DATA.trackLength;
      lastLap = demoClock - lapStart;
      lapStart = demoClock;
      lapNo += 1;
    }
    el("laptime").textContent = fmtTime(demoClock - lapStart);
  }
  paint(speed);
  requestAnimationFrame(frame);
}

const ctl = el("speedctl");
[1, 2, 4].forEach(r => {
  const b = document.createElement("button");
  b.textContent = "×" + r;
  b.className = r === 1 ? "on" : "";
  b.onclick = () => {
    rate = r;
    ctl.querySelectorAll("button").forEach(o => o.classList.remove("on"));
    b.classList.add("on");
  };
  ctl.appendChild(b);
});

// The same message shape this page has always accepted. A field that is absent
// shows as an em dash and never as zero.
window.addEventListener("message", (event) => {
  const d = event.data;
  if (!d || d.type !== "UPDATE_TELEMETRY") return;
  if (!external) {
    external = true;
    ctl.style.display = "none";
    el("laptime").textContent = "—";
  }
  const L = d.trackLengthMeters || DATA.trackLength;
  if (d.lapDistanceMeters != null) {
    target = ((d.lapDistanceMeters / L) * DATA.trackLength) % DATA.trackLength;
  }
  shownSpeed = (d.speedKmh == null ? null : Number(d.speedKmh));
  if (d.lap != null) lapNo = d.lap;
  if (d.lapTimeSeconds != null) el("laptime").textContent = fmtTime(d.lapTimeSeconds);
  if (d.lastLapSeconds != null) lastLap = d.lastLapSeconds;
});

requestAnimationFrame(frame);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Page 2 - the spectator page, for people who are not at the circuit
# --------------------------------------------------------------------------- #
# WHAT THIS PAGE MUST NEVER DO: pretend. It is watched by people with no other
# source of information about the car, who cannot tell a frozen animation from
# a slow lap. So every number here is either a reading the car actually
# published or an em dash, the car stops moving the moment the feed goes stale,
# and the status pill says which of those is happening in plain words.
SPECTATOR_TEMPLATE = r"""<!DOCTYPE html>
__BANNER__
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Afeka Solar Racing Live</title>
<meta name="description" content="Follow the Afeka Solar &amp; Electric Racing team live from the iESC 24-hour race at Circuit Zolder.">
__ICON__
__FONT_LINK__
<style>
__BASE_CSS__
  body { min-height: 100%; height: auto; }
  #stage {
    display: grid; grid-template-columns: 360px 1fr;
    gap: 14px; padding: 16px; height: 100vh;
  }
  #rail { display: flex; flex-direction: column; gap: 14px;
          min-width: 0; overflow-y: auto; }
  .card {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 10px; padding: 13px 15px;
  }
  /* Only on the phone page: a badge that pulses is how a card nobody is
     watching catches the corner of an eye. The wall does not, because on a TV
     that runs for 24 hours it is just movement. */
  .chg { animation: pulse 1.6s ease-in-out infinite; }
  /* The IN THE PIT sign. Big enough to be the first thing read after the car
     stops, and in the page's own accent rather than a warning colour: a pit
     stop is the plan, not a problem. */
  #pitsign {
    margin-top: 10px; padding: 8px 12px; border-radius: 10px;
    background: rgba(0, 229, 255, 0.12); border: 1px solid var(--accent);
  }
  #pitsign .mark {
    display: block; color: var(--accent); font-weight: 700;
    font-size: 0.95rem; letter-spacing: 3px; text-transform: uppercase;
  }
  #pitsign .why {
    display: block; color: var(--dim); font-size: 0.8rem; font-weight: 600;
    margin-top: 2px;
  }
  .pill {
    display: inline-flex; align-items: center; gap: 8px;
    border-radius: 999px; padding: 5px 13px; font-size: 0.72rem;
    letter-spacing: 2.5px; text-transform: uppercase; font-weight: 700;
    border: 1px solid var(--line); color: var(--dim);
  }
  .pill .dot { width: 8px; height: 8px; border-radius: 50%;
               background: currentColor; }
  .pill.live { color: var(--good); border-color: var(--good); }
  .pill.live .dot { animation: pulse 1.6s ease-in-out infinite; }
  .pill.stale { color: var(--warn); border-color: var(--warn); }
  .pill.off { color: var(--bad); border-color: var(--bad); }
  /* Loud on purpose. A pit wall showing invented numbers without saying so is
     worse than a blank one: somebody walks past it mid-race and reads it as
     real. Filled, not outlined, so it cannot be mistaken for a status pill. */
  .pill.demo { color: #0b1220; background: var(--warn); border-color: var(--warn);
               font-weight: 700; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.25; } }
  .hero { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .hero .value { font-size: 3.1rem; line-height: 1;
                 text-shadow: 0 0 26px rgba(0, 229, 255, 0.18); }
  .hero .unit { font-size: 1rem; color: var(--accent); }
  .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .bar { position: relative; height: 10px; border-radius: 5px;
         background: #0f172a; overflow: hidden; margin-top: 7px; }
  .bar > i { position: absolute; left: 0; top: 0; bottom: 0; display: block;
             border-radius: 5px; background: var(--accent); width: 0; }
  #soc-bar > i { background: var(--good); }
  /* Both clocks, named. "Your time" was worse than it looks: it depends on
     whatever device the page happens to be opened on, so the same family got
     different numbers on a phone abroad and a laptop at home, and neither
     matched the 13:00 every official announcement quotes. Naming the zones
     removes the ambiguity entirely. */
  .tz { display: grid; grid-template-columns: 1fr 1fr;
        margin-top: 11px; border-top: 1px solid #1e293b; padding-top: 10px; }
  .tz .z { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
  .tz .z + .z { border-left: 1px solid #1e293b; padding-left: 14px; }
  .tz .name { font-size: 0.6rem; letter-spacing: 2.4px; text-transform: uppercase;
              color: var(--dim); font-weight: 700; }
  .tz .time { font-size: 1.15rem; font-weight: 700; line-height: 1.15;
              font-variant-numeric: tabular-nums; }
  .tz .date { font-size: 0.72rem; color: var(--dim);
              font-variant-numeric: tabular-nums; }
  #race-bar > i { background: var(--accent); }
  #sector-name { color: var(--accent); }
  .foot { font-size: 0.7rem; color: #64748b; line-height: 1.55; }
  #map { border: 1px solid var(--line); border-radius: 10px;
         background: rgba(6, 9, 15, 0.4); overflow: hidden; }
  /* The car stops being drawn as a live thing the moment the feed is not. */
  #car, #car-core, #trail { transition: opacity 0.4s ease; }
  body.stale #car, body.stale #car-core, body.stale #trail { opacity: 0.28; }
  /* A reading too old to show at all (CONFIG.oldAfterS): no car on the map. */
  body.old #car, body.old #car-core, body.old #trail { opacity: 0; }
  /* Hidden by sliding UP out of view, but the offset is in PIXELS, not a
     percentage of its own height. It was -140%, and an EMPTY banner is only
     about 24px tall (padding, no line box), so -140% lifted it just 34px from a
     22px offset and left a cyan sliver of itself permanently visible at the top
     of the page -- on every load, before any milestone had happened. It looked
     correct in testing only because a banner WITH text is tall enough for the
     percentage to clear the viewport. opacity is belt and braces: even if a
     browser disagrees about the transform, an invisible banner cannot leak. */
  #milestone {
    position: fixed; left: 50%; top: 22px;
    transform: translateX(-50%) translateY(-200px);
    opacity: 0; pointer-events: none;
    background: linear-gradient(90deg, #0ea5e9, #22d3ee);
    color: #04121a; font-weight: 700; letter-spacing: 2px;
    text-transform: uppercase; padding: 12px 26px; border-radius: 999px;
    box-shadow: 0 14px 40px rgba(34, 211, 238, 0.35);
    transition: transform 0.5s cubic-bezier(.2,.9,.3,1.2), opacity 0.35s ease;
    z-index: 50; font-size: 0.95rem; text-align: center;
  }
  #milestone.show { transform: translateX(-50%) translateY(0); opacity: 1; }
  /* -- official timing ---------------------------------------------------- */
  /* The stage is a viewport-tall app; this sits under it, so the page now
     scrolls where it did not before. Hence the cue in the rail -- a section
     nobody scrolls to is the same as a section that is not there. */
  #timing { padding: 0 16px 26px; max-width: 1500px; margin: 0 auto; }
  .t-head { display: flex; align-items: flex-end; justify-content: space-between;
            gap: 14px; flex-wrap: wrap; margin-bottom: 10px; }
  .t-head h2 { margin: 3px 0 0; font-size: 1.25rem; font-weight: 700; }
  .t-open { font-size: 0.8rem; font-weight: 700; color: var(--accent);
            text-decoration: none; border: 1px solid var(--line);
            border-radius: 999px; padding: 6px 14px; white-space: nowrap; }
  .t-open:hover { border-color: var(--accent); }
  /* A light surface on purpose. The provider's page is light-themed, and an
     unannounced white rectangle on a dark page reads as a rendering fault --
     framed as a panel with its own border, it reads as a quoted document. */
  #t-frame { position: relative; background: #fff; border: 1px solid var(--line);
             border-radius: 10px; overflow: hidden;
             height: min(74vh, 880px); }
  #t-frame iframe { display: block; width: 100%; height: 100%; border: 0; }
  #t-note { position: absolute; inset: 0; display: flex; flex-direction: column;
            align-items: center; justify-content: center; gap: 10px;
            text-align: center; padding: 24px; background: var(--panel);
            color: var(--dim); font-size: 0.9rem; font-weight: 600; }
  .t-credit { margin-top: 9px; font-size: 0.72rem; color: #64748b;
              line-height: 1.55; }
  .t-credit a { color: #64748b; }
  /* The cue that there is anything below the fold at all. */
  #t-jump { display: inline-block; margin-top: 10px; font-size: 0.72rem;
            font-weight: 700; letter-spacing: 2.5px; text-transform: uppercase;
            color: var(--dim); text-decoration: none; border: 1px solid var(--line);
            border-radius: 999px; padding: 5px 13px; }
  #t-jump:hover { color: var(--accent); border-color: var(--accent); }
  @media (max-width: 900px) {
    #stage { grid-template-columns: 1fr; height: auto; padding: 12px; }
    #map { height: 56vh; }
    #legend { display: none; }
    .hero .value { font-size: 2.6rem; }
    #timing { padding: 0 12px 20px; }
    #t-frame { height: 78vh; }
  }
</style>
</head>
<body>
<div id="milestone"></div>
<div id="stage">
  <div id="rail">
    <div>
      <div class="eyebrow">Afeka Solar &amp; Electric Racing</div>
      <h1>Live from <span>Zolder</span></h1>
      <div style="margin-top:10px"><span class="pill off" id="status">
        <span class="dot"></span><span id="status-text">Connecting</span></span></div>
      <div class="sub" id="status-sub">&mdash;</div>
      <!-- IN THE PIT. The one line on this page written for somebody who does
           not follow racing: a car that has stopped moving is the moment a
           family watching from home starts to worry, and "in the pit" is the
           answer in three words. It is DERIVED, never typed: the reasons
           underneath are the two things the system actually knows -- a driver
           change the crew flagged, and the charger the car itself detects. No
           third button to remember in the box, and nothing here can claim the
           car is in the pits on its own. -->
      <div id="pitsign" hidden>
        <span class="mark">IN THE PIT</span>
        <span class="why" id="pitwhy"></span>
      </div>
      <!-- Hidden until the count is both known and non-zero. A page that
           cannot reach the count must not claim an empty grandstand, and the
           viewer reading this is themselves proof the number is never 0. -->
      <div class="sub" id="watching" style="display:none"></div>
      <a id="t-jump" href="#timing">Official timing &darr;</a>
    </div>

    <div class="card hero">
      <div>
        <div class="label">Lap</div>
        <div class="value"><span id="lap">&mdash;</span></div>
      </div>
      <div>
        <div class="label">Speed</div>
        <div class="value"><span id="speed">&mdash;</span><span class="unit">KM/H</span></div>
      </div>
    </div>

    <!-- Hidden until the pit types a name OR flags a driver change. There is
         no default driver, and a swap can begin before anyone has typed one. -->
    <div class="card" id="driver-card" style="display:none">
      <div class="label">Driving now <span id="swap" class="chg swap" hidden>DRIVER CHANGE</span></div>
      <div class="value small" id="driver-name"></div>
      <div class="sub" id="swap-sub" hidden></div>
    </div>

    <div class="card">
      <div class="label">Where the car is</div>
      <div class="value small" id="sector-name">&mdash;</div>
      <div class="sub" id="next-sub">&mdash;</div>
      <div class="sub" id="gps-mark">&mdash;</div>
      <div id="progress"><div id="progress-mark"></div></div>
    </div>

    <div class="card" id="race-card">
      <div class="label" id="race-label">Race clock</div>
      <div class="value small"><span id="race-elapsed">&mdash;</span><span
        class="sub" style="display:inline" id="race-of"> of <span
        id="race-total">24h</span></span></div>
      <div class="bar" id="race-bar"><i></i></div>
      <div class="sub" id="race-sub">&mdash;</div>
      <div class="tz">
        <div class="z">
          <span class="name">Zolder &middot; BE</span>
          <span class="time" id="tz-be-time">&mdash;</span>
          <span class="date" id="tz-be-date">&mdash;</span>
        </div>
        <div class="z">
          <span class="name">Israel</span>
          <span class="time" id="tz-il-time">&mdash;</span>
          <span class="date" id="tz-il-date">&mdash;</span>
        </div>
      </div>
    </div>

    <div class="card row">
      <div>
        <div class="label">Distance</div>
        <div class="value small"><span id="odo">&mdash;</span><span class="unit">KM</span></div>
      </div>
      <div>
        <div class="label">Last lap</div>
        <div class="value small" id="lastlap">&mdash;</div>
      </div>
    </div>

    <div class="card">
      <div class="label">Battery <span id="charging" class="chg" hidden>CHARGING</span></div>
      <div class="value small"><span id="soc">&mdash;</span><span class="unit">%</span></div>
      <div class="bar" id="soc-bar"><i></i></div>
      <div class="sub" id="charging-sub" hidden>The car is in the pits on the charger.</div>
    </div>

    <div class="card">
      <div class="label">At the circuit</div>
      <div class="value small" id="weather">&mdash;</div>
      <div class="sub" id="daynight">&mdash;</div>
    </div>

    <div class="foot" id="foot"></div>
  </div>

  <div id="map">
__MAP_SVG__
    <div id="legend"></div>
  </div>
</div>

<!-- The official classification, as published by the timing provider. We frame
     their page; we do not read their feed and republish the numbers. Two
     reasons, and the first one settles it on its own: their robots.txt
     disallows /lt and /signalr, which is where the standings live. And these
     results are the race's official record -- a copy of them served from our
     page could disagree with the real one at exactly the moment it matters,
     and ours is the one nobody can correct. -->
<section id="timing">
  <div class="t-head">
    <div>
      <div class="eyebrow">Official classification</div>
      <h2>iESC Live Timing &middot; Circuit Zolder</h2>
    </div>
    <a class="t-open" href="__TIMING_URL__" target="_blank" rel="noopener noreferrer">
      Open the full timing &#8599;</a>
  </div>
  <div id="t-frame">
    <div id="t-note">Live timing loads when you scroll here.</div>
  </div>
  <div class="t-credit">
    Live timing &copy; __TIMING_CREDIT__, embedded with attribution from
    <a href="__TIMING_URL__" target="_blank" rel="noopener noreferrer">livetiming.getraceresults.com</a>.
    These are the official results; Afeka Solar &amp; Electric Racing neither
    produces nor verifies them. Everything else on this page is our own car's
    telemetry and is not official timing.
  </div>
</section>

<script>
const DATA = __DATA__;
const CONFIG = __CONFIG__;
</script>
<script>
__MAP_JS__

el("foot").innerHTML = DATA.attribution +
  "<br>The car publishes once a second. Every value here is a reading the car " +
  "actually sent &mdash; a dash means it did not send one, never zero.";

// ── state ──────────────────────────────────────────────────────────────── //
// `snap` is the last thing the car published, verbatim. Nothing here ever
// invents a value to fill a gap in it.
const CIRCUIT_TZ = "Europe/Brussels";   // the clock every announcement uses
const HOME_TZ = "Asia/Jerusalem";       // where most of the people watching are
let snap = null;
let lastRxWall = 0;        // our clock, for "how long since anything arrived"
let dist = 0, target = null, everPainted = false;
// The GPS marker, in SVG units: where the last fix put it, and where it is
// drawn (eased toward the first, so a 1 Hz feed reads as motion).
let fixTarget = null, fixShown = null;
// The age of the car's last fix and the car-clock instant it was sampled at.
let gpsAge = null, gpsSampleTs = null;
let race = { start: CONFIG.raceStart, end: CONFIG.raceEnd };
let sun = null;

const num = (v) => (v == null || v === "" || isNaN(Number(v))) ? null : Number(v);

// Is the last snapshot too old to show (CONFIG.oldAfterS)? Judged on the car's
// own sample time, not on when this page received it: the stream delivers the
// retained snapshot the moment the page opens, however old it is. A snapshot
// with no time at all cannot be shown to be current, so it counts as old.
function isOld(now) {
  if (snap == null) return false;
  const t = num(snap.ts);
  return t == null || now - t > CONFIG.oldAfterS;
}

function fmtCountdown(s) {
  s = Math.max(0, Math.floor(s));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return d + "d " + h + "h";
  if (h > 0) return h + "h " + (m < 10 ? "0" : "") + m + "m";
  return m + "m " + (s % 60) + "s";
}

// Both clocks, each named. Not "your time": that silently depends on the device
// the page is opened on, so the same family saw different numbers on a phone
// abroad and a laptop at home, and neither matched the 13:00 in the official
// timetable. Naming Zolder and Israel means nobody has to work out which is
// which, or convert anything.
function renderZones(ts) {
  const zones = [["tz-be", CIRCUIT_TZ], ["tz-il", HOME_TZ]];
  for (const [id, tz] of zones) {
    let t = "—", d = "—";
    try {
      const when = new Date(ts * 1000);
      t = when.toLocaleTimeString("en-GB",
            { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false });
      d = when.toLocaleDateString("en-GB",
            { timeZone: tz, weekday: "short", day: "numeric", month: "short" });
    } catch (e) { /* an engine without this zone: dashes, never a wrong time */ }
    el(id + "-time").textContent = t;
    el(id + "-date").textContent = d;
  }
}

// Sunrise/sunset formatted at the CIRCUIT, whoever is reading. "Sunset 19:47"
// is a fact about Zolder, not about where the viewer happens to be sitting.
function hhmmAtCircuit(date) {
  try {
    return date.toLocaleTimeString(undefined, { timeZone: CIRCUIT_TZ,
      hour: "2-digit", minute: "2-digit", hour12: false });
  } catch (e) { return date.toTimeString().slice(0, 5); }
}
const dash = (v, digits, suffix) => v == null ? "—"
  : v.toLocaleString(undefined, { minimumFractionDigits: digits,
                                  maximumFractionDigits: digits }) + (suffix || "");

// ── the feed ───────────────────────────────────────────────────────────── //
// RTDB's REST stream: one connection, the server pushes on every change. If it
// cannot be opened (an old browser, a proxy that eats text/event-stream) we
// fall back to polling the same URL, which is slower but works everywhere.
const LIVE_URL = CONFIG.dbUrl + "/" + CONFIG.publicPath + ".json";

function applySnapshot(obj) {
  if (obj == null) return;
  snap = obj;
  lastRxWall = Date.now() / 1000;
  // POSITION, GPS FIRST. lap_distance_m is a distance since a datum, and the
  // datum is wrong whenever the Pi restarted mid-lap or the trip was reset off
  // the line -- the marker then sits in a corner the car is nowhere near. A fix
  // needs no datum. It is still the fallback, because a car in a tunnel, in the
  // garage or with a dead receiver must not take the map down with it.
  const lat = num(obj.lat), lon = num(obj.lon);
  const gpsDist = trackDistAt(lat, lon);
  fixTarget = gpsDist == null ? null : geoToSvg(lat, lon);
  // The car sends the age of its last fix even when it withholds a stale
  // position, so the page can say WHEN rather than just "no GPS".
  gpsAge = num(obj.gps_age_s);
  gpsSampleTs = num(obj.ts);
  if (fixTarget == null) fixShown = null;
  const d = gpsDist != null ? gpsDist : num(obj.lap_distance_m);
  if (d != null) {
    target = ((d % DATA.trackLength) + DATA.trackLength) % DATA.trackLength;
    if (!everPainted) { dist = target; everPainted = true; }
  }
  checkMilestones(obj);
  render();
}

function startStream() {
  let es;
  try { es = new EventSource(LIVE_URL); } catch (e) { return startPolling(); }
  let opened = false;
  es.addEventListener("open", () => { opened = true; });
  const onEvent = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (!msg) return;
    if (msg.path === "/") applySnapshot(msg.data);
    else if (snap && msg.path) {          // a patch to one field
      snap[msg.path.replace(/^\//, "")] = msg.data;
      applySnapshot(snap);
    }
  };
  es.addEventListener("put", onEvent);
  es.addEventListener("patch", onEvent);
  es.addEventListener("error", () => {
    // Never opened at all -> streaming is not available here, poll instead.
    // Opened once and dropped -> EventSource reconnects by itself; leave it.
    if (!opened) { es.close(); startPolling(); }
  });
}

let polling = false;
function startPolling() {
  if (polling) return;
  polling = true;
  const tick = () => fetch(LIVE_URL, { cache: "no-store" })
    .then(r => r.ok ? r.json() : null)
    .then(applySnapshot)
    .catch(() => {});
  tick();
  setInterval(tick, 2000);
}

// The race window, if anyone has published one. Optional: without it the clock
// card hides rather than counting down to a guess.
function loadRace() {
  fetch(CONFIG.dbUrl + "/" + CONFIG.racePath + ".json", { cache: "no-store" })
    .then(r => r.ok ? r.json() : null)
    .then(r => {
      if (r && num(r.start_ts)) race = { start: num(r.start_ts), end: num(r.end_ts) };
      render();
    }).catch(() => {});
}

// The driver's name, typed in at the pit (Pit_Web writes /public/driver). Only
// a real name shows the card; no name, a deleted node or a failed read hides
// it. textContent, never innerHTML: this is text someone typed.
let driverName = "";
let swapSince = null;      // when the pit said the change began, or null

function loadDriver() {
  fetch(CONFIG.dbUrl + "/" + CONFIG.driverPath + ".json", { cache: "no-store" })
    .then(r => r.ok ? r.json() : null)
    .then(d => {
      driverName = d && typeof d.name === "string" ? d.name.trim() : "";
      // === true, not truthy: this node is written by the pit and read by
      // strangers, and a stray string must not turn the badge on.
      swapSince = d && d.changing === true ? num(d.since) : null;
      paintDriver(Date.now() / 1000);
    }).catch(() => {});
}

// Who is driving, and whether they are being swapped out right now.
//
// The swap half exists because this page used to show the old driver and a car
// sitting still, with nothing to say why -- which reads as a broken car to the
// family this page is for. The pit says when a change starts; the same press
// that starts the next driver's stint ends it.
//
// NOTHING HERE EXPIRES IT. The team asked for a flag that stays up until they
// take it down, so the page states how long it has been up instead of quietly
// deciding the crew must have forgotten.
function paintDriver(now) {
  const swapping = swapSince != null;
  el("driver-name").textContent = driverName || (swapping ? "Changing over" : "");
  el("driver-card").style.display = (driverName || swapping) ? "" : "none";
  el("swap").hidden = !swapping;
  el("swap-sub").hidden = !swapping;
  if (swapping) {
    el("swap-sub").textContent =
      "In the pits for a driver change · " + fmtAgeShort(Math.max(0, now - swapSince));
  }
}

// ── who else is watching ───────────────────────────────────────────────── //
// This page writes ONE key saying "still here", and reads ONE integer saying
// how many such keys are fresh. It never reads the other viewers' keys: that
// would be every viewer downloading every other viewer, which is quadratic and
// would eat the database's monthly transfer allowance by mid-race. The pit
// collector does the counting and the sweeping.
const VIEWER_ID = (() => {
  // Per tab, kept across a refresh. Without the sessionStorage half, a viewer
  // pressing F5 leaves their old key behind and counts twice until it expires.
  let id = null;
  try { id = sessionStorage.getItem("viewerId"); } catch (e) {}
  if (!id) {
    id = Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
    try { sessionStorage.setItem("viewerId", id); } catch (e) {}
  }
  return id;
})();
const VIEWER_URL = CONFIG.dbUrl + "/" + CONFIG.viewersPath + "/" + VIEWER_ID + ".json";

// {".sv":"timestamp"} is the server's clock, not ours. It matters: a phone an
// hour out of sync would otherwise be swept as stale the moment it arrived, or
// linger long after it left.
function beat() {
  fetch(VIEWER_URL, { method: "PUT", body: '{".sv":"timestamp"}' }).catch(() => {});
}

// There is deliberately no "goodbye" delete on pagehide. It would drop a closed
// tab out of the count in a second rather than in VIEWER_STALE_MS -- but the
// only rule that permits it is one that lets ANY visitor delete ANY key, and
// then one person with the developer console can hold the whole grandstand at
// zero. A number that can be silently pushed down is worth less than a number
// that lags a closed tab by forty seconds, on a page people leave open for
// hours. So the collector's sweep is the only way a key ever goes away, and
// the database rule refuses deletes from the browser outright.

function loadWatching() {
  fetch(CONFIG.dbUrl + "/" + CONFIG.viewersCountPath + ".json", { cache: "no-store" })
    .then(r => r.ok ? r.json() : null)
    .then(n => {
      const card = el("watching");
      // Not a number means the collector is not running or we could not ask;
      // either way we do not know, and "0 watching" would be a lie told to at
      // least one person -- the one reading it.
      if (typeof n !== "number" || !isFinite(n) || n < 1) {
        card.style.display = "none";
        return;
      }
      card.textContent = n === 1 ? "1 person watching" : n + " people watching";
      card.style.display = "";
    })
    .catch(() => {});
}

// ── the official timing embed ──────────────────────────────────────────── //
// Not loaded with the page. It is a third-party document plus a live SignalR
// connection, on a page whose first rule is that it comes up on bad venue wifi
// -- so it costs nothing until somebody actually scrolls to it.
function mountTiming() {
  const host = el("t-frame");
  if (!host || host.dataset.mounted) return;
  host.dataset.mounted = "1";
  const note = el("t-note");
  note.textContent = "Loading the official timing…";

  const f = document.createElement("iframe");
  f.title = "iESC live timing at Circuit Zolder, by __TIMING_CREDIT__";
  f.loading = "lazy";
  // No sandbox: the standings arrive over SignalR and sandboxing has to permit
  // so much to keep that working that it protects nothing worth the risk of
  // silently breaking their page. It is cross-origin, so it cannot read ours.
  f.src = "__TIMING_URL__";

  // A cross-origin frame will not tell us it failed, so treat silence as
  // failure: if nothing has loaded by the time this fires, say so plainly and
  // leave the viewer a link that definitely works. Better than a white box.
  const giveUp = setTimeout(() => {
    note.innerHTML = "";
    const a = document.createElement("a");
    a.href = "__TIMING_URL__";
    a.target = "_blank"; a.rel = "noopener noreferrer";
    a.className = "t-open";
    a.textContent = "Open the official timing ↗";
    note.appendChild(document.createTextNode(
      "The timing provider is not answering from here."));
    note.appendChild(a);
  }, 20000);

  f.addEventListener("load", () => { clearTimeout(giveUp); note.style.display = "none"; });
  host.appendChild(f);
}

// IntersectionObserver where it exists; where it does not, the section simply
// loads at once rather than never.
if ("IntersectionObserver" in window) {
  const io = new IntersectionObserver((entries, obs) => {
    for (const e of entries) if (e.isIntersecting) { mountTiming(); obs.disconnect(); }
  }, { rootMargin: "240px" });
  io.observe(el("t-frame"));
} else {
  mountTiming();
}
// Following the rail's cue must not leave someone staring at a placeholder
// waiting for the observer to catch up.
el("t-jump").addEventListener("click", mountTiming);

// ── weather, straight from Open-Meteo, same source the pit uses ─────────── //
function loadWeather() {
  const u = "https://api.open-meteo.com/v1/forecast?latitude=" + CONFIG.lat +
            "&longitude=" + CONFIG.lon +
            "&current=temperature_2m,cloud_cover,wind_speed_10m" +
            // timeformat=unixtime is the important part. The default returns
            // naive strings like "2026-09-19T19:47", which JS parses in the
            // VIEWER's timezone -- so for anyone not in Belgium the day/night
            // state flipped at the wrong moment, and the page went on claiming
            // daylight after dark at the track. Unix timestamps are absolute
            // instants and cannot be misread that way.
            "&daily=sunrise,sunset&timezone=Europe%2FBrussels" +
            "&timeformat=unixtime&forecast_days=3";
  fetch(u).then(r => r.ok ? r.json() : null).then(w => {
    if (!w || !w.current) return;
    const c = w.current;
    el("weather").textContent =
      Math.round(c.temperature_2m) + "°C · " + Math.round(c.cloud_cover) +
      "% cloud · " + Math.round(c.wind_speed_10m) + " km/h wind";
    if (w.daily && w.daily.sunrise) {
      sun = { rise: w.daily.sunrise, set: w.daily.sunset };
      renderDayNight();
    }
  }).catch(() => {});
}

function renderDayNight() {
  if (!sun) return;
  const now = new Date();
  let label = null;
  for (let i = 0; i < sun.rise.length; i++) {
    // Unix seconds -> a real instant, so this comparison is right wherever the
    // reader is. It is answering "is it dark AT ZOLDER right now".
    const rise = new Date(sun.rise[i] * 1000), set = new Date(sun.set[i] * 1000);
    if (now >= rise && now < set) {
      label = "☀ Daylight at the circuit · sunset " + hhmmAtCircuit(set);
      break;
    }
    if (now < rise) {
      label = "🌙 Dark at the circuit · sunrise " + hhmmAtCircuit(rise);
      break;
    }
  }
  el("daynight").textContent = label || "—";
}

// ── milestones ─────────────────────────────────────────────────────────── //
// Only fired for transitions SEEN while the page is open. Someone opening the
// page on lap 137 should not be greeted by a stale celebration of lap 100.
let seenLap = null, seenKm = null, milestoneTimer = null;
function celebrate(text) {
  const m = el("milestone");
  m.textContent = text;
  m.classList.add("show");
  clearTimeout(milestoneTimer);
  milestoneTimer = setTimeout(() => m.classList.remove("show"), 9000);
}
function checkMilestones(obj) {
  const lap = num(obj.lap);
  if (lap != null) {
    if (seenLap != null && lap > seenLap && lap % 10 === 0) {
      celebrate("Lap " + lap + " complete");
    }
    seenLap = lap;
  }
  const km = num(obj.odometer_m) == null ? null : num(obj.odometer_m) / 1000;
  if (km != null) {
    if (seenKm != null && Math.floor(km / 100) > Math.floor(seenKm / 100)) {
      celebrate(Math.floor(km / 100) * 100 + " km covered");
    }
    seenKm = km;
  }
}

// ── rendering ──────────────────────────────────────────────────────────── //
function render() {
  const now = Date.now() / 1000;
  const age = lastRxWall ? now - lastRxWall : null;
  // Two clocks on purpose: `age` is how long since WE heard anything, and
  // sampleAge is how old the car says its own reading is. A phone with a wrong
  // clock skews the second, so the status is driven by the first.
  const stale = age == null || age > CONFIG.staleAfterS;
  const old = isOld(now);

  const pill = el("status");
  pill.className = "pill " + (age == null || old ? "off" : stale ? "stale" : "live");
  el("status-text").textContent =
    age == null ? "Waiting for the car" : old ? "Car not running"
                : stale ? "No data" : "Live";
  const sampleTs = snap ? num(snap.ts) : null;
  el("status-sub").textContent =
    age == null ? "Nothing has arrived yet — the car may not be running."
                : old ? "Last heard from " + (sampleTs == null ? "a while"
                          : fmtClock(now - sampleTs)) + " ago. Numbers will "
                          + "appear when the car is back on track."
                : stale ? "Nothing for " + Math.round(age) + "s. The car is out of "
                          + "contact; the marker below is where it was last seen."
                : "Updated " + Math.max(0, Math.round(age)) + "s ago";
  document.body.classList.toggle("stale", stale);
  document.body.classList.toggle("old", old);

  // An old reading is not shown at all: every value renders as a dash.
  const s = old ? {} : (snap || {});
  el("lap").textContent = num(s.lap) == null ? "—" : num(s.lap);
  el("speed").textContent = num(s.speed_kmh) == null ? "—"
                            : Math.round(num(s.speed_kmh));
  el("lastlap").textContent = fmtTime(num(s.last_lap_time_s));
  const odo = num(s.odometer_m);
  el("odo").textContent = odo == null ? "—" : dash(odo / 1000, 1);

  // Is the marker on GPS, and if not, when was the car last seen? Both the
  // instant and the elapsed time: a screenshot sent to somebody an hour later
  // still answers the question.
  const gmark = el("gps-mark");
  if (fixTarget != null) {
    gmark.textContent = "GPS live" + (gpsSampleTs == null ? ""
                                      : " · " + fmtClockTime(gpsSampleTs));
  } else if (gpsAge != null && gpsSampleTs != null) {
    gmark.textContent = "no GPS · last fix " + fmtClockTime(gpsSampleTs - gpsAge)
                        + " · " + fmtAgeShort(gpsAge) + " ago";
  } else {
    gmark.textContent = "position from lap distance";
  }

  const soc = num(s.soc_percent);
  el("soc").textContent = soc == null ? "—" : Math.round(soc);
  el("soc-bar").firstElementChild.style.width = (soc == null ? 0 : soc) + "%";

  // Charging, straight from the car's own detector (charge_detector.py). Read
  // with === true, not a truthiness test: `s` is the EMPTY object whenever the
  // reading is old or absent, and an undefined field must render as "not
  // charging" rather than throwing. The badge disappearing when the feed dies
  // is the right failure -- a page that keeps saying CHARGING for an hour
  // after the car left the box is worse than one that says nothing.
  const charging = s.is_charging === true;
  el("charging").hidden = !charging;
  el("charging-sub").hidden = !charging;

  // The driver card counts its swap up here rather than in loadDriver, which
  // only runs every 15 s: the badge says how long the change has been running,
  // and a minute counter that moves in fifteen-second steps looks broken.
  paintDriver(now);

  // IN THE PIT, from the two things that are known rather than guessed. A car
  // simply reading 0 km/h is NOT one of them: a car stopped out on the circuit
  // reads exactly the same, and telling the family it is in the pits when it
  // is stranded at Turn 12 is a worse answer than saying nothing.
  const why = [];
  if (swapSince != null) {
    why.push("driver change · " + fmtAgeShort(Math.max(0, now - swapSince)));
  }
  if (charging) why.push("on the charger");
  el("pitsign").hidden = why.length === 0;
  el("pitwhy").textContent = why.join(" · ");

  // Race clock. Hidden outright when nobody has published a window -- a
  // countdown to a date this page guessed would be worse than no countdown.
  // Three states, because for most of the time this page exists the race has
  // either not started or is over, and "0h 00m of 24h 00m" is a confusing way
  // to say "not yet" to someone who opened the link a fortnight early.
  const card = el("race-card");
  if (race.start && race.end) {
    card.style.display = "";
    const total = race.end - race.start;
    const bar = card.querySelector(".bar > i");
    el("race-total").textContent = fmtClock(total);
    if (now < race.start) {
      el("race-label").textContent = "Race starts in";
      el("race-elapsed").textContent = fmtCountdown(race.start - now);
      el("race-of").style.display = "none";
      el("race-sub").textContent = "Lights out";
      renderZones(race.start);
      bar.style.width = "0%";
    } else if (now >= race.end) {
      el("race-label").textContent = "Race complete";
      el("race-elapsed").textContent = fmtClock(total);
      el("race-of").style.display = "none";
      el("race-sub").textContent = "Chequered flag";
      renderZones(race.end);
      bar.style.width = "100%";
    } else {
      const done = now - race.start;
      el("race-label").textContent = "Race clock";
      el("race-elapsed").textContent = fmtClock(done);
      el("race-of").style.display = "inline";
      // Mid-race the useful instant is the FINISH, not the start.
      el("race-sub").textContent = fmtClock(total - done)
                                   + " remaining · chequered flag at";
      renderZones(race.end);
      bar.style.width = (done / total * 100) + "%";
    }
  } else {
    card.style.display = "none";
  }
}

// ── the smooth part ────────────────────────────────────────────────────── //
// The car reports once a second; easing toward the reported distance turns
// that into motion instead of a marker that jumps. When the feed is stale the
// easing stops with it: a frozen car is the honest picture of no data.
let prev = null;
function frame(t) {
  const dt = prev == null ? 0 : Math.min(0.25, (t - prev) / 1000);
  prev = t;
  const fresh = lastRxWall && (Date.now() / 1000 - lastRxWall) <= CONFIG.staleAfterS;
  if (target != null && fresh) {
    let delta = target - dist;
    const L = DATA.trackLength;
    if (delta < -L / 2) delta += L;
    if (delta > L / 2) delta -= L;
    dist = ((dist + delta * Math.min(1, dt * 2.2)) % L + L) % L;
  }
  if (everPainted && isOld(Date.now() / 1000)) {
    el("sector-name").textContent = "—";
    el("next-sub").textContent = "—";
  } else if (everPainted) {
    if (fixTarget != null) {
      const k = fixShown == null ? 1 : Math.min(1, dt * 2.2);
      fixShown = fixShown == null ? fixTarget
        : [fixShown[0] + (fixTarget[0] - fixShown[0]) * k,
           fixShown[1] + (fixTarget[1] - fixShown[1]) * k];
    }
    const s = paintMap(dist, fixShown);
    el("sector-name").textContent = "S" + s.id + " · " + s.name;
    const nx = nextLandmark(dist);
    if (nx) {
      el("next-sub").textContent = "Next: " + nx[0].name + " · " +
        Math.round(nx[1]).toLocaleString() + " m ahead";
    }
  }
  requestAnimationFrame(frame);
}

render();
startStream();
loadRace();
loadDriver();
loadWeather();
beat();
loadWatching();
setInterval(beat, CONFIG.viewerBeatMs);
setInterval(loadWatching, CONFIG.viewerPollMs);
setInterval(render, 1000);          // keeps the ages and the race clock moving
setInterval(loadWeather, 900000);
setInterval(renderDayNight, 60000);
setInterval(loadRace, 300000);
setInterval(loadDriver, 15000);
requestAnimationFrame(frame);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# The pit wall
# --------------------------------------------------------------------------- #
# A TV in the garage, read from across it. Three things drive every decision
# here and none of them apply to the other two pages:
#
#   IT IS READ FROM FOUR METRES AWAY.  Every size is in vw/clamp(), not px, so
#   the same file fills a 1080p monitor and a 4K panel. The spectator page uses
#   fixed px with one breakpoint, which is why it needs browser zoom on a TV.
#
#   NOBODY WILL BE TOUCHING IT.  No controls, no tabs, no hover. It reconnects
#   by itself and it never shows a modal anyone would have to dismiss.
#
#   A WRONG NUMBER IS WORSE THAN NO NUMBER.  The pit makes calls off this. Every
#   field that was carried forward from an earlier reading rather than sent now
#   is rendered as a dash, and the whole panel dims when the car goes quiet, so
#   nothing on screen can be mistaken for current when it is not.
WALL_TEMPLATE = r"""<!DOCTYPE html>
__BANNER__
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pit Wall — Afeka Solar Racing</title>
__ICON__
__FONT_LINK__
<style>
__BASE_CSS__
  /* Everything scales off the viewport so one file fits any panel -- the
     shared badge included, which is sized in rem for a phone. No animation on
     it here: on a TV that runs for 24 hours a pulsing badge is just movement. */
  .chg { margin-left: 0.5vw; padding: 0.15vw 0.5vw; font-size: 0.8vw;
         letter-spacing: 0.1vw; vertical-align: 0.1vw; }
  html, body { height: 100%; overflow: hidden; }
  #stage {
    display: grid; grid-template-columns: 1fr minmax(300px, 27vw);
    gap: 0.9vw; padding: 0.9vw; height: 100vh;
  }
  /* min-height:0 on both columns and on the map itself. Grid and flex items
     default to min-height:auto, which refuses to shrink below their content --
     and the map's content is a 1617x1614 SVG, so without these three the track
     runs off the bottom of the screen and takes the sector legend, the lap
     strip and the footer with it. */
  #left { display: flex; flex-direction: column; gap: 0.7vw;
          min-width: 0; min-height: 0; }
  /* A GRID, not a flex column. As a flex column the cards were free to shrink
     below their own content (min-height:0 is what lets the map fit), and the
     overflow then painted straight over the card below -- "Target 65 km/h" and
     the POWER row disappeared under its neighbours. Explicit rows plus
     overflow:hidden means a card can only ever clip its own content. */
  #rail { display: grid; gap: 0.7vw; min-width: 0; min-height: 0;
          grid-template-rows: auto auto auto auto minmax(0, 1fr); }
  .card {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 0.6vw; padding: 0.8vw 1vw; min-height: 0;
    overflow: hidden;
  }
  .card.grow { display: flex; flex-direction: column; min-height: 0; }
  /* -- top strip ------------------------------------------------------- */
  #top { display: flex; align-items: center; gap: 1.2vw; flex: 0 0 auto; }
  #top h1 { font-size: clamp(14px, 1.5vw, 34px); margin: 0; }
  #top .spacer { flex: 1 1 auto; }
  .pill {
    display: inline-flex; align-items: center; gap: 0.5vw;
    border-radius: 999px; padding: 0.35vw 1vw;
    font-size: clamp(10px, 0.85vw, 20px);
    letter-spacing: 0.2vw; text-transform: uppercase; font-weight: 700;
    border: 1px solid var(--line); color: var(--dim); white-space: nowrap;
  }
  .pill .dot { width: 0.65vw; height: 0.65vw; min-width: 6px; min-height: 6px;
               border-radius: 50%; background: currentColor; }
  /* The age ticks once a second forever, so it gets a box of its own that is
     already wide enough for the biggest number it will hold. Without this the
     pill re-measured on every tick and the whole strip twitched sideways --
     and it was worse than one nudge a second, because the age was printed to
     a tenth and so redrew ten times a second. tabular-nums stops the digits
     themselves changing width as they roll. Wide enough for four digits: past
     that the feed has been dead over an hour and one reflow is not the
     problem. */
  #statusage { display: inline-block; min-width: 6.5ch; text-align: left;
               font-variant-numeric: tabular-nums; }
  #statusage:empty { display: none; }
  .pill.live { color: var(--good); border-color: var(--good); }
  .pill.live .dot { animation: pulse 1.6s ease-in-out infinite; }
  .pill.stale { color: var(--warn); border-color: var(--warn); }
  .pill.off { color: var(--bad); border-color: var(--bad); }
  /* Filled, like DEMO: this one is not a state of the FEED, it is a thing the
     crew is doing, and it has to be tellable apart from the status pill
     beside it at the length of a garage. */
  .pill.swap { color: #0b1220; background: var(--accent); border-color: var(--accent); }
  /* Ticks once a second, same as #statusage above -- same treatment, for the
     same reason: the strip must not twitch sideways as the digits roll. */
  #swapage { font-variant-numeric: tabular-nums; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.25; } }
  /* -- the numbers ----------------------------------------------------- */
  .label { font-size: clamp(9px, min(0.72vw, 1.5vh), 16px);
           letter-spacing: 0.18vw; }
  .sub { font-size: clamp(10px, min(0.85vw, 1.7vh), 19px); }
  .huge {
    font-size: clamp(34px, min(7.2vw, 11vh), 170px); font-weight: 700;
    line-height: 0.95;
    font-variant-numeric: tabular-nums; letter-spacing: -0.02em;
  }
  .big {
    font-size: clamp(22px, min(3.4vw, 6vh), 80px); font-weight: 700;
    line-height: 1;
    font-variant-numeric: tabular-nums;
  }
  .mid {
    font-size: clamp(14px, min(1.7vw, 3.2vh), 40px); font-weight: 700;
    line-height: 1.1;
    font-variant-numeric: tabular-nums;
  }
  .unit { font-size: clamp(10px, min(1vw, 2vh), 22px); margin-left: 0.4vw; }
  .pair { display: grid; grid-template-columns: 1fr 1fr; gap: 0.7vw; }
  .quad { display: grid; grid-template-columns: 1fr 1fr; gap: 0.5vw 1vw; }
  .accent { color: var(--accent); }
  .good { color: var(--good); } .warn { color: var(--warn); }
  .bad  { color: var(--bad); }
  /* A value that is not current is never drawn as if it were. */
  .stale-val { color: var(--dim); opacity: 0.45; }
  /* Stopped from the pit or the car, not stale: dimmed so the number
     reads as parked rather than as a page that has stopped updating. */
  .held-val { opacity: 0.62; }
  /* -- battery bar ----------------------------------------------------- */
  .bar { position: relative; height: 0.9vw; min-height: 8px; border-radius: 0.45vw;
         background: #0f172a; overflow: hidden; margin-top: 0.5vw; }
  .bar > i { display: block; height: 100%; width: 0%; background: var(--good);
             transition: width 0.4s ease, background 0.4s ease; }
  /* -- lap list -------------------------------------------------------- */
  /* flex:1 1 auto so the list fills its card. Without it the list is only as
     tall as flex-shrink leaves it, the trim below measures against that
     smaller box, and rows get dropped while visible space sits empty under
     them. */
  #laps { flex: 1 1 auto; overflow: hidden; margin-top: 0.4vw; min-height: 0; }
  .lap-row {
    display: grid; grid-template-columns: auto 1fr auto;
    gap: 0.6vw; align-items: baseline;
    font-size: clamp(11px, min(1.15vw, 2.1vh), 26px);
    font-variant-numeric: tabular-nums;
    padding: 0.18vw 0; border-bottom: 1px solid rgba(51, 65, 85, 0.45);
  }
  .lap-row b { color: var(--dim); font-weight: 600; }
  .lap-row .t { font-weight: 700; }
  /* -- faults ---------------------------------------------------------- */
  #faults {
    display: none; flex: 0 0 auto; background: rgba(248, 113, 113, 0.14);
    border: 1px solid var(--bad); color: #fecaca; border-radius: 0.5vw;
    padding: 0.5vw 1vw; font-weight: 700; letter-spacing: 0.12vw;
    font-size: clamp(11px, 1.05vw, 24px);
  }
  #faults.show { display: block; }
  /* The whole screen fades when the car stops talking: visible from across
     the garage without anyone having to read a word of it. */
  #stage.dead #left, #stage.dead #rail { opacity: 0.34; filter: grayscale(0.75); }
  /* A pulsing marker says "this is live". On a dead feed it is a lie told in
     motion, which is the hardest kind to ignore, so it stops. */
  #stage.dead #car-pulse { display: none; }
  #stage.dead { transition: none; }
  #map { flex: 1 1 auto; min-height: 0; }
  #legend { font-size: clamp(9px, 0.78vw, 17px); right: 1vw; bottom: 1vw;
            padding: 0.6vw 1vw; }
  #foot { flex: 0 0 auto; display: flex; gap: 1.2vw; align-items: baseline;
          font-size: clamp(10px, 0.85vw, 19px); color: var(--dim); }
  #foot .spacer { flex: 1 1 auto; }
</style>
</head>
<body>
<div id="stage">
  <div id="left">
    <div id="top">
      <div>
        <div class="eyebrow">Afeka Solar &amp; Electric Racing</div>
        <h1>Pit <span>Wall</span></h1>
      </div>
      <div class="spacer"></div>
      <div id="demo" class="pill demo" hidden>Demo &mdash; not live data</div>
      <!-- Up while the pit has a driver change flagged, and it says for how
           long. Nothing expires it: the crew asked for a flag that stays until
           they clear it, so the wall states its age rather than second-guess. -->
      <div id="swap" class="pill swap" hidden>Driver change <span id="swapage"></span></div>
      <div id="race" class="pill">Race <span id="raceclock">&mdash;</span></div>
      <div id="status" class="pill off"><span class="dot"></span><span id="statustext">Connecting</span><span id="statusage"></span></div>
    </div>
    <div id="faults"></div>
    <div class="card grow" id="map">
      __MAP_SVG__
      <div id="legend"></div>
    </div>
    <div id="progress"><div id="progress-mark"></div></div>
    <div id="foot">
      <span>Next: <b id="nextcorner" style="color:var(--text)">&mdash;</b></span>
      <span>Sector <b id="sector" style="color:var(--text)">&mdash;</b></span>
      <span>Pos <b id="gpsmark" style="color:var(--text)">&mdash;</b></span>
      <span class="spacer"></span>
      <span id="src">&mdash;</span>
    </div>
  </div>

  <div id="rail">
    <div class="card">
      <div class="label">Speed</div>
      <div class="huge"><span id="speed">&mdash;</span><span class="unit accent">KM/H</span></div>
      <div class="sub">Target <span id="target">&mdash;</span> km/h</div>
    </div>

    <div class="card">
      <div class="pair">
        <div>
          <div class="label">Lap</div>
          <div class="big accent" id="lap">&mdash;</div>
        </div>
        <div>
          <div class="label" id="laptime-label">This lap</div>
          <div class="big" id="laptime">&mdash;</div>
        </div>
      </div>
      <div class="sub">Last <span id="lastlap">&mdash;</span> &middot; <span id="lastwh">&mdash;</span>
           &nbsp;&middot;&nbsp; This lap <span id="lapwh">&mdash;</span></div>
    </div>

    <div class="card">
      <div class="label">Battery <span id="charging" class="chg" hidden>CHARGING</span></div>
      <div class="big" id="soc">&mdash;<span class="unit">%</span></div>
      <div class="bar"><i id="socbar"></i></div>
      <div class="quad" style="margin-top:0.6vw">
        <div><div class="label">Pack</div><div class="mid" id="packv">&mdash;</div></div>
        <div><div class="label">Current</div><div class="mid" id="packa">&mdash;</div></div>
        <div style="grid-column: span 2"><div class="label">Power</div><div class="mid" id="power">&mdash;</div></div>
      </div>
    </div>

    <div class="card">
      <div class="quad">
        <div><div class="label">Motor temp</div><div class="mid" id="mtemp">&mdash;</div></div>
        <div><div class="label">Pack temp</div><div class="mid" id="btemp">&mdash;</div></div>
        <div><div class="label">Race energy</div><div class="mid" id="energy">&mdash;</div></div>
        <div><div class="label">Regen</div><div class="mid" id="regen">&mdash;</div></div>
      </div>
    </div>

    <div class="card grow">
      <div class="label">Completed laps <span id="strategy" style="float:right"></span></div>
      <div id="laps"></div>
    </div>
  </div>
</div>

<script>
const DATA = __DATA__;
const CONFIG = __CONFIG__;
__MAP_JS__

// ── the feed ───────────────────────────────────────────────────────────── //
// Plain polling, not SSE. The spectator page streams from Firebase because it
// is talking to the internet across the world; this is a LAN hop to a process
// on the next table, where a 1 s poll costs 0.3 ms of SQLite and cannot get
// stuck in a half-open stream that needs a human to notice and reload.
let snap = null;          // the last payload that arrived
let snapAt = 0;           // performance.now() when it did
let failures = 0;

function poll() {
  fetch("/live.json", { cache: "no-store" })
    .then(r => r.ok ? r.json() : Promise.reject(r.status))
    .then(j => { snap = j; snapAt = performance.now(); failures = 0; })
    .catch(() => { failures++; })
    .finally(() => setTimeout(poll, CONFIG.pollMs));
}

// Age is measured from the SERVER's own clock difference, plus however long
// ago this browser received it. A TV with a wrong clock -- which is most TVs --
// therefore cannot make a live car look stale or a dead one look live.
function ageOf(s) {
  if (!s || s.device_ts == null || s.served_ts == null) return null;
  return (s.served_ts - s.device_ts) + (performance.now() - snapAt) / 1000;
}

// The PIT SERVER's clock, carried forward since the snapshot arrived. Used for
// anything stamped by the pit rather than by the car: the TV this runs on may
// have no network time at all, and a wall clock an hour out would age a
// two-minute driver change into an hour-long one.
function serverNow(s) {
  if (!s || s.served_ts == null) return null;
  return s.served_ts + (performance.now() - snapAt) / 1000;
}

// A field the car is no longer sending. carried_ts only holds fields that came
// from last_known instead of the newest row, so this is exactly "the car has
// gone quiet about this one thing" -- a dead sensor on an otherwise live car.
function isCarried(s, name) {
  const t = s && s.carried_ts ? s.carried_ts[name] : undefined;
  if (t == null) return false;
  return (s.device_ts - t) > CONFIG.staleAfterS;
}

function num(v) {
  const n = typeof v === "number" ? v : parseFloat(v);
  return Number.isFinite(n) ? n : null;
}

// One place decides whether a value may be drawn, so no tile can forget.
function put(id, s, field, fmt) {
  const node = el(id);
  if (!node) return null;
  const v = isCarried(s, field) ? null : num(s ? s[field] : null);
  node.textContent = v == null ? "—" : fmt(v);
  node.classList.toggle("stale-val", v == null);
  return v;
}

// ROUNDED TO TENTHS ONCE, THEN SPLIT. Taking the minute off first and rounding
// what was left put an impossible "3:60.0" on the TV: a 239.96 s lap had its
// 3 minutes removed, and the 59.96 s remainder then rounded up to 60.0. It sat
// there for the whole of the following lap, and the big clock flickered
// through it at most minute boundaries. The HUD has always truncated for the
// same reason (driver_dash_v2._lap_time_text); this is the same rule in JS.
function fmtLapTime(sec) {
  if (sec == null || !isFinite(sec)) return "—";
  const tenths = Math.round(Math.max(0, sec) * 10);
  const r = (tenths % 600) / 10;
  return Math.floor(tenths / 600) + ":" + (r < 10 ? "0" : "") + r.toFixed(1);
}

function fmtDelta(d) {
  if (d == null) return "—";
  return (d >= 0 ? "+" : "−") + Math.abs(d).toFixed(1) + " s";
}

// ── painting ───────────────────────────────────────────────────────────── //
function render() {
  const s = snap;
  const age = ageOf(s);
  const dead = s == null || age == null || age > CONFIG.staleAfterS;
  el("stage").classList.toggle("dead", dead);

  const pill = el("status");
  pill.className = "pill " + (dead ? (failures > 2 ? "off" : "stale") : "live");
  // Word and number are separate elements: the number is the only part that
  // changes on a tick, and it redraws inside a fixed box, so nothing on the
  // strip moves. Whole seconds -- a tenth of a second is not a fact anyone
  // acts on, and it made the readout flicker at 10 Hz.
  const showAge = s != null && failures <= 2 && age != null;
  el("statustext").textContent =
    s == null        ? "Connecting"
    : failures > 2   ? "Pit wall server unreachable"
    : age == null    ? "No reading"
    : dead           ? "No signal"
                     : "Live";
  el("statusage").textContent = showAge ? " · " + Math.round(age) + "s" : "";

  if (!s) return;
  el("demo").hidden = !s.demo;

  // A driver change the pit has flagged (Pit_Web writes it, pit_wall.py reads
  // it back out of app_state). The wall and the public page are showing the
  // same instant, so they cannot disagree about whether a swap is on.
  const swapSince = num(s.driver_change_since);
  const swapNow = serverNow(s);
  el("swap").hidden = swapSince == null;
  el("swapage").textContent =
    swapSince == null || swapNow == null
      ? "" : "· " + fmtAgeShort(Math.max(0, swapNow - swapSince));

  // -- speed, battery, power ------------------------------------------- //
  put("speed",  s, "mms_vehicle_speed_kmh", v => Math.round(v));
  put("target", s, "target_speed_kmh",      v => Math.round(v));
  // Charging, from the car's own detector. isCarried() matters here as much as
  // on any number: a charging flag held over from the last row the car sent is
  // not evidence the car is on the charger NOW, and this badge's only job is to
  // explain a car that is stopped at this moment.
  el("charging").hidden = isCarried(s, "is_charging") || num(s.is_charging) !== 1;
  const soc = put("soc", s, "bms_soc_percent", v => Math.round(v) + "%");
  const bar = el("socbar");
  bar.style.width = (soc == null ? 0 : Math.max(0, Math.min(100, soc))) + "%";
  bar.style.background = soc == null ? "var(--line)"
                       : soc < 20 ? "var(--bad)"
                       : soc < 40 ? "var(--warn)" : "var(--good)";
  put("packv", s, "bms_voltage_V",    v => v.toFixed(1) + " V");
  put("packa", s, "bms_current_A",    v => v.toFixed(1) + " A");
  put("power", s, "mms_power_W",      v => (v / 1000).toFixed(2) + " kW");
  // The MOTOR's own PT1000. This read mms_temperature_C -- the CONTROLLER's
  // internal sensor -- under a "Motor temp" label for its whole life, so the
  // one number on the wall that says how hard the motor is working was never
  // the motor's.
  put("mtemp", s, "mms_motor_temp_C", v => Math.round(v) + "°C");
  put("btemp", s, "battery_temp_C",    v => Math.round(v) + "°C");
  // WATT-HOURS. lap_tracker sends total_energy_wh and the store keeps Wh, so
  // printing " kWh" beside the raw value put a 4238 Wh race on the wall as
  // "4238.27 kWh". No decimals either: a whole-race total is four digits, and
  // the hundredths were only ever noise on a screen read from ten feet away.
  put("energy", s, "total_race_energy", v => Math.round(v) + " Wh");
  put("regen",  s, "regen_energy",      v => Math.round(v) + " Wh");

  // -- lap -------------------------------------------------------------- //
  const lap = num(s.calculated_lap);
  el("lap").textContent = lap == null ? "—" : Math.round(lap);

  // THE LAP CLOCK IS NOT WORKED OUT HERE, and that is the point. The
  // dashboard's header asks Pit_Dashboard/lap_clock.py for it and pit_wall.py
  // hands this page that same function's answer, so the TV and the dashboard
  // cannot show two different lap times -- which they could, and did, while
  // this page subtracted two of the car's own wall-clock stamps in JavaScript
  // and the header followed a rule nothing here had ever heard of.
  //
  // What arrives with it: the pit's own presses, at the instant they are made.
  // Stop lap clock parks this clock and Cut lap sends it back to 0:00 without
  // the TV sitting out the four to five seconds the car takes to confirm.
  //
  // IT DOES NOT STOP WHEN THE FEED DOES. The lap did not pause because the
  // telemetry did, and on this link a clock that stopped with it is a clock
  // nobody can use. The LABEL carries what the freeze used to say -- that the
  // car has confirmed nothing for a while, and for how long -- so a number
  // counting on the pit's clock alone is never read as one the car sent.
  const lc = (s && s.lap_clock) || {};
  const started = num(lc.startedAt), parked = num(lc.heldAt);
  const pitNow = serverNow(s);
  const running = started == null ? null
                : parked != null ? parked - started
                : pitNow == null ? null : pitNow - started;
  const quiet = started != null && parked == null
                && (age == null || age > CONFIG.dataStaleAfterS);
  el("laptime").textContent = fmtLapTime(running);
  el("laptime").classList.toggle("stale-val", running == null);
  // A clock that merely stops moving reads as a frozen page, so it says which.
  el("laptime").classList.toggle("held-val", parked != null);
  el("laptime-label").textContent =
      parked != null ? "This lap · held"
    : quiet ? "This lap · no data " + fmtAgeShort(age)
    : "This lap";

  const last = isCarried(s, "last_lap_time_s") ? null : num(s.last_lap_time_s);
  el("lastlap").textContent = fmtLapTime(last);

  // The strategy is NAMED on the wall but no longer SCORED against: the lap
  // list used to print every lap's delta to the active profile's target, which
  // made the profile the yardstick even when the pit was deliberately off it.
  el("strategy").textContent = s.active_strategy ? s.active_strategy : "";

  // What this lap has cost so far, beside the clock counting it. The pit
  // subtracts a baseline for this (pit_wall.Feed._lap_energy) and sends null
  // whenever the answer would be a guess -- so a dash here means "not known",
  // never "cheap lap".
  const lapWh = num(s.lap_energy_wh);
  el("lapwh").textContent = (dead || lapWh == null) ? "—" : lapWh.toFixed(1) + " Wh";

  // What the lap that just finished cost, beside the time it took. Guarded
  // like the lap time next to it and NOT blanked on a dead feed: a completed
  // lap is a fact that stays true while the link is down, unlike the running
  // lap above, which is still being measured and so goes to a dash.
  const lastWh = isCarried(s, "last_lap_energy") ? null : num(s.last_lap_energy);
  el("lastwh").textContent = lastWh == null ? "—" : lastWh.toFixed(1) + " Wh";

  // -- faults ----------------------------------------------------------- //
  const notes = [];
  if (num(s.bms_has_error)) notes.push("BMS fault " + (s.bms_error_code ?? "?"));
  if (num(s.mms_has_error)) notes.push("Motor fault " + (s.mms_error_code ?? "?"));
  const fnode = el("faults");
  fnode.textContent = notes.join("   ·   ");
  fnode.classList.toggle("show", notes.length > 0);

  // -- the map ---------------------------------------------------------- //
  // GPS first, exactly as on the spectator page: a fix needs no datum, so it
  // survives a Pi restart mid-lap and a trip reset taken off the line, both of
  // which leave lap_distance_m pointing at the wrong corner.
  //
  // But ONLY a current fix. The car goes on serving its last known position
  // after the receiver loses lock -- deliberately, a frozen dot beats an empty
  // map on the driver's screen -- so gps_age_s is what separates "here" from
  // "here half an hour ago". Without this gate the marker sits still while
  // lap_distance_m, which is live, says the car is two laps down the road.
  // isCarried is a different guard and both are needed: it catches a field the
  // car has STOPPED sending, not one it keeps resending unchanged.
  const fixAge = isCarried(s, "gps_age_s") ? null : num(s.gps_age_s);
  const haveFix = fixAge != null && fixAge <= CONFIG.gpsMaxAgeS
                  && !isCarried(s, "lat") && !isCarried(s, "lon");
  const lat = haveFix ? num(s.lat) : null, lon = haveFix ? num(s.lon) : null;
  const gpsDist = trackDistAt(lat, lon);
  const fixXY = gpsDist == null ? null : geoToSvg(lat, lon);
  const dist = gpsDist != null ? gpsDist
             : (isCarried(s, "lap_distance_m") ? null : num(s.lap_distance_m));
  if (dist != null) {
    const sec = paintMap(dist, fixXY);
    el("sector").textContent = "S" + sec.id + " " + sec.name;
    const nx = nextLandmark(dist);
    el("nextcorner").textContent = nx
      ? nx[0].name + (nx[0].speed != null ? " · " + nx[0].speed + " km/h" : "") +
        " in " + Math.round(nx[1]) + " m"
      : "—";
  }

  // Where the marker came from, and -- when it is not GPS -- the instant of the
  // last fix as well as its age. A wall photographed and sent to someone an hour
  // later still says when the car was last seen.
  const gm = el("gpsmark");
  if (haveFix) {
    gm.textContent = "GPS live · " + fmtClockTime(s.device_ts);
    gm.style.color = "var(--ok, #35d07f)";
  } else if (fixAge != null) {
    gm.textContent = "no fix · last " + fmtClockTime(s.device_ts - fixAge)
                     + " · " + fmtAgeShort(fixAge) + " ago";
    gm.style.color = "var(--warn, #ffb300)";
  } else {
    gm.textContent = dist == null ? "—" : "by distance";
    gm.style.color = "var(--text)";
  }

  el("src").textContent =
    "store " + (s.store_mode || "?") +
    (s.lap_source ? " · lap by " + s.lap_source : "") +
    (s.error ? " · " + s.error : "");

  // -- completed laps --------------------------------------------------- //
  const box = el("laps");
  const rows = s.recent_laps || [];
  box.innerHTML = rows.length ? "" : '<div class="sub">No completed laps yet.</div>';
  // The car against ITSELF: each lap's delta to the lap before it, which is
  // the one on the row below (the list is newest first). No profile involved,
  // so the column keeps meaning something when the pit is deliberately off the
  // strategy. The oldest row shown has nothing below it and gets no delta --
  // it is not a zero.
  rows.forEach((r, i) => {
    const d = document.createElement("div");
    d.className = "lap-row";
    const prev = rows[i + 1];
    const gap = (r.time_s != null && prev && prev.time_s != null)
                ? r.time_s - prev.time_s : null;
    const cls = gap == null ? "sub" : gap > 0 ? "sub warn" : "sub accent";
    d.innerHTML = "<b>" + r.lap + "</b>" +
                  '<span class="t">' + fmtLapTime(r.time_s) + "</span>" +
                  '<span class="' + cls + '" style="margin:0">' +
                  (gap == null ? "" : fmtDelta(gap)) + "</span>";
    box.appendChild(d);
  });
  // Drop whole rows rather than let the card clip one through the middle. The
  // card already hides its overflow, so nothing can escape it -- this is only
  // about a half-drawn lap time looking like a fault on a screen whose whole
  // job is that a fault is obvious.
  // Measured against the list's own box. offsetTop would be wrong here: it is
  // relative to the nearest POSITIONED ancestor, not to #laps, so it read far
  // too large and deleted every row.
  const room = box.getBoundingClientRect().bottom;
  while (box.lastElementChild &&
         box.lastElementChild.getBoundingClientRect().bottom > room + 0.5) {
    box.removeChild(box.lastElementChild);
  }
}

// ── the race clock ─────────────────────────────────────────────────────── //
// The race clock counts only while the pit says the race is running.
// race_start_time survives in app_state long after a session ends, so counting
// from it unconditionally put "RACE 1321H 26M ELAPSED" on the wall -- a clock
// that is obviously broken is still a clock somebody has to stop and think
// about, in a garage where the whole point is being read at a glance.
function renderRace() {
  const node = el("raceclock");
  const s = snap;
  el("race").classList.toggle("live", !!(s && s.is_racing));
  if (!s || !s.race_start_time) { node.textContent = "not started"; return; }
  const elapsed = (Date.now() / 1000) - s.race_start_time;
  if (elapsed < 0) { node.textContent = "starts in " + fmtClock(-elapsed); return; }
  if (!s.is_racing) { node.textContent = "not started"; return; }
  node.textContent = fmtClock(elapsed) + " elapsed";
}

poll();
// Repaint faster than the feed so the lap clock and the age counter move
// smoothly; every repaint draws the same payload until a new one lands.
setInterval(render, 100);
setInterval(renderRace, 1000);
render();
</script>
</body>
</html>
"""


def render_demo(data):
    return (DEMO_TEMPLATE
            .replace("__BANNER__", BANNER)
            .replace("__FONT_LINK__", FONT_LINK)
            .replace("__ICON__", ICON_LINK)
            .replace("__BASE_CSS__", BASE_CSS)
            .replace("__MAP_SVG__", MAP_SVG)
            .replace("__MAP_JS__", MAP_JS)
            .replace("__DATA__", json.dumps(data, separators=(",", ":"))))


def render_spectator(data, race_start, race_end):
    config = {
        "dbUrl": DB_URL.rstrip("/"),
        "publicPath": PUBLIC_PATH,
        "racePath": RACE_PATH,
        "driverPath": DRIVER_PATH,
        "viewersPath": VIEWERS_PATH,
        "viewersCountPath": VIEWERS_COUNT_PATH,
        "viewerBeatMs": VIEWER_BEAT_MS,
        "viewerPollMs": VIEWER_POLL_MS,
        "staleAfterS": STALE_AFTER_S,
        "oldAfterS": OLD_AFTER_S,
        "raceStart": race_start,
        "raceEnd": race_end,
        "lat": track.FINISH_LINE_LAT,
        "lon": track.FINISH_LINE_LON,
    }
    return (SPECTATOR_TEMPLATE
            .replace("__TIMING_URL__", TIMING_URL)
            .replace("__TIMING_CREDIT__", TIMING_CREDIT)
            .replace("__BANNER__", BANNER)
            .replace("__FONT_LINK__", FONT_LINK)
            .replace("__ICON__", ICON_LINK)
            .replace("__BASE_CSS__", BASE_CSS)
            .replace("__MAP_SVG__", MAP_SVG)
            .replace("__MAP_JS__", MAP_JS)
            .replace("__CONFIG__", json.dumps(config, separators=(",", ":")))
            .replace("__DATA__", json.dumps(data, separators=(",", ":"))))


def render_wall(data):
    """The pit-wall page. Same map, same sectors, same landmark list as the
    other two -- only the panel beside it differs."""
    config = {
        "pollMs": 1000,
        "staleAfterS": STALE_AFTER_S,
        "gpsMaxAgeS": GPS_LIVE_MAX_AGE_S,
        # When the lap clock starts saying it is counting on the pit's clock
        # alone. THE DASHBOARD'S OWN THRESHOLD (constants.DATA_STALE_AFTER_S,
        # shipped to its frontend as dataStaleAfterS), not this page's 20 s
        # dimming one: the two screens show the same clock and must say the
        # same thing about it at the same moment.
        "dataStaleAfterS": DATA_STALE_AFTER_S,
        # No "profiles" map any more. It existed solely to give the lap list a
        # target to subtract from, and the wall no longer scores laps against a
        # profile -- each lap is compared with the one before it instead.
        #
        # No "maxLapS" either. It blanked a running lap over 15 minutes as "not
        # a lap, a stopped feed" -- the clock now keeps counting through a dead
        # feed by design and says so in its label, so a long one is a long one.
    }
    return (WALL_TEMPLATE
            .replace("__BANNER__", BANNER)
            .replace("__FONT_LINK__", FONT_LINK)
            .replace("__ICON__", ICON_LINK)
            .replace("__BASE_CSS__", BASE_CSS)
            .replace("__MAP_SVG__", MAP_SVG)
            .replace("__MAP_JS__", MAP_JS)
            .replace("__CONFIG__", json.dumps(config, separators=(",", ":")))
            .replace("__DATA__", json.dumps(data, separators=(",", ":"))))


def _epoch(text):
    """ISO-8601 (with offset, e.g. 2026-09-19T12:00+02:00) -> epoch seconds."""
    if not text:
        return None
    dt = datetime.datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise SystemExit("--race-start/--race-end need a timezone offset, e.g. "
                         "2026-09-19T12:00+02:00 — without one the countdown is "
                         "wrong for everyone not in your timezone.")
    return dt.timestamp()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--verify", action="store_true",
                    help="write the pages and print a geometry report")
    ap.add_argument("--race-start", default=None,
                    help="ISO-8601 with offset, e.g. 2026-09-19T12:00+02:00. "
                         "Without it the spectator page hides its race clock "
                         "rather than counting down to a guess.")
    ap.add_argument("--race-end", default=None,
                    help="ISO-8601 with offset. Defaults to 24 h after the start.")
    args = ap.parse_args()

    start = _epoch(args.race_start)
    end = _epoch(args.race_end)
    if start and not end:
        end = start + 24 * 3600
    if start and end and end <= start:
        raise SystemExit("--race-end must be after --race-start.")

    data = build_data()
    os.makedirs(_SITE, exist_ok=True)

    demo = render_demo(data)
    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(demo)
    print(f"wrote {os.path.relpath(OUT_PATH, _REPO)}  ({len(demo) / 1024:.1f} KB)")

    spec = render_spectator(data, start, end)
    with open(SPECTATOR_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(spec)
    print(f"wrote {os.path.relpath(SPECTATOR_PATH, _REPO)}  "
          f"({len(spec) / 1024:.1f} KB)"
          + ("" if start else "   [race clock hidden — no --race-start given]"))

    wall = render_wall(data)
    with open(WALL_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(wall)
    print(f"wrote {os.path.relpath(WALL_PATH, _REPO)}  ({len(wall) / 1024:.1f} KB)"
          f"   [pit LAN only - serve it with tools/pit_wall.py]")

    if args.verify:
        print(f"  built            {datetime.datetime.now(datetime.timezone.utc)}")
        print(f"  centreline       {len(data['line'])} points, "
              f"OSM relation {OSM_RELATION_ID} @ {OSM_TIMESTAMP}")
        print(f"  baked centreline {BUILT_UTC}")
        print(f"  viewBox          {data['viewBox']} (user unit = 1 m)")
        print(f"  lap length       {data['trackLength']:.0f} m")
        print(f"  sectors          {len(data['sectors'])}: " +
              ", ".join(f"S{s['id']} {s['start']:.0f}-{s['end']:.0f}m"
                        for s in data["sectors"]))
        print(f"  landmarks        {len(data['landmarks'])}: " +
              ", ".join(f"{l['name']}@{l['dist']:.0f}m" for l in data["landmarks"]))
        targets = _all_profile_lap_seconds()
        print(f"  wall targets     " + ", ".join(
            f"{k} {v:.0f}s" for k, v in sorted(targets.items(), key=lambda kv: kv[1])))
        print(f"  demo profile     {len(data['profile'])} points, "
              f"modelled lap {data['profileLapSeconds']:.1f} s")
        print(f"  spectator feed   {DB_URL}/{PUBLIC_PATH}.json "
              f"(stale after {STALE_AFTER_S}s)")
        if start:
            print(f"  race window      "
                  f"{datetime.datetime.fromtimestamp(start, datetime.timezone.utc)} "
                  f"-> {datetime.datetime.fromtimestamp(end, datetime.timezone.utc)} UTC")
        # The centreline is an OPEN polyline closed by one final segment from
        # the last vertex back to the first, so the two ends being apart is
        # expected -- what must hold is that the gap matches the length CUM_M
        # budgets for that closing segment. If those disagree, every distance
        # on both pages is off by the difference.
        gap = math.hypot(data["line"][0][0] - data["line"][-1][0],
                         data["line"][0][1] - data["line"][-1][1])
        booked = track_map.CUM_M[-1] - track_map.CUM_M[-2]
        print(f"  closing segment  drawn {gap:.1f} m vs {booked:.1f} m in CUM_M "
              f"({'ok' if abs(gap - booked) < 1.0 else 'MISMATCH'})")


if __name__ == "__main__":
    main()
