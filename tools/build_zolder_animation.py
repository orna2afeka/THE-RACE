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
    profiles/base_210s.csv   the 210 s baseline lap the demo car actually drives

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
from strategy_engine import SECTIONS_INFO, TRACK_LANDMARKS    # noqa: E402
from constants import SECTION_NAMES                           # noqa: E402

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
PROFILE_PATH = os.path.join(_REPO, "profiles", "base_210s.csv")

BOUNDARIES = [SECTIONS_INFO[s]["range"][0] for s in sorted(SECTIONS_INFO)]
SECTOR_IDS = sorted(SECTIONS_INFO)

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
# The drawing is fitted to its own CONTENT, not to the tarmac: the callouts
# stick out much further than the track does, and a fixed margin big enough for
# the longest of them ("Chicane (Turns 5,6)", which runs 260 m wide at this
# scale) wastes that much space on all four sides. So the bounds below are
# measured from the labels themselves and this is only the breathing room added
# once they are all accounted for.
PAD_M = 40.0
# Rough advance width of one character as a fraction of font size, for working
# out how far a callout actually reaches. It only has to be close: a little
# generous costs a few metres of margin, a little tight clips a label.
CHAR_W = 0.56
TRACK_CASING_M = 17.0
TRACK_CORE_M = 11.0
GATE_HALF_M = 17.0
FINISH_HALF_M = 24.0
SECTOR_LABEL_OFFSET_M = 46.0
LANDMARK_LEADER_M = 78.0
CAR_RADIUS_M = 13.0
TRAIL_M = 170.0       # how much track the car's tail covers
# In metres, like everything else here, and known to PYTHON rather than only to
# the stylesheet because the viewBox is fitted around the text: the bounds
# maths cannot ask the browser how wide a word came out.
LANDMARK_FONT_M = 25.0
LANDMARK_SPEED_FONT_M = 21.0
SECTOR_FONT_M = 30.0
LABEL_PAD_M = 12.0


def _svg_xy(x, y, ox, oy):
    """One local-metre point in SVG user units (y flipped, origin at 0,0)."""
    return (round(x - ox, 1), round(oy - y, 1))


