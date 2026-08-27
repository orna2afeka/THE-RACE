"""
track_map_view.py — the F1-style vector circuit map
====================================================
Draws Circuit Zolder as a 2D plan view with the nine sectors colour-coded, and
puts the car on it. Rendered directly below the satellite map in the Driver
Telemetry tab; the satellite map is untouched and the two are complementary —
imagery for "which corner is that", this for "where are we in the lap".

    render_vector_track_map(ctx)      # ctx is pit_dashboard._live_context()

WHAT THE CAR MARKER MEANS, WHICH IS THE WHOLE POINT
Four states, resolved in a fixed order by resolve_car_marker():

    LIVE       a real GPS fix, recent, inside the Zolder geofence
    HELD       we HAVE seen a real Zolder fix this session, and have lost it
    SIMULATED  no Zolder fix has ever arrived, so the marker is placed from lap
               distance instead — modelled, not measured, and drawn to say so
    NO DATA    no fix and no distance: nothing to place, so nothing is drawn

The transition from SIMULATED to LIVE is automatic and has no date in it. It is
driven only by whether a real fix is inside the geofence, so the morning the car
first turns a wheel at Zolder the map switches by itself, and nobody has to
remember to turn a test mode off. A date-based switch would also have been wrong
for a shakedown trip, and would have lied if the travel plan moved.

THE GEOMETRY LIVES ELSEWHERE
track_map.py (repo root) owns the centreline and knows nothing about sectors or
Streamlit. This file owns the sector model, the colours and the Plotly figure.
"""

import time

import streamlit as st

from constants import SECTION_COLORS, SECTION_NAMES, SECTION_RISK, WARNING
from strategy_engine import SECTIONS_INFO
import track_map

try:
    import plotly.graph_objects as go
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

# Sector starts, in order, from the one place that defines them.
BOUNDARIES = [SECTIONS_INFO[s]["range"][0] for s in sorted(SECTIONS_INFO)]
SECTOR_IDS = sorted(SECTIONS_INFO)

# ── Marker states ─────────────────────────────────────────────────────────── #
MAP_LIVE = "live"
MAP_HELD = "held"
MAP_SIMULATED = "simulated"
MAP_NO_DATA = "no_data"

# ── Look ──────────────────────────────────────────────────────────────────── #
# The track line is the subject of this chart, not a plot of it, so it is much
# heavier than the History tab's 2.2 px traces — and wide enough that the car
# marker sitting on it is unambiguous.
TRACK_WIDTH_PX = 5
TICK_HALF_LEN_M = 14.0
LABEL_OFFSET_M = 26.0
CHART_HEIGHT = 420

# The car keeps the cyan it has on the satellite map above. It is the same car;
# a second colour for it on a second map is something the crew would have to
# learn for no reason.
CAR_COLOR = "#00FFCC"
CAR_OUTLINE = "#080c12"
HELD_COLOR = SECTION_COLORS[WARNING]
SIM_COLOR = "#9aa7b4"

_SESSION_LAST_FIX = "trackmap_last_fix"
_CHART_KEY = "trackmap_chart"


