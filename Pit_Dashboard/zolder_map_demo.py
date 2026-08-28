"""
zolder_map_demo.py — live animated preview of the Circuit Zolder vector map
============================================================================
A throwaway demo, NOT part of the pit dashboard. Drives the exact rendering
function the real dashboard calls — track_map_view.render_vector_track_map —
with a scripted, continuously-moving car position, so the whole state machine
(SIMULATED -> LIVE -> SIGNAL LOST -> LIVE again) can be watched end to end
without waiting for the car to actually be at Zolder.

    streamlit run zolder_map_demo.py --server.port 8502

Runs on its own port, its own process, its own session state. It never opens
telemetry.db and never touches the real pit_dashboard.py process — nothing
here can affect the live pit wall.

WHY A SEPARATE APP AND NOT FAKE ROWS IN telemetry.db
telemetry.db is the pit's actual store, already holding this car's real test
history. Writing synthetic Zolder rows into it — even under a different
device_id — would need those rows found and deleted afterward, which is
exactly the kind of thing that gets missed under time pressure and quietly
contaminates a race database. Calling the production render function directly,
with a made-up ctx dict standing in for _live_context()'s output, shows the
identical chart with none of that risk and nothing to clean up.
"""

import math
import os
import sys
import time

import streamlit as st

# track.py / track_map.py live at the repo root, one level above Pit_Dashboard/.
# pit_dashboard.py gets this for free via constants.py's own bootstrap (which
# this standalone script never imports), so it needs the identical bootstrap
# constants.py uses, or `import track` fails the moment this runs on its own.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import track  # noqa: E402  (path set up immediately above)
import track_map  # noqa: E402
from strategy_engine import SECTIONS_INFO  # noqa: E402
from track_map_view import HAS_PLOTLY, render_vector_track_map  # noqa: E402
from track_map_view import _SESSION_LAST_FIX  # noqa: E402  (see _reset_last_fix)

st.set_page_config(page_title="Zolder Map — DEMO", layout="wide")

if not HAS_PLOTLY:
    st.error("This demo needs Plotly — run `pip install -r requirements_pit.txt`.")
    st.stop()

st.warning(
    "**DEMO — synthetic position, not real telemetry.** This calls the exact "
    "same `render_vector_track_map()` the pit dashboard uses, fed a scripted "
    "car position instead of a real GPS fix, so the SIMULATED / LIVE / SIGNAL "
    "LOST state machine can be watched without waiting for the car to be at "
    "Zolder. Nothing here touches telemetry.db or the real dashboard.",
    icon=":material/science:",
)

# --------------------------------------------------------------------------- #
# The exact inverse of track.to_local_xy (lat/lon -> local metres), so a
# position along the baked centreline can be handed to the state machine as a
# plausible "GPS fix" during the LIVE stage below.
#
# _EARTH_RADIUS_M is read off track.py rather than retyped, so this can never
# quietly drift from the value the real projection uses.
# --------------------------------------------------------------------------- #
_EARTH_RADIUS_M = track._EARTH_RADIUS_M


def _xy_to_latlon(x, y):
    lat0 = math.radians(track.FINISH_LINE_LAT)
    lat = track.FINISH_LINE_LAT + math.degrees(y / _EARTH_RADIUS_M)
    lon = track.FINISH_LINE_LON + math.degrees(x / (_EARTH_RADIUS_M * math.cos(lat0)))
    return lat, lon


def _sector_at(distance_m):
    d = distance_m % track.TRACK_LENGTH_METERS
    for sid, info in SECTIONS_INFO.items():
        lo, hi = info["range"]
        if lo <= d < hi:
            return sid
    return 1


# --------------------------------------------------------------------------- #
# The scripted drive. Distance advances continuously and smoothly for the
# whole demo; what changes underneath it is whether a GPS fix is available at
# all, and whether it is near Zolder — exactly the two facts the real state
# machine gates on.
# --------------------------------------------------------------------------- #
LAP_SECONDS = 30.0      # fast-forwarded: one lap every 30s of wall-clock, for a watchable demo
CYCLE_SECONDS = 90.0    # the whole SIMULATED->LIVE->LOST->LIVE tour repeats every 90s

# (start_s, end_s, label) within one CYCLE_SECONDS tour.
STAGES = [
    (0.0, 28.0, "SIMULATED — testing far from Zolder"),
    (28.0, 60.0, "LIVE — arrived at Zolder"),
    (60.0, 68.0, "SIGNAL LOST — GPS dropped at Zolder"),
    (68.0, 90.0, "LIVE — signal reacquired"),
]


