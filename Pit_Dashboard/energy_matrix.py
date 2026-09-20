"""
energy_matrix.py — what each speed profile costs, from runs the car really did
===============================================================================
A SEPARATE app from the pit wall, on its own port, exactly like the Speed
Profile Builder and for the same reason: it opens telemetry.db READ-ONLY and
runs in its own process, so nothing it does can slow, lock or crash the
dashboard the engineers are working from.

    streamlit run Pit_Dashboard/energy_matrix.py --server.port 8504

(or double-click "Start Energy Matrix.bat" at the repo root)

WHAT IT IS FOR
constants.PROFILE_MATRIX holds one number per profile that nothing can derive
from a speed curve: what a lap COSTS. The five in there today are a placeholder
ladder — 80 Wh plus or minus 5 % and 10 % — and the whole Strategy tab plans
stops with them. This turns runs the car actually did into all five.

IT NEVER WRITES ANYTHING. Not constants.py, not the profiles, not the store. It
prints a matrix and shows its working; putting the numbers in is a separate,
deliberate act — by hand, or through the Profile Builder's Save button, which
validates and backs up. A tool that silently re-costs the race is not a tool
anyone should have open during practice.

The arithmetic lives in energy_model.py, which has no Streamlit in it:
`python Pit_Dashboard/energy_model.py --self-check`.
"""

import os
import sys