# --------------------------------------------------------------------------- #
# The state machine — pure, so it can be tested without a Streamlit runtime
# --------------------------------------------------------------------------- #
def resolve_car_marker(has_gps, lat, lon, fresh, lap_dist_m, has_distance,
                       last_fix, now):
    """Decide what to draw for the car. Returns (marker, new_last_fix).

    `marker` is a dict: mode, x, y (None when nothing should be drawn), and the
    facts a caption needs. `last_fix` is the caller's stored (x, y, ts) of the
    most recent real in-geofence fix, or None; the caller writes back whatever
    comes out.

    The order of these tests is load-bearing and each one is here for a reason.
    """
    at_zolder = bool(has_gps) and track_map.is_at_zolder(lat, lon)

    # 1. LIVE. has_gps is checked FIRST and unconditionally, because the pit
    #    dashboard substitutes the Zolder paddock (50.9895, 5.2568) when the car
    #    reports no position — and that placeholder is 172 m from the finish
    #    line, comfortably inside the geofence. Asking "is it at Zolder?" before
    #    "is it real?" would paint a confident car on the grid every time the
    #    GPS died, which is the worst answer this map could give.
    if at_zolder and fresh:
        x, y = track_map.to_local_xy(float(lat), float(lon))
        return ({"mode": MAP_LIVE, "x": x, "y": y, "lat": lat, "lon": lon,
                 "lap_dist_m": lap_dist_m, "away_m": None, "held_age_s": None},
                (x, y, now))

    # 2. HELD. This OUTRANKS the simulated marker, and that is the point: once
    #    the car is genuinely at Zolder, a four-second dropout must not teleport
    #    it to a modelled position and back. That flicker reads as a real event.
    #
    #    There is deliberately NO timeout here. "The last place we actually saw
    #    the car" beats a position derived from a drifting odometer for as long
    #    as it takes, and a held marker that silently turned fictional after
    #    sixty seconds would be a trap rather than a feature. The caption's
    #    growing age is the honest signal; let it grow.
    if last_fix:
        hx, hy, hts = last_fix
        return ({"mode": MAP_HELD, "x": hx, "y": hy, "lat": None, "lon": None,
                 "lap_dist_m": lap_dist_m, "away_m": None,
                 "held_age_s": max(0.0, now - hts)},
                last_fix)

    # 3. NO DATA. Checked before SIMULATED because _live_context fabricates
    #    current_lap_dist_m = 0.0 when the car has reported nothing at all, so a
    #    map that trusted it would park a marker permanently on the finish line
    #    — indistinguishable from a car sitting on the grid.
    if not has_distance:
        return ({"mode": MAP_NO_DATA, "x": None, "y": None, "lat": None,
                 "lon": None, "lap_dist_m": None, "away_m": None,
                 "held_age_s": None}, last_fix)

    # 4. SIMULATED. Real distance, no Zolder fix. Includes how far away the car
    #    actually is, because "simulated" on its own reads as "the GPS is
    #    broken" — and during Israel testing the GPS is perfect, it is simply
    #    three thousand kilometres from this map.
    away = track_map.distance_from_zolder_m(lat, lon) if has_gps else None
    x, y = track_map.position_at_distance(lap_dist_m)
    return ({"mode": MAP_SIMULATED, "x": x, "y": y, "lat": None, "lon": None,
             "lap_dist_m": lap_dist_m, "away_m": away, "held_age_s": None},
            last_fix)


# --------------------------------------------------------------------------- #
# Static geometry — computed once, not once per tick
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def _static_layers(dark):
    """Coordinate arrays and annotations for everything that never moves.

    Returns plain lists and dicts, NOT a go.Figure. st.cache_data hands every
    caller the same object, so a cached Figure would accumulate one car marker
    per rerun across every browser tab watching the dashboard.

    Coordinates are rounded to 0.1 m. The chart is about 1,200 m across in
    roughly 600 px, so 0.1 m is already twenty times finer than a pixel, and it
    roughly halves the figure JSON that crosses the websocket every two seconds.
    """
    fg = "#c9d3dd" if dark else "#1c2733"

    # Nine sector runs, grouped into three traces by risk tier and separated by
    # None. Colour is the only thing that varies between runs, so nine traces
    # would carry nine identical style dicts and nine hover registrations for no
    # visual difference at all.
    tiers = {}
    for sector_id, (_, _, xs, ys) in zip(SECTOR_IDS, track_map.split_at(BOUNDARIES)):
        tier = SECTION_RISK[sector_id]
        tx, ty = tiers.setdefault(tier, ([], []))
        if tx:
            tx.append(None)
            ty.append(None)
        tx.extend(round(v, 1) for v in xs)
        ty.extend(round(v, 1) for v in ys)

    ticks = track_map.boundary_ticks(BOUNDARIES, TICK_HALF_LEN_M)

    # The finish line gets its own trace: it is the one boundary that is also a
    # timing point, and white makes it findable at a glance.
    # Boundary gates, all in one None-separated trace. The 0 m tick is excluded
    # here because it gets its own white trace below — it is the finish line.
    finish = ticks[0]
    gate_x, gate_y = [], []
    for _, a, b, _ in ticks[1:]:
        gate_x.extend([round(a[0], 1), round(b[0], 1), None])
        gate_y.extend([round(a[1], 1), round(b[1], 1), None])

    # FULLY-FORMED annotation dicts, not raw positions, because _figure passes
    # them straight into the layout instead of calling add_annotation nine
    # times. Measured: nine add_annotation calls cost 21.2 ms, the identical
    # annotations handed to the layout in one go cost 2.7 ms. add_annotation
    # revalidates the whole layout each time, and this figure is rebuilt every
    # two seconds on the same thread that redraws the rest of the wall.
    notes = []
    for (dist, _, _, normal), sector_id, side in zip(ticks, SECTOR_IDS,
                                                     track_map.LABEL_SIDE):
        px, py = track_map.position_at_distance(dist)
        notes.append(dict(
            x=round(px + LABEL_OFFSET_M * side * normal[0], 1),
            y=round(py + LABEL_OFFSET_M * side * normal[1], 1),
            # The label at a boundary names the sector STARTING there: 0 m is
            # S1, 600 m is S2, and so on round to 3430 m being S9. It reads
            # correctly and it is easy to write wrong.
            text=f"S{sector_id}", showarrow=False,
            font=dict(size=11, color=fg),
            # The plate is what makes an 11 px label legible where it crosses a
            # bright track line. Without it the labels wash out in both themes.
            bgcolor="rgba(14,17,23,0.72)" if dark else "rgba(255,255,255,0.80)",
            borderpad=2, borderwidth=0))

    return {
        "tiers": {t: (xs, ys) for t, (xs, ys) in tiers.items()},
        "gates": (gate_x, gate_y),
        "finish": ([round(finish[1][0], 1), round(finish[2][0], 1)],
                   [round(finish[1][1], 1), round(finish[2][1], 1)]),
        "labels": notes,
        "fg": fg,
    }