def _stage_at(t):
    for lo, hi, label in STAGES:
        if lo <= t < hi:
            return lo, hi, label
    return STAGES[0]  # t == CYCLE_SECONDS exactly; falls back to the first stage


def _reset_last_fix():
    """Forget any stored Zolder fix, so a SIMULATED stage is genuinely simulated.

    resolve_car_marker deliberately makes SIGNAL LOST outrank SIMULATED once a
    real Zolder fix has been seen this session — that ordering is the whole
    point in production (see track_map_view.py). Left alone here, it means this
    demo's hollow SIMULATED ring would only ever appear once, in the first few
    seconds of the very first cycle; every lap after that the "SIMULATED"
    window would render as SIGNAL LOST instead, because by then a real fix
    always exists from the previous lap's LIVE stage.

    That is correct behaviour, not a bug — but a demo whose job is to show the
    ring off should actually show it every cycle. So this demo resets the
    stored fix at each cycle boundary (auto mode) and whenever SIMULATED is
    picked directly (manual mode), which the real dashboard has no reason to
    ever do to a live session.
    """
    st.session_state.pop(_SESSION_LAST_FIX, None)


if "demo_started_at" not in st.session_state:
    st.session_state.demo_started_at = time.time()

with st.sidebar:
    st.markdown("### Demo controls")
    st.toggle("Auto-cycle through every state", value=True, key="demo_auto")
    if st.button(":material/restart_alt: Restart the cycle"):
        st.session_state.demo_started_at = time.time()
    if not st.session_state.demo_auto:
        st.selectbox("Force a state", ["SIMULATED", "LIVE", "SIGNAL LOST"],
                     key="demo_forced")
        st.slider("Distance along lap (m)", 0.0, track.TRACK_LENGTH_METERS,
                  0.0, 10.0, key="demo_manual_dist")


@st.fragment(run_every=0.4)
def _live_preview():
    """Renders directly into its own slot — no st.empty(), same rule the real
    dashboard's fragments follow (see pit_dashboard.py's FRAGMENTS note) — so
    the sidebar controls above are never touched by this fragment's reruns."""
    elapsed = time.time() - st.session_state.demo_started_at
    distance = (elapsed / LAP_SECONDS) * track.TRACK_LENGTH_METERS

    if st.session_state.demo_auto:
        t = elapsed % CYCLE_SECONDS
        lo, hi, stage = _stage_at(t)
        countdown = hi - t
        # Crossed back to the first stage -> a new lap of the demo cycle
        # started. See _reset_last_fix for why this has to happen here.
        stage_idx = STAGES.index((lo, hi, stage))
        if stage_idx == 0 and st.session_state.get("_demo_prev_stage") not in (0, None):
            _reset_last_fix()
        st.session_state["_demo_prev_stage"] = stage_idx
    else:
        stage = {
            "SIMULATED": "SIMULATED — testing far from Zolder",
            "LIVE": "LIVE — arrived at Zolder",
            "SIGNAL LOST": "SIGNAL LOST — GPS dropped at Zolder",
        }[st.session_state.demo_forced]
        distance = st.session_state.demo_manual_dist
        countdown = None
        if st.session_state.demo_forced == "SIMULATED":
            _reset_last_fix()

    is_live = stage.startswith("LIVE")
    is_lost = stage.startswith("SIGNAL LOST")

    if is_live:
        x, y = track_map.position_at_distance(distance)
        lat, lon = _xy_to_latlon(x, y)
        has_gps, fresh = True, True
    elif is_lost:
        # No new fix at all. render_vector_track_map's own resolve_car_marker
        # falls back to whatever it stored as the last real Zolder position
        # during the LIVE stage just before this one — that fallback IS the
        # SIGNAL LOST behaviour being demonstrated, not something staged here.
        lat, lon, has_gps, fresh = None, None, False, False
    else:  # SIMULATED
        # A real fix, just nowhere near Zolder — the same shape as the car's
        # actual Israel test data (has_gps=True, coordinates far from Belgium).
        lat, lon, has_gps, fresh = 31.2694, 34.7275, True, True

    ctx = {
        "state": {"has_gps": has_gps, "lat": lat, "lon": lon,
                  "lap_distance_m": distance},
        "fresh": fresh,
        "current_lap_dist_m": distance,
        "current_sector_id": _sector_at(distance),
        "odometer_km": None,
    }

    note = f"  ·  next change in {countdown:0.0f}s" if countdown else ""
    st.markdown(f"**Demo stage:** {stage}{note}")
    render_vector_track_map(ctx)


st.markdown("### :material/route: Circuit Map (live preview)")
_live_preview()