def _clearance(px, py):
    """How far a point is from the nearest bit of tarmac, in metres.

    Used to decide which side of the track a turn callout goes on. The obvious
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
    # Bounds start as the tarmac and grow to contain each callout, so the
    # finished viewBox is exactly the drawing and no more.
    x0, x1, y0, y1 = track_map.BOUNDS_XY
    lo_x, hi_x, lo_y, hi_y = x0, x1, y0, y1

    def grow(px, py):
        nonlocal lo_x, hi_x, lo_y, hi_y
        lo_x, hi_x = min(lo_x, px), max(hi_x, px)
        lo_y, hi_y = min(lo_y, py), max(hi_y, py)

    landmarks_local = []
    for lm in TRACK_LANDMARKS:
        dist = float(lm["dist_m"]) % track.TRACK_LENGTH_METERS
        if dist == 0.0:
            continue          # the finish line already has its own white gate
        px, py = track_map.position_at_distance(dist)
        tx, ty = track_map.tangent_at(dist)
        nx, ny = -ty, tx

        # Which side: whichever end has more room around it. Near-ties (a
        # straight, with equal space both ways) fall back to pointing away from
        # the middle of the circuit, which keeps the callouts fanned outwards.
        cands = []
        for sign in (1.0, -1.0):
            ex = px + LANDMARK_LEADER_M * sign * nx
            ey = py + LANDMARK_LEADER_M * sign * ny
            cands.append((_clearance(ex, ey), sign, ex, ey))
        cands.sort(reverse=True)
        if abs(cands[0][0] - cands[1][0]) < 15.0:
            outward = ((px - centre_local[0]) * nx + (py - centre_local[1]) * ny)
            sign = 1.0 if outward >= 0 else -1.0
            ex = px + LANDMARK_LEADER_M * sign * nx
            ey = py + LANDMARK_LEADER_M * sign * ny
        else:
            _, sign, ex, ey = cands[0]

        speed = lm.get("max_speed")
        text = str(lm["name"])
        # How far the text itself reaches past the end of its leader line, so
        # the bounds below account for the words and not just the line.
        speed_reach = (len("%s km/h" % speed) * LANDMARK_SPEED_FONT_M * CHAR_W
                       if speed is not None else 0.0)
        reach = max(len(text) * LANDMARK_FONT_M * CHAR_W, speed_reach)
        landmarks_local.append({
            "name": text, "speed": speed, "dist": dist,
            "px": px, "py": py, "ex": ex, "ey": ey,
            "right": ex > px + 1.0, "left": ex < px - 1.0,
        })
        grow(px, py)
        if ex > px + 1.0:
            grow(ex + reach + LABEL_PAD_M, ey)
        elif ex < px - 1.0:
            grow(ex - reach - LABEL_PAD_M, ey)
        else:
            grow(ex - reach / 2, ey)
            grow(ex + reach / 2, ey)
        # Two lines of text hang below or above the end of the leader.
        grow(ex, ey + LANDMARK_FONT_M * 2.2)
        grow(ex, ey - LANDMARK_FONT_M * 2.2)

    ticks = track_map.boundary_ticks(BOUNDARIES, GATE_HALF_M)
    slabels_local = []
    for (dist, _a, _b, normal), sector_id, side in zip(ticks, SECTOR_IDS,
                                                       track_map.LABEL_SIDE):
        px, py = track_map.position_at_distance(dist)
        lx = px + SECTOR_LABEL_OFFSET_M * side * normal[0]
        ly = py + SECTOR_LABEL_OFFSET_M * side * normal[1]
        slabels_local.append((sector_id, lx, ly))
        grow(lx, ly)

    lo_x -= PAD_M
    hi_x += PAD_M
    lo_y -= PAD_M
    hi_y += PAD_M

    # -- pass 2: into SVG user units (one unit = one metre, y flipped) ------- #
    ox, oy = lo_x, hi_y
    width, height = hi_x - lo_x, hi_y - lo_y

    line = [_svg_xy(x, y, ox, oy) for x, y in track_map.CENTRELINE_XY]
    cum = [round(c, 2) for c in track_map.CUM_M]

    sectors = []
    for sector_id, (seg_start, seg_end, xs, ys) in zip(
            SECTOR_IDS, track_map.split_at(BOUNDARIES)):
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

    landmarks = []
    for lm in landmarks_local:
        sx, sy = _svg_xy(lm["px"], lm["py"], ox, oy)
        exs, eys = _svg_xy(lm["ex"], lm["ey"], ox, oy)
        landmarks.append({
            "name": lm["name"], "speed": lm["speed"], "dist": lm["dist"],
            "x1": sx, "y1": sy, "x2": exs, "y2": eys,
            # Anchor away from the track: a callout on the left of the circuit
            # must run leftwards, or its text crosses back over the tarmac.
            "anchor": ("start" if lm["right"] else
                       "end" if lm["left"] else "middle"),
            "dy": 1 if eys > sy else -1,
        })
    landmarks.sort(key=lambda l: l["dist"])

    return {
        "viewBox": "0 0 %.0f %.0f" % (width, height),
        "trackLength": track.TRACK_LENGTH_METERS,
        "line": line,
        "cum": cum,
        "sectors": sectors,
        "gates": gates,
        "sectorLabels": labels,
        "finish": {"x1": fa_x, "y1": fa_y, "x2": fb_x, "y2": fb_y},
        "landmarks": landmarks,
        "profile": [[round(d, 1), round(v, 2)] for d, v in _read_profile()],
        "profileLapSeconds": round(_profile_lap_seconds(), 2),
        "carColor": CAR_COLOR,
        "attribution": OSM_ATTRIBUTION,
        "style": {
            "casing": TRACK_CASING_M, "core": TRACK_CORE_M,
            "car": CAR_RADIUS_M, "trail": TRAIL_M,
            "lmFont": LANDMARK_FONT_M, "lmSpeedFont": LANDMARK_SPEED_FONT_M,
            "sFont": SECTOR_FONT_M,
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
# projection, the sector splits, the callout placement — and having it exist
# twice is how the two would end up disagreeing about where Turn 12 is.
# --------------------------------------------------------------------------- #

# The database the car publishes to. Read from SolarRace_OS/main.py's own URL
# rather than retyped: this is the fourth place that string would otherwise
# live, and a spectator page pointed at the wrong database shows nothing with
# no error anyone would notice.
DB_URL = "https://solar-race-telemetry-default-rtdb.europe-west1.firebasedatabase.app"
PUBLIC_PATH = "public/live"
RACE_PATH = "public/race"

# How long after the car's last sample the page stops claiming to be live. The
# car publishes once a second; 20 s is twenty missed updates, which is a real
# outage and not a bad moment on a mobile network.
STALE_AFTER_S = 20

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
  .leader { stroke: #475569; stroke-width: 2.4; }
  .lm-dot { fill: var(--accent); }
  .lm-name { fill: #cbd5e1; font-weight: 600; letter-spacing: 1px; }
  .lm-speed { fill: #64748b; font-weight: 600; }
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
      <g id="g-landmarks"></g>
      <g id="g-slabels"></g>
      <path id="trail" fill="none" stroke-linecap="round"></path>
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

DATA.landmarks.forEach(lm => {
  const g = el("g-landmarks");
  g.appendChild(mk("line", {
    x1: lm.x1, y1: lm.y1, x2: lm.x2, y2: lm.y2, class: "leader",
  }));
  g.appendChild(mk("circle", { cx: lm.x1, cy: lm.y1, r: 5, class: "lm-dot" }));

  const pad = lm.anchor === "start" ? 12 : lm.anchor === "end" ? -12 : 0;
  const name = mk("text", {
    x: lm.x2 + pad, y: lm.y2 + (lm.dy > 0 ? 24 : -6),
    "text-anchor": lm.anchor, class: "lm-name",
    "font-size": DATA.style.lmFont,
  });
  name.textContent = lm.name;
  g.appendChild(name);

  if (lm.speed != null) {
    const sp = mk("text", {
      x: lm.x2 + pad, y: lm.y2 + (lm.dy > 0 ? 48 : 18),
      "text-anchor": lm.anchor, class: "lm-speed",
      "font-size": DATA.style.lmSpeedFont,
    });
    sp.textContent = lm.speed + " km/h";
    g.appendChild(sp);
  }
});

DATA.sectorLabels.forEach(s => {
  const t = mk("text", { x: s.x, y: s.y, class: "s-label", fill: s.color,
                         "font-size": DATA.style.sFont });
  t.textContent = s.text;
  el("g-slabels").appendChild(t);
});

const car = el("car");
car.setAttribute("r", DATA.style.car);
car.setAttribute("fill", DATA.carColor);
el("car-core").setAttribute("r", DATA.style.car * 0.36);
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

function sectorAt(d) {
  const L = DATA.trackLength;
  d = ((d % L) + L) % L;
  for (const s of DATA.sectors) if (d >= s.start && d < s.end) return s;
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

function fmtTime(s) {
  if (s == null) return "—";
  const m = Math.floor(s / 60);
  const r = s - m * 60;
  return m + ":" + (r < 10 ? "0" : "") + r.toFixed(1);
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
function paintMap(dist) {
  const [x, y] = posAt(dist);
  car.setAttribute("cx", x); car.setAttribute("cy", y);
  el("car-core").setAttribute("cx", x); el("car-core").setAttribute("cy", y);

  const s = sectorAt(dist);
  el("trail").setAttribute("stroke", s.color);
  el("trail").setAttribute("d", trailPath(dist));
  el("trail").setAttribute("opacity", "0.55");

  if (progress) {
    el("progress-mark").style.left = (dist / DATA.trackLength * 100) + "%";
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
  "<br>Demo lap driven by the team's 210 s baseline velocity profile &mdash; " +
  "modelled lap " + DATA.profileLapSeconds.toFixed(1) + " s.";

function speedAt(d) {
  const p = DATA.profile, L = DATA.trackLength;
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
<title>Afeka Solar Racing &mdash; Live from Circuit Zolder</title>
<meta name="description" content="Follow the Afeka Solar &amp; Electric Racing team live from the iESC 24-hour race at Circuit Zolder.">
__FONT_LINK__
<style>
__BASE_CSS__
  body { min-height: 100%; }
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
  #race-bar > i { background: var(--accent); }
  #sector-name { color: var(--accent); }
  .foot { font-size: 0.7rem; color: #64748b; line-height: 1.55; }
  #map { border: 1px solid var(--line); border-radius: 10px;
         background: rgba(6, 9, 15, 0.4); overflow: hidden; }
  /* The car stops being drawn as a live thing the moment the feed is not. */
  #car, #car-core, #trail { transition: opacity 0.4s ease; }
  body.stale #car, body.stale #car-core, body.stale #trail { opacity: 0.28; }
  #milestone {
    position: fixed; left: 50%; top: 22px; transform: translateX(-50%) translateY(-140%);
    background: linear-gradient(90deg, #0ea5e9, #22d3ee);
    color: #04121a; font-weight: 700; letter-spacing: 2px;
    text-transform: uppercase; padding: 12px 26px; border-radius: 999px;
    box-shadow: 0 14px 40px rgba(34, 211, 238, 0.35);
    transition: transform 0.5s cubic-bezier(.2,.9,.3,1.2); z-index: 50;
    font-size: 0.95rem; text-align: center;
  }
  #milestone.show { transform: translateX(-50%) translateY(0); }
  @media (max-width: 900px) {
    #stage { grid-template-columns: 1fr; height: auto; padding: 12px; }
    #map { height: 56vh; }
    #legend { display: none; }
    .hero .value { font-size: 2.6rem; }
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

    <div class="card">
      <div class="label">Where the car is</div>
      <div class="value small" id="sector-name">&mdash;</div>
      <div class="sub" id="next-sub">&mdash;</div>
      <div id="progress"><div id="progress-mark"></div></div>
    </div>

    <div class="card" id="race-card">
      <div class="label" id="race-label">Race clock</div>
      <div class="value small"><span id="race-elapsed">&mdash;</span><span
        class="sub" style="display:inline" id="race-of"> of <span
        id="race-total">24h</span></span></div>
      <div class="bar" id="race-bar"><i></i></div>
      <div class="sub" id="race-sub">&mdash;</div>
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
      <div class="label">Battery</div>
      <div class="value small"><span id="soc">&mdash;</span><span class="unit">%</span></div>
      <div class="bar" id="soc-bar"><i></i></div>
      <div class="sub">Solar input <span id="solar">&mdash;</span></div>
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
let snap = null;
let lastRxWall = 0;        // our clock, for "how long since anything arrived"
let dist = 0, target = null, everPainted = false;
let race = { start: CONFIG.raceStart, end: CONFIG.raceEnd };
let sun = null;

const num = (v) => (v == null || v === "" || isNaN(Number(v))) ? null : Number(v);

function fmtCountdown(s) {
  s = Math.max(0, Math.floor(s));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return d + "d " + h + "h";
  if (h > 0) return h + "h " + (m < 10 ? "0" : "") + m + "m";
  return m + "m " + (s % 60) + "s";
}

// Shown in the VIEWER's timezone, deliberately. Family watching from Israel
// should read the start time in their own evening, not in Belgian local time
// they then have to convert.
function whenLocal(ts) {
  try {
    return new Date(ts * 1000).toLocaleString(undefined,
      { weekday: "short", day: "numeric", month: "short",
        hour: "2-digit", minute: "2-digit" });
  } catch (e) { return "—"; }
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
  const d = num(obj.lap_distance_m);
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

// ── weather, straight from Open-Meteo, same source the pit uses ─────────── //
function loadWeather() {
  const u = "https://api.open-meteo.com/v1/forecast?latitude=" + CONFIG.lat +
            "&longitude=" + CONFIG.lon +
            "&current=temperature_2m,cloud_cover,wind_speed_10m" +
            "&daily=sunrise,sunset&timezone=Europe%2FBrussels&forecast_days=2";
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
    const rise = new Date(sun.rise[i]), set = new Date(sun.set[i]);
    if (now >= rise && now < set) {
      label = "☀ Daylight · sunset " + set.toTimeString().slice(0, 5);
      break;
    }
    if (now < rise) {
      label = "🌙 Dark · sunrise " + rise.toTimeString().slice(0, 5);
      break;
    }
  }
  el("daynight").textContent = label ||
    "🌙 Dark · sunrise " + new Date(sun.rise[sun.rise.length - 1])
      .toTimeString().slice(0, 5);
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

  const pill = el("status");
  pill.className = "pill " + (age == null ? "off" : stale ? "stale" : "live");
  el("status-text").textContent =
    age == null ? "Waiting for the car" : stale ? "No data" : "Live";
  el("status-sub").textContent =
    age == null ? "Nothing has arrived yet — the car may not be running."
                : stale ? "Nothing for " + Math.round(age) + "s. The car is out of "
                          + "contact; the marker below is where it was last seen."
                : "Updated " + Math.max(0, Math.round(age)) + "s ago";
  document.body.classList.toggle("stale", stale);

  const s = snap || {};
  el("lap").textContent = num(s.lap) == null ? "—" : num(s.lap);
  el("speed").textContent = num(s.speed_kmh) == null ? "—"
                            : Math.round(num(s.speed_kmh));
  el("lastlap").textContent = fmtTime(num(s.last_lap_time_s));
  const odo = num(s.odometer_m);
  el("odo").textContent = odo == null ? "—" : dash(odo / 1000, 1);

  const soc = num(s.soc_percent);
  el("soc").textContent = soc == null ? "—" : Math.round(soc);
  el("soc-bar").firstElementChild.style.width = (soc == null ? 0 : soc) + "%";
  const amps = num(s.solar_current_A);
  el("solar").textContent = amps == null ? "—" : dash(amps, 1, " A");

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
      el("race-sub").textContent = "Lights out " + whenLocal(race.start);
      bar.style.width = "0%";
    } else if (now >= race.end) {
      el("race-label").textContent = "Race complete";
      el("race-elapsed").textContent = fmtClock(total);
      el("race-of").style.display = "none";
      el("race-sub").textContent = "Finished " + whenLocal(race.end);
      bar.style.width = "100%";
    } else {
      const done = now - race.start;
      el("race-label").textContent = "Race clock";
      el("race-elapsed").textContent = fmtClock(done);
      el("race-of").style.display = "inline";
      el("race-sub").textContent = fmtClock(total - done) + " remaining";
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
  if (everPainted) {
    const s = paintMap(dist);
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
loadWeather();
setInterval(render, 1000);          // keeps the ages and the race clock moving
setInterval(loadWeather, 900000);
setInterval(renderDayNight, 60000);
setInterval(loadRace, 300000);
requestAnimationFrame(frame);
</script>
</body>
</html>
"""


def render_demo(data):
    return (DEMO_TEMPLATE
            .replace("__BANNER__", BANNER)
            .replace("__FONT_LINK__", FONT_LINK)
            .replace("__BASE_CSS__", BASE_CSS)
            .replace("__MAP_SVG__", MAP_SVG)
            .replace("__MAP_JS__", MAP_JS)
            .replace("__DATA__", json.dumps(data, separators=(",", ":"))))


def render_spectator(data, race_start, race_end):
    config = {
        "dbUrl": DB_URL.rstrip("/"),
        "publicPath": PUBLIC_PATH,
        "racePath": RACE_PATH,
        "staleAfterS": STALE_AFTER_S,
        "raceStart": race_start,
        "raceEnd": race_end,
        "lat": track.FINISH_LINE_LAT,
        "lon": track.FINISH_LINE_LON,
    }
    return (SPECTATOR_TEMPLATE
            .replace("__BANNER__", BANNER)
            .replace("__FONT_LINK__", FONT_LINK)
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