def _figure(marker, dark):
    """Build the whole figure. Cheap: the static half is cached above.

    ONE go.Figure(...) call, not a Figure plus a run of add_trace /
    add_annotation calls. Both of those revalidate on every call, and this
    figure is rebuilt every two seconds on the thread that also redraws the
    live tiles: assembled incrementally it measured 76 ms, assembled in one
    constructor it is a fraction of that.
    """
    layers = _static_layers(dark)
    fg = layers["fg"]

    traces = [go.Scatter(x=xs, y=ys, mode="lines", hoverinfo="skip",
                         showlegend=False,
                         connectgaps=False,   # the Nones between runs matter
                         line=dict(color=SECTION_COLORS[tier],
                                   width=TRACK_WIDTH_PX))
              for tier, (xs, ys) in layers["tiers"].items()]

    gx, gy = layers["gates"]
    traces.append(go.Scatter(x=gx, y=gy, mode="lines", hoverinfo="skip",
                             showlegend=False, connectgaps=False,
                             line=dict(color=fg, width=1.6)))

    fx, fy = layers["finish"]
    traces.append(go.Scatter(x=fx, y=fy, mode="lines", hoverinfo="skip",
                             showlegend=False,
                             line=dict(color="#ffffff", width=3)))

    if marker["x"] is not None:
        traces.append(go.Scatter(
            x=[round(marker["x"], 1)], y=[round(marker["y"], 1)],
            mode="markers", hoverinfo="skip", showlegend=False,
            marker=_marker_style(marker["mode"])))

    annotations = list(layers["labels"])
    if marker["mode"] == MAP_SIMULATED:
        # IN the figure, not just in the caption below it. The caption is cropped
        # out of every screenshot, and a screenshot of a modelled car position
        # that is not labelled in the image is how a debrief ends up arguing
        # about a lap that never happened.
        annotations.append(dict(
            xref="paper", yref="paper", x=0.5, y=0.99, xanchor="center",
            text="SIMULATED — NOT GPS", showarrow=False, borderpad=4,
            font=dict(size=12, color="#0e1117"), bgcolor=HELD_COLOR))

    x0, x1, y0, y1 = track_map.BOUNDS_XY
    layout = dict(
        height=CHART_HEIGHT,
        margin=dict(l=0, r=0, t=8, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        dragmode=False,
        annotations=annotations,
        # Fixed, baked ranges. Two reasons, and the second is the important one:
        # Plotly never has to run an autorange pass, and the axes come out
        # BYTE-IDENTICAL on every redraw. Streamlit hashes the figure JSON into
        # the chart's element id and the browser uses that as its React key, so
        # a chart whose data changes is always torn down and rebuilt — key= and
        # uirevision cannot prevent it (see the History tab's note). What they
        # can do is make the rebuild invisible: same frame, same track, same
        # labels, one dot two metres further along.
        xaxis=dict(range=[x0, x1], visible=False, fixedrange=True),
        yaxis=dict(range=[y0, y1], visible=False, fixedrange=True,
                   # 1 m of x must be 1 m of y in pixels whatever the container
                   # width, or Zolder comes out squashed and unrecognisable.
                   scaleanchor="x", scaleratio=1),
    )
    return go.Figure(data=traces, layout=layout)


def _marker_style(mode):
    if mode == MAP_LIVE:
        return dict(size=14, color=CAR_COLOR, symbol="circle",
                    line=dict(color=CAR_OUTLINE, width=2))
    if mode == MAP_HELD:
        # Same SHAPE as live, so it still reads as a position; different colour,
        # to say it is no longer being measured.
        return dict(size=14, color=HELD_COLOR, symbol="circle",
                    line=dict(color=CAR_OUTLINE, width=2))
    # A hollow ring is the strongest "not a measurement" signal in Plotly's
    # marker vocabulary, and it survives being eight pixels on a laptop.
    return dict(size=15, symbol="circle-open", color=SIM_COLOR,
                line=dict(color=SIM_COLOR, width=3))


# --------------------------------------------------------------------------- #
# The Streamlit component
# --------------------------------------------------------------------------- #
def render_vector_track_map(ctx):
    """Draw the circuit map for one live context. Renders in place — no
    st.empty(), per the fragment rule in pit_dashboard."""
    if not HAS_PLOTLY:
        st.info("The circuit map needs Plotly — run "
                "`pip install -r requirements_pit.txt`.",
                icon=":material/info:")
        # Deliberately no st.line_chart fallback: without an aspect lock it
        # draws a squashed blob that is not recognisably Zolder, which is worse
        # than saying plainly that the chart is unavailable.
        return

    state = ctx["state"]
    # Straight from the stored row, NOT from ctx["current_lap_dist_m"], which
    # _live_context fabricates as 0.0 so the sector strip has something to draw.
    has_distance = (state.get("lap_distance_m") is not None
                    or ctx.get("odometer_km") is not None)

    marker, last_fix = resolve_car_marker(
        has_gps=state.get("has_gps"),
        lat=state.get("lat"), lon=state.get("lon"),
        fresh=ctx.get("fresh"),
        lap_dist_m=ctx.get("current_lap_dist_m") or 0.0,
        has_distance=has_distance,
        last_fix=st.session_state.get(_SESSION_LAST_FIX),
        now=time.time(),
    )
    # Session-only, never persisted: a dashboard restarted the morning after the
    # race should work out where the car is from what the car is saying now, not
    # from a stale "we were at Zolder yesterday" flag.
    if last_fix is not None:
        st.session_state[_SESSION_LAST_FIX] = last_fix

    dark = st.session_state.get("theme_mode") != "Light"
    st.plotly_chart(
        _figure(marker, dark), key=_CHART_KEY, theme=None, width="stretch",
        # staticPlot skips Plotly's entire event, hover and modebar setup on
        # every mount — the single biggest saving available here. It is also the
        # honest choice: a chart that rebuilds every two seconds cannot offer a
        # zoom that survives, and this codebase's rule is to remove an
        # interaction rather than offer it broken.
        config={"staticPlot": True, "displaylogo": False})

    st.caption(_caption(marker, ctx))
    st.caption(track_map.OSM_ATTRIBUTION)


def _caption(marker, ctx):
    """One honest line about what that marker is."""
    mode = marker["mode"]
    if mode == MAP_LIVE:
        sector = SECTION_NAMES.get(ctx.get("current_sector_id"), "")
        return (f":green[● LIVE GPS] — {sector} · "
                f"{marker['lap_dist_m']:,.0f} m of "
                f"{track_map.TRACK_LENGTH_METERS:,.0f} m")
    if mode == MAP_HELD:
        return (f":orange[◐ SIGNAL LOST] — holding the last Zolder fix, "
                f"{marker['held_age_s']:.0f}s old. Check gpsd on the Pi "
                f"(`gpspipe -w`).")
    if mode == MAP_SIMULATED:
        where = ("the car has not reported a GPS position"
                 if marker["away_m"] is None
                 else f"the car's GPS fix is {marker['away_m'] / 1000:,.0f} km "
                      f"from Zolder")
        return (f":orange[○ SIMULATED — NOT GPS] — placed from lap distance "
                f"({marker['lap_dist_m']:,.0f} m); {where}.")
    return (":red[○ No position] — no GPS fix and no lap distance from the car, "
            "so there is nothing to place.")