import streamlit as st

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
for _p in (_REPO_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import energy_model as em                                   # noqa: E402
try:
    from strategy_engine import MAX_STOPS as _MAX_STOPS
except Exception:                                           # noqa: BLE001
    _MAX_STOPS = 3
import profile_manage as pm                                 # noqa: E402

try:
    import plotly.graph_objects as go
    HAS_PLOTLY = True
except Exception:                                           # noqa: BLE001
    HAS_PLOTLY = False

# The Profile Builder's palette, deliberately. Two pit tools that both plot
# "measured" and "derived" must not paint them different colours.
C_MEASURED = "#FF9900"
C_MODEL = "#00FFCC"
C_INSTALLED = "#94a3b8"
C_BAD = "#f87171"

st.set_page_config(page_title="Energy Matrix", page_icon="⚡", layout="wide")


@st.cache_data(ttl=30, show_spinner=False)
def _profiles():
    """Every profile the car would find, with its lap time and drag integral.

    Cached briefly, not forever: the Profile Builder rewrites these CSVs while
    this app may be open, and 30 s is short enough that a rebuilt profile is
    costed correctly on the next interaction.
    """
    return em.load_profiles()


profiles = _profiles()

st.title("⚡ Energy Matrix")
st.caption("What each speed profile costs per lap, worked out from runs the "
           "car actually did. Nothing here is written anywhere.")

if not profiles:
    st.error("No profiles in profiles/ — nothing to cost.")
    st.stop()

lo_pace, hi_pace = em.profile_pace_range(profiles)
law = em.aero_law(profiles)

source = st.radio(
    "Where is the measurement from?",
    ["Laps the car drove (telemetry.db)", "A run I measured myself"],
    horizontal=True,
    help="The store is better when the laps are good: every lap brings its own "
         "time and distance, so nothing has to be assumed, and laps at "
         "different paces fit both coefficients on their own.")

measurements, fatal = [], None

# --------------------------------------------------------------------------- #
# From the store
# --------------------------------------------------------------------------- #
if source.startswith("Laps"):
    from contextlib import closing
    import sqlite3
    from pit_config import SQLITE_PATH

    db_path = st.text_input("telemetry.db", value=SQLITE_PATH)
    recent = st.slider("Use the most recent N laps", 5, 300, 60, step=5,
                       help="A fit over the whole race would put this "
                            "morning's conditions into tonight's answer.")
    found = []
    try:
        with closing(sqlite3.connect("file:%s?mode=ro" % db_path,
                                     uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            found = em.measurements_from_db(conn, profiles, recent_laps=recent)
    except Exception as exc:                                # noqa: BLE001
        fatal = "Cannot read %s — %s" % (db_path, exc)

    if not fatal and not found:
        fatal = ("No laps in the store carry both an energy figure and a lap "
                 "time yet. Drive some, or switch to \"A run I measured "
                 "myself\".")

    if found:
        # EVERY LAP IS SHOWN AND EVERY LAP CAN BE DROPPED. The fit is only as
        # good as the laps in it, and the store is full of laps that are not
        # racing: the out-lap, the lap the car sat in the pits for, the bench
        # session. Deciding which laps count is the engineer's job, so the
        # table hands them the two facts that decide it — the real pace, and
        # whether the GPS line actually cut the lap.
        st.markdown("**Which laps count?** Untick anything that was not real "
                    "running — an out-lap, a lap spent stopped, a bench session.")
        rows_in = []
        for m in found:
            pace = m.implied_lap_time_s
            outside = pace and not (lo_pace * 0.95 <= pace <= hi_pace * 1.05)
            rows_in.append({
                "use": not outside,
                "lap": m.note.split(",")[0].replace("lap ", ""),
                "Wh": round(m.energy_wh, 1),
                "m": round(m.distance_m),
                "s": round(m.seconds or 0, 1),
                "pace s/lap": round(pace, 1) if pace else None,
                "Wh/lap": round(m.energy_per_lap_wh, 1),
                "cut by": m.lap_source or "?",
                "labelled": m.profile_key or "—",
            })
        edited = st.data_editor(
            rows_in, width="stretch", hide_index=True,
            disabled=[c for c in rows_in[0] if c != "use"],
            column_config={"use": st.column_config.CheckboxColumn(
                "use", help="Unticked by default when the lap's pace is "
                            "outside the range the profiles cover.")})
        measurements = [m for m, r in zip(found, edited) if r["use"]]
        if not measurements:
            fatal = ("Every lap is unticked. Tick at least one — or, if none of "
                     "them is real running, use \"A run I measured myself\".")

# --------------------------------------------------------------------------- #
# Typed in by hand
# --------------------------------------------------------------------------- #
else:
    # The distance field is the one that matters and the layout says so: energy
    # and duration alone fix the average POWER and nothing else. 117 Wh in five
    # minutes is 81.9 Wh/lap over 1.43 laps and 117 Wh/lap over one, and the
    # matrix is made of Wh per LAP.
    c1, c2 = st.columns(2)
    wh = c1.number_input("Energy used (Wh)", min_value=0.0, value=117.0,
                         step=1.0, format="%.1f")
    minutes = c2.number_input("Over how long (minutes)", min_value=0.0,
                              value=5.0, step=0.5, format="%.2f")

    st.markdown("**How far did the car go?** — this is what turns Wh into "
                "Wh *per lap*. The speeds are not needed; the distance is.")
    how = st.radio("How far did the car go?",
                   ["I know the distance", "I know the lap count",
                    "I don't know — assume it ran at one profile's pace"],
                   label_visibility="collapsed")

    keys = list(profiles)
    default_key = "lap93_293s" if "lap93_293s" in profiles else keys[0]
    dist_m = laps = None
    if how.startswith("I know the distance"):
        # Defaults to the distance base pace would have covered, so the
        # box opens on the most likely answer rather than on one lap —
        # 117 Wh over 5 minutes is a 300 s lap if you take the 4000 m
        # default literally, and that is a very different matrix.
        guess = (minutes * 60.0 * em.LAP_M
                 / profiles[default_key]["lap_time_s"]) if minutes else em.LAP_M
        dist_m = st.number_input("Distance (m)", min_value=1.0,
                                 value=float(round(guess)) or em.LAP_M,
                                 step=10.0, format="%.0f")
    elif how.startswith("I know the lap"):
        laps = st.number_input("Laps covered", min_value=0.01, value=1.0,
                               step=0.1, format="%.2f")

    # THE CONSEQUENCE, SPELLED OUT WHILE IT CAN STILL BE CHANGED. Energy and
    # duration fix the average power; only the distance turns it into Wh per
    # lap, and the implied pace is the one number that tells the engineer at
    # a glance whether what they typed describes the run they remember.
    if minutes and (dist_m or laps):
        d = dist_m if dist_m else laps * em.LAP_M
        pace = minutes * 60.0 * em.LAP_M / d
        inside = lo_pace * 0.95 <= pace <= hi_pace * 1.05
        st.caption(("That is **%.0f W** average and **%.1f Wh per 4000 m lap**, at **%.0f s/lap**. " % (wh / minutes * 60.0 if minutes else 0,
            wh * em.LAP_M / d, pace))
            + ("Inside the %.0f-%.0f s the profiles cover."
               % (lo_pace, hi_pace) if inside else
               "**Outside the %.0f-%.0f s the profiles cover** - the matrix below is extrapolated from it." % (lo_pace, hi_pace)))

    which = st.selectbox(
        "Which profile was it meant to be?", keys,
        index=keys.index(default_key),
        format_func=lambda k: "%s — %.1f s" % (k, profiles[k]["lap_time_s"]),
        help="A label for the report. The run is costed at the pace your "
             "numbers imply, not at this profile's pace — unless you pick the "
             "third option below, which has nothing else to go on.")

    if how.startswith("I don't know"):
        st.warning("Then the pace is an **assumption**, not a measurement — the "
                   "run is taken to have been at %s's own pace (%.1f s/lap). If "
                   "it was actually slower, every number below is wrong."
                   % (which, profiles[which]["lap_time_s"]))

    try:
        measurements = [em.measurement_from_duration(
            wh, minutes * 60.0 if minutes else None, dist_m, laps, which,
            profiles)]
    except Exception as exc:                                # noqa: BLE001
        fatal = str(exc)

if fatal:
    st.error(fatal)
    st.stop()

# --------------------------------------------------------------------------- #
# The one assumption, and when it stops mattering
# --------------------------------------------------------------------------- #
spread = (max(m.aero for m in measurements)
          / min(m.aero for m in measurements)) if measurements else 1.0
fitted = spread >= 1.02

if fitted:
    paces = sorted(m.implied_lap_time_s for m in measurements
                   if m.implied_lap_time_s)
    st.success("**%d runs spanning %.0f–%.0f s/lap — nothing is assumed.** Both "
               "the per-metre and the drag term are fitted from the data."
               % (len(measurements), paces[0], paces[-1]) if paces else
               "**Runs at different paces — nothing is assumed.**")
    share = em.DEFAULT_AERO_SHARE
else:
    st.info("Everything here is at effectively one pace, so there is one "
            "equation and two unknowns. The split between distance cost and "
            "drag cost has to be supplied. **Measure a run at a clearly "
            "different pace and this control disappears.**")
    share = st.slider(
        "Assumed aero share of the anchor lap", 0.05, 0.95,
        float(em.DEFAULT_AERO_SHARE), step=0.01,
        help="What fraction of that lap's energy is aerodynamic drag. The "
             "default of 1/3 reproduces the team's existing fast-side numbers "
             "almost exactly, which is why it was chosen.")

try:
    model = em.fit_from_measurements(measurements, profiles, share)
except Exception as exc:                                    # noqa: BLE001
    st.error(str(exc))
    st.stop()


# The race simulation costs ~140 ms and Streamlit reruns this script on every
# widget touch, so it is cached on the only things it depends on: the two fitted
# coefficients and the profiles themselves. Not on the widgets — moving the aero
# slider changes the coefficients, so the cache key moves with it.
@st.cache_data(ttl=600, show_spinner="Simulating the race...")
def _matrix(a, b, spec):
    profs = {k: {"label": lbl, "lap_time_s": t, "aero": ar}
             for k, lbl, t, ar in spec}
    return em.matrix(em.EnergyModel(a, b, ""), profs)


rows = _matrix(model.a, model.b,
               tuple((k, p["label"], p["lap_time_s"], p["aero"])
                     for k, p in profiles.items()))
current = em.current_matrix()
bad = em.problems(model, rows)
mislabelled = em.label_warnings(measurements, profiles)
extrapolated = em.extrapolation_warnings(measurements, profiles)

# --------------------------------------------------------------------------- #
# The answer, with everything wrong with it stated first
# --------------------------------------------------------------------------- #
if bad:
    st.error("**Do not use this matrix.**\n\n"
             + "\n".join("- %s" % b for b in bad))
if extrapolated:
    st.warning("**%d run%s outside the %.0f–%.0f s/lap the profiles cover.** "
               "Their drag is extrapolated, and the further out they are the "
               "less the matrix below means. A matrix built only from laps "
               "much slower than racing is a bench measurement, not a race "
               "plan."
               % (len(extrapolated), "" if len(extrapolated) == 1 else "s",
                  lo_pace, hi_pace))
    with st.expander("Which ones"):
        for w in extrapolated:
            st.write("- " + w)
if mislabelled:
    with st.expander("%d run%s filed under a profile it was not driven at"
                     % (len(mislabelled),
                        "" if len(mislabelled) == 1 else "s")):
        st.caption("Costed at its real pace either way — but a lap this far "
                   "off usually means the GPS finish-line trigger never fired "
                   "and the lap was cut by the odometer instead.")
        for w in mislabelled:
            st.write("- " + w)

st.subheader("Cost per lap, and what it wins")
best_laps = max([r["_laps"] for r in rows if r["_laps"]] or [0])
table = []
for r in sorted(rows, key=lambda r: r["lap_time_s"]):
    was = current.get(r["key"])
    table.append({
        "profile": r["key"],
        "label": r["label"],
        "lap time (s)": round(r["lap_time_s"], 1),
        "Wh / lap": round(r["energy_wh"], 1),
        "of which drag": "%.0f%%" % (100.0 * (r["aero_share"] or 0.0)),
        # The lap count stays a NUMBER. Putting the trophy in the same
        # column made it a mixed int/str column, which Arrow cannot
        # serialise — Streamlit recovers, but it logs a traceback and the
        # column stops sorting as a number. The marker gets its own.
        "LAPS in the race": r["_laps"],
        "best": "🏆" if (r["_laps"] and r["_laps"] == best_laps) else "",
        "stops": r["_stops"],
        "final SoC": r["_final_soc"],
        "Wh now": None if was is None else round(was, 1),
        "laps on those": r["_laps_now"],
    })
st.dataframe(table, width="stretch", hide_index=True)
st.caption("**LAPS in the race** is a full %.0f h from a full pack, simulated through the pit's OWN strategy engine — the same battery, the same charging curve and the same %d-charge regulation cap the Strategy tab uses. So it is what that tab will show once these energy numbers are in constants.py. **laps on those** is the same race on the figures constants.py holds today, so the difference is what adopting this matrix actually costs or buys."
           % (em.RACE_MIN / 60.0, _MAX_STOPS))

winner = next((r for r in rows if r["_laps"] == best_laps and best_laps), None)
if winner:
    fastest = min(rows, key=lambda r: r["lap_time_s"])
    if winner["key"] != fastest["key"]:
        # The whole reason this table is worth having. The quickest lap is not
        # the most laps once the charge stops are paid for, and which profile
        # wins moves as soon as the energy numbers move.
        st.success("**%s wins on these numbers: %d laps** — %d more than %s, "
                   "which laps %.0f s quicker but spends %.1f Wh more doing it. "
                   "The fastest profile is not the one that covers the most "
                   "ground once the charge stops are paid for."
                   % (winner["key"], winner["_laps"],
                      winner["_laps"] - (fastest["_laps"] or 0), fastest["key"],
                      winner["lap_time_s"] - fastest["lap_time_s"],
                      fastest["energy_wh"] - winner["energy_wh"]))
    else:
        st.success("**%s wins on these numbers: %d laps.**"
                   % (winner["key"], winner["_laps"]))

m1, m2, m3 = st.columns(3)
m1.metric("Paid per lap whatever the pace", "%.1f Wh" % (model.a * em.LAP_M),
          help="Rolling resistance, bearings and drivetrain loss. Paid per "
               "METRE, so going slower does not reduce it — it only takes "
               "longer. This is the number the ±10 % ladder gets wrong.")
m2.metric("Fitted from", "%d run%s" % (len(measurements),
                                       "" if len(measurements) == 1 else "s"))
m3.metric("Aero share", "assumed %.0f%%" % (100 * share) if model.assumed
          else "measured", help=model.basis)

if HAS_PLOTLY:
    ordered = sorted(rows, key=lambda r: r["lap_time_s"])
    fig = go.Figure()
    have_current = [r for r in ordered if current.get(r["key"]) is not None]
    if have_current:
        fig.add_trace(go.Scatter(
            x=[r["lap_time_s"] for r in have_current],
            y=[current[r["key"]] for r in have_current],
            mode="lines+markers", name="in constants.py now",
            line=dict(color=C_INSTALLED, width=2, dash="dot")))
    fig.add_trace(go.Scatter(
        x=[r["lap_time_s"] for r in ordered],
        y=[r["energy_wh"] for r in ordered],
        mode="lines+markers", name="this model",
        line=dict(color=C_BAD if bad else C_MODEL, width=2.5)))
    pts = [(m.implied_lap_time_s, m.energy_per_lap_wh) for m in measurements
           if m.implied_lap_time_s]
    if pts:
        fig.add_trace(go.Scatter(
            x=[p[0] for p in pts], y=[p[1] for p in pts], mode="markers",
            name="measured", marker=dict(color=C_MEASURED, size=11,
                                         symbol="diamond")))
    # The band the profiles actually cover. Outside it the green line is the
    # power law guessing, and the chart should show where that starts.
    fig.add_vrect(x0=lo_pace, x1=hi_pace, line_width=0,
                  fillcolor=C_MODEL, opacity=0.07)
    fig.update_layout(height=360, margin=dict(l=0, r=0, t=10, b=0),
                      paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)",
                      xaxis_title="lap time (s)  —  faster is left",
                      yaxis_title="Wh per lap",
                      legend=dict(orientation="h", y=1.12))
    st.plotly_chart(fig, width="stretch", config={"displaylogo": False})
    st.caption("Orange diamonds are what was actually measured; the green line "
               "is the model filling in profiles nobody has driven. The shaded "
               "band is the %.0f–%.0f s the five profiles cover — diamonds "
               "outside it are extrapolation. Where the green line sits ABOVE "
               "the grey one at the slow end, that is the placeholder ladder "
               "assuming rolling loss gets cheaper when you slow down. It does "
               "not." % (lo_pace, hi_pace))

with st.expander("Show the working"):
    st.code(em.render(model, rows, measurements, current), language="text")
    st.markdown(
        "**The model.** `E_lap = a·L + b·A`, where `L` is the 4000 m lap and "
        "`A` is ∫v²ds. `a` is everything paid per metre — rolling resistance, "
        "bearings, drivetrain loss — and `b` is aerodynamic drag, the part "
        "strategy actually buys and sells. Across the five profiles `A` spans "
        "**+31 % to −18 %** while `L` does not move at all.\n\n"
        "**Costing a lap that is not a profile.** `A` comes from a profile's "
        "own CSV when the pace matches one, and otherwise from `A ≈ C·t^−n` "
        "fitted across all five"
        + (" (here **n = %.2f**)" % law[1] if law else "")
        + ". A lap driven at 257 s is costed as a 257 s lap, never as the "
        "profile the pit happened to have sent.\n\n"
        "**Which watt-hours.** The car's own integration: signed motor power, "
        "trapezoidal, regen subtracting. Same basis as `last_lap_energy`, so "
        "these are directly comparable with the *measured* column the Strategy "
        "tab already shows. Motor-side, **not** pack-side — a pack sized off "
        "these alone would be sized short.")

st.subheader("To use these numbers")
st.caption("Copy this over the block between the two marker lines in "
           "Pit_Dashboard/constants.py, or type the values into the Profile "
           "Builder and press Save there. This app writes nothing.")
snippet = {r["key"]: {"label": r["label"],
                      "energy_wh": round(r["energy_wh"], 1),
                      "target_s": round(r["lap_time_s"], 1)}
           for r in rows}
st.code(pm.render_matrix(snippet), language="python")
if bad or extrapolated:
    st.error("The matrix above has the problems listed at the top of this "
             "page. Fix the measurements before using it.")
