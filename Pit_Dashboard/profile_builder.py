"""
profile_builder.py — build speed profiles from laps the car actually drove
==========================================================================
A SEPARATE app from the pit wall. Own port, own process, own session state, and
it opens telemetry.db READ-ONLY, so nothing it does can slow, lock or crash the
dashboard the engineers are working from.

    streamlit run Pit_Dashboard/profile_builder.py --server.port 8502

(or double-click "Build Speed Profiles.bat" at the repo root)

WHY IT IS NOT A TAB IN THE PIT DASHBOARD
Streamlit runs every fragment of a session on one script thread. Reading and
resampling whole laps is exactly the kind of work that, on that thread, stops the
speed tile updating — which is the bug we just spent a day removing from the
History tab. This is also not race-time work: it is done between sessions, by one
person, deliberately.

WHAT IT REPLACES
profiles/*.csv are synthetic — tools/generate_profiles.py scales one modelled lap
to five target times. This writes the same files from a lap the car really drove,
so the target the driver chases is a lap that actually happened at this circuit.

WHAT IT DOES NOT DO
It does not talk to the car, and there is no "record" button anywhere in the
system: every lap the car has ever driven is already in telemetry.db, so laps are
chosen AFTER the fact. That is strictly better than arming a recorder — you are
never limited to the laps somebody remembered to record, and you can change your
mind about any lap, any time.

The arithmetic lives in profile_build.py, which has no Streamlit in it and can be
exercised with `python Pit_Dashboard/profile_build.py`.
"""

import datetime
import json
import os
import re
import sys

import numpy as np
import pandas as pd
import streamlit as st

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
for _p in (_REPO_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import db                                                   # noqa: E402
import profile_build as pb                                  # noqa: E402
import speed_profile                                        # noqa: E402
from constants import STRATEGIES, DEFAULT_STRATEGY_KEY      # noqa: E402

try:
    import plotly.graph_objects as go
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

PROFILE_DIR = os.path.join(_REPO_ROOT, "profiles")
SIDECAR_PATH = os.path.join(PROFILE_DIR, "profiles.json")
BACKUP_DIR = os.path.join(PROFILE_DIR, "_backup")
BASELINE_KEY = DEFAULT_STRATEGY_KEY

# The five built-in targets are 10.5 s apart, so anything at or above 5.25 s
# would let one lap fall in two categories. Capped below that by construction.
DEFAULT_RADIUS_S = 3.0
MAX_RADIUS_S = 5.0
KEY_RE = re.compile(r"^[a-z0-9_]{1,32}$")

st.set_page_config(page_title="Speed Profile Builder", layout="wide",
                   page_icon=":material/route:")


# --------------------------------------------------------------------------- #
# Categories — the five built-ins plus anything the team has added
# --------------------------------------------------------------------------- #
def _default_categories():
    return {s["key"]: {"label": s["label"],
                       "target_s": round(float(s["lap_time_min"]) * 60.0, 1),
                       "radius_s": DEFAULT_RADIUS_S,
                       "energy_wh": s.get("energy_wh")}
            for s in STRATEGIES}


def load_sidecar():
    """profiles/profiles.json, merged over the five built-ins."""
    cats = _default_categories()
    try:
        with open(SIDECAR_PATH, encoding="utf-8") as fh:
            stored = json.load(fh)
        for key, meta in (stored.get("categories") or {}).items():
            cats.setdefault(key, {})
            cats[key].update(meta)
    except FileNotFoundError:
        pass
    except Exception as exc:                      # never let a bad file block work
        st.warning(f"profiles.json unreadable ({exc}) — using the built-in five.",
                   icon=":material/warning:")
    return cats


def save_sidecar(cats, provenance=None):
    """Write the sidecar atomically. Provenance is merged, never replaced."""
    existing = {}
    try:
        with open(SIDECAR_PATH, encoding="utf-8") as fh:
            existing = json.load(fh)
    except Exception:
        pass
    existing["categories"] = cats
    built = existing.setdefault("built", {})
    if provenance:
        built.update(provenance)
    os.makedirs(PROFILE_DIR, exist_ok=True)
    tmp = SIDECAR_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, indent=2, sort_keys=True)
    os.replace(tmp, SIDECAR_PATH)


# --------------------------------------------------------------------------- #
# Reading the store (read-only, always)
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=60, show_spinner="Reading laps…")
def load_laps():
    """The lap table, plus the alignment proof. One grouped pass over the store.

    Returns (DataFrame, offset, detail, db_mode). The DataFrame has one row per
    lap TRACE, already joined to the car's own per-lap figures at the offset the
    data itself says is right.
    """
    conn, mode = db.get_conn_ro()
    try:
        overview = db.lap_overview(conn)
        summary = db.fetch_lap_summary(conn)
        offset, detail = pb.check_lap_alignment(overview, summary)
        by_lap = {int(r["lap"]): r for r in summary if r["lap"] is not None}

        recs = []
        for r in overview:
            if r["trace_lap"] is None:
                continue
            trace_lap = int(r["trace_lap"])
            s = by_lap.get(trace_lap + (offset or 0))
            n = int(r["n_samples"] or 0)
            n_speed = int(r["n_speed"] or 0)
            end_m = float(r["trace_end_m"] or 0.0)
            recs.append({
                "Trace lap": trace_lap,
                "Lap time (s)": (float(s["lap_time_s"])
                                 if s and s["lap_time_s"] is not None else None),
                "Car distance (m)": (float(s["distance_m"])
                                     if s and s["distance_m"] is not None else None),
                "Trace distance (m)": end_m,
                "Energy (Wh)": (float(s["energy_wh"])
                                if s and s["energy_wh"] is not None else None),
                "Samples": n,
                "Speed %": (100.0 * n_speed / n) if n else 0.0,
                "Spacing (m)": (end_m / n) if n else 0.0,
                "Max speed": float(r["v_max_kmh"] or 0.0),
                "When": (datetime.datetime.fromtimestamp(r["t0"]).strftime("%d %b %H:%M")
                         if r["t0"] else ""),
            })
        return pd.DataFrame(recs), offset, detail, mode
    finally:
        conn.close()


@st.cache_data(ttl=60, show_spinner=False)
def load_lap_samples(trace_lap):
    """One lap's raw samples, as plain tuples so the cache can hash them."""
    conn, _mode = db.get_conn_ro()
    try:
        rows = db.fetch_lap_profile_samples(conn, int(trace_lap))
    finally:
        conn.close()
    return [(r["device_ts"], r["lap_distance_m"], r["mms_vehicle_speed_kmh"],
             r["lap_source"]) for r in rows]


@st.cache_data(ttl=30, show_spinner=False)
def load_installed(key, mtime):
    """The profile currently on disk for `key` — the corner-cap reference."""
    path = os.path.join(PROFILE_DIR, f"{key}.csv")
    if not os.path.exists(path):
        path = os.path.join(PROFILE_DIR, f"{BASELINE_KEY}.csv")
    p = speed_profile.load_csv(path, lap_length_m=pb.LAP_M)
    return path, list(p.distances_m), list(p.speeds_ms), list(p.sections)


def installed_profile(key):
    path = os.path.join(PROFILE_DIR, f"{key}.csv")
    stamp = os.path.getmtime(path) if os.path.exists(path) else 0.0
    src, d, v, sec = load_installed(key, stamp)
    return src, speed_profile.SpeedProfile(key, d, v, sec, lap_length_m=pb.LAP_M)


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
st.title(":material/route: Speed Profile Builder")
st.caption("Builds `profiles/*.csv` from laps the car actually drove. "
           "Reads `telemetry.db` read-only — it cannot affect the pit dashboard "
           "or the collector.")

laps_df, offset, align_detail, db_mode = load_laps()

# --- the alignment proof, which gates everything -------------------------- #
if offset is None:
    st.error("Cannot verify how lap traces line up with lap times — there are "
             "not enough completed laps in the store yet. Build nothing from "
             "this data.", icon=":material/error:")
    st.caption(align_detail)
    st.stop()

with st.expander(
        f":material/verified: Lap alignment verified — trace N pairs with lap "
        f"N+{offset}", expanded=(offset != 1)):
    st.write(
        "`calculated_lap` is the number of laps **completed**, so the samples "
        "carrying it are the lap being driven *next*, while `last_lap_time_s` "
        "on those same rows describes the lap just *finished*. Getting this "
        "join wrong files every profile under a neighbouring lap's time, and "
        "nothing on screen looks wrong — so it is measured here from the data "
        "rather than assumed.")
    st.code(align_detail, language=None)
    if offset != 1:
        st.warning(f"Expected offset 1 from the car's code; this store says "
                   f"{offset}. Investigate before building anything.",
                   icon=":material/warning:")

# --- settings -------------------------------------------------------------- #
with st.sidebar:
    st.markdown("### Build settings")
    smooth_pts = st.slider("Smoothing window", 1, 11, pb.DEFAULT_SMOOTH_POINTS, 2,
                           help="In 10 m grid points. 1 = raw. The window wraps "
                                "across the finish line so the start straight is "
                                "not flattened.")
    st.caption(f"= {smooth_pts * 10} m" if smooth_pts > 1 else "raw, no smoothing")
    corner_cap = st.toggle(
        "Cap corner speeds to the installed profile", value=True,
        help="Never target more speed through a turn than the profile the car "
             "already follows. Leave this on: at ~1 Hz a corner apex can be "
             "missed entirely, and the interpolation across the miss reads "
             "FASTER than the car actually went.")
    if not corner_cap:
        st.warning("Uncapped: an under-sampled apex can ask the driver to take "
                   "a corner faster than the car has been shown to take it.",
                   icon=":material/warning:")
    allow_gaps = st.toggle("Allow laps with telemetry gaps", value=False,
                           help="Off by default. A gap is filled from the "
                                "installed profile and recorded in the file's "
                                "provenance — it is not measured data.")
    st.divider()
    st.markdown("### Store")
    st.caption(f"Mode: `{db_mode}` "
               + ("(read-only at the file level)" if db_mode == "ro"
                  else "(query_only — writes refused at the SQL level)"))
    st.caption(f"{len(laps_df)} lap trace(s)")

# --- lap quality ----------------------------------------------------------- #
st.markdown("### :material/table_rows: Laps in the store")

if laps_df.empty:
    st.info("No laps recorded yet.", icon=":material/info:")
    st.stop()

verdicts, details = [], []
for _, row in laps_df.iterrows():
    why = []
    if row["Max speed"] > pb.LEGACY_SPEED_KMH:
        why.append("legacy 50x speeds")
    if row["Speed %"] < 99.0:
        why.append(f"speed missing on {100 - row['Speed %']:.0f}% of samples")
    if row["Samples"] < 100:
        why.append("too few samples")
    if row["Lap time (s)"] is None or pd.isna(row["Lap time (s)"]):
        why.append("no lap time from the car")
    if abs((row["Trace distance (m)"] or 0) - pb.LAP_M) > pb.MAX_LENGTH_ERROR_M:
        why.append(f"{row['Trace distance (m)']:.0f} m, not ~{pb.LAP_M:.0f} m")
    verdicts.append("Usable" if not why else "Rejected")
    details.append("; ".join(why))
laps_df = laps_df.assign(Verdict=verdicts, Why=details)

st.dataframe(
    laps_df.style.format({"Lap time (s)": "{:.1f}", "Car distance (m)": "{:.0f}",
                          "Trace distance (m)": "{:.0f}", "Energy (Wh)": "{:.1f}",
                          "Speed %": "{:.0f}", "Spacing (m)": "{:.1f}",
                          "Max speed": "{:.0f}"}, na_rep="—"),
    width="stretch", hide_index=True)
st.caption(
    "**Car distance** is what the car reported for that lap; **Trace distance** "
    "is how far this lap's own samples reach. They should agree to within one "
    "sample — a big disagreement means the trace/lap join is wrong for that lap. "
    f"Spacing is the average gap between samples: the profile grid is 10 m, so "
    f"anything above that is interpolated up, not measured.")

usable = laps_df[laps_df["Verdict"] == "Usable"].dropna(subset=["Lap time (s)"])
if usable.empty:
    st.warning("No lap in the store is usable as a profile yet. The columns "
               "above say why for each one.", icon=":material/warning:")
    st.stop()

# --- categories ------------------------------------------------------------ #
cats = load_sidecar()

st.markdown("### :material/category: Categories")
if HAS_PLOTLY:
    fig = go.Figure()
    fig.add_trace(go.Histogram(x=usable["Lap time (s)"], nbinsx=40,
                               marker_color="#00FFCC", name="usable laps"))
    for key, meta in sorted(cats.items(), key=lambda kv: kv[1]["target_s"]):
        t, r = float(meta["target_s"]), float(meta.get("radius_s", DEFAULT_RADIUS_S))
        fig.add_vrect(x0=t - r, x1=t + r, line_width=0, fillcolor="#FF9900",
                      opacity=0.16, annotation_text=key, annotation_position="top")
    fig.update_layout(height=240, margin=dict(l=0, r=0, t=24, b=0),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      xaxis_title="measured lap time (s)", showlegend=False)
    st.plotly_chart(fig, width="stretch", config={"displaylogo": False})
    st.caption("Shaded bands are the categories. If your laps sit outside all of "
               "them, the five built-in targets do not describe how this car "
               "actually runs — create a category where the laps really are.")

names = sorted(cats, key=lambda k: cats[k]["target_s"])
chosen_key = st.selectbox("Category", names,
                          index=names.index(BASELINE_KEY) if BASELINE_KEY in names else 0,
                          format_func=lambda k: f"{cats[k]['label']} · "
                                                f"{cats[k]['target_s']:.0f}s  [{k}]")
meta = cats[chosen_key]
radius = st.slider("Radius (s)", 0.5, MAX_RADIUS_S,
                   float(min(meta.get("radius_s", DEFAULT_RADIUS_S), MAX_RADIUS_S)),
                   0.5, help="How far a lap's time may sit from the target and "
                             "still belong to this category. Capped so that "
                             "neighbouring categories can never overlap.")
target = float(meta["target_s"])
inb = usable[(usable["Lap time (s)"] - target).abs() <= radius].copy()
inb["Δ vs target"] = inb["Lap time (s)"] - target
inb = inb.sort_values(by="Δ vs target", key=abs)

st.write(f"**{len(inb)}** usable lap(s) within ±{radius:.1f}s of "
         f"{target:.0f}s")

with st.expander(":material/add: Create a category from a lap you actually drove"):
    c1, c2, c3 = st.columns([1, 1, 1])
    seed = float(usable["Lap time (s)"].median())
    new_t = c1.number_input("Target lap time (s)", 60.0, 900.0, round(seed, 1), 0.5)
    new_key = c2.text_input("Key", value=f"real_{int(round(new_t))}s",
                            help="Becomes the filename and the string sent to "
                                 "the car. Lower case, digits, underscore.")
    new_label = c3.text_input("Label", value=f"Real {int(round(new_t))}s")
    if st.button(":material/add: Add category"):
        if not KEY_RE.match(new_key):
            st.error("Key must be lower-case letters, digits or underscore "
                     "(max 32 chars).", icon=":material/error:")
        elif new_key in cats:
            st.error(f"`{new_key}` already exists.", icon=":material/error:")
        else:
            clash = [k for k, m in cats.items()
                     if abs(float(m["target_s"]) - new_t)
                     < (float(m.get("radius_s", DEFAULT_RADIUS_S)) + DEFAULT_RADIUS_S)]
            if clash:
                st.error(f"Overlaps existing categor{'y' if len(clash)==1 else 'ies'}: "
                         f"{', '.join(clash)}. Move the target or shrink the radii.",
                         icon=":material/error:")
            else:
                cats[new_key] = {"label": new_label, "target_s": float(new_t),
                                 "radius_s": DEFAULT_RADIUS_S, "energy_wh": None}
                save_sidecar(cats)
                st.success(f"Added `{new_key}`.", icon=":material/check_circle:")
                st.rerun()

if inb.empty:
    st.info("No lap falls in this category. Widen the radius, or create a "
            "category where your laps actually are.", icon=":material/info:")
    st.stop()

# --- pick one lap ---------------------------------------------------------- #
st.markdown("### :material/check_circle: Choose the representative lap")
st.caption("One lap becomes the profile — not an average. Every number in the "
           "file is then something the car really did on one particular lap.")

lap_options = inb["Trace lap"].tolist()
pick = st.selectbox(
    "Lap", lap_options,
    format_func=lambda l: (
        f"trace {l} · {inb.loc[inb['Trace lap'] == l, 'Lap time (s)'].iloc[0]:.1f}s "
        f"({inb.loc[inb['Trace lap'] == l, 'Δ vs target'].iloc[0]:+.1f}s) · "
        f"{inb.loc[inb['Trace lap'] == l, 'Samples'].iloc[0]} samples · "
        f"{inb.loc[inb['Trace lap'] == l, 'When'].iloc[0]}"))

samples = load_lap_samples(pick)
src_path, baseline = installed_profile(chosen_key)
measured_time = float(inb.loc[inb["Trace lap"] == pick, "Lap time (s)"].iloc[0])

try:
    v_ms, diag = pb.build_profile(samples, baseline, smooth_points=smooth_pts,
                                  corner_cap=corner_cap, allow_gaps=allow_gaps)
except ValueError as exc:
    st.error(f"This lap cannot become a profile: {exc}", icon=":material/error:")
    st.stop()

m1, m2, m3, m4 = st.columns(4)
m1.metric("Measured lap", f"{measured_time:.1f} s")
m2.metric("Samples used", f"{diag['n_used']}")
m3.metric("Mean spacing", f"{diag['mean_spacing_m']:.1f} m")
m4.metric("Coverage", f"{diag['coverage_pct']:.0f} %")

notes = []
if diag["n_dropped_nonmonotonic"]:
    notes.append(f"{diag['n_dropped_nonmonotonic']} sample(s) dropped where the "
                 f"car was stationary or distance went backwards")
if diag["filled_ranges"]:
    notes.append(f"**{len(diag['filled_ranges'])} gap(s) filled from the "
                 f"installed profile — not measured**: "
                 + ", ".join(f"{a:.0f}–{b:.0f} m" for a, b in diag["filled_ranges"]))
if diag["capped_points"]:
    notes.append(f"{len(diag['capped_points'])} turn point(s) capped to the "
                 f"installed profile")
if diag["clamped_points"]:
    notes.append(f"{len(diag['clamped_points'])} point(s) clamped to the "
                 f"{pb.MIN_SPEED_MS} m/s floor")
if notes:
    st.info("  \n".join(f"· {n}" for n in notes), icon=":material/info:")

# --- the review chart ------------------------------------------------------ #
if HAS_PLOTLY:
    d_raw, v_raw, _ = pb.clean_samples(samples)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=list(baseline.distances_m),
                             y=[s * 3.6 for s in baseline.speeds_ms],
                             mode="lines", name=f"installed ({chosen_key})",
                             line=dict(color="#94a3b8", width=2, dash="dot")))
    fig.add_trace(go.Scatter(x=d_raw, y=v_raw, mode="markers",
                             name="measured samples",
                             marker=dict(color="#FF9900", size=4, opacity=0.55)))
    fig.add_trace(go.Scatter(x=pb.GRID_M, y=v_ms * 3.6, mode="lines",
                             name="new profile",
                             line=dict(color="#00FFCC", width=2.5)))
    for a, b in diag["filled_ranges"]:
        fig.add_vrect(x0=a, x1=b, line_width=0, fillcolor="#f87171", opacity=0.20)
    fig.update_layout(height=420, margin=dict(l=0, r=0, t=10, b=0),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      xaxis_title="distance around the lap (m)",
                      yaxis_title="km/h",
                      legend=dict(orientation="h", y=1.08))
    st.plotly_chart(fig, width="stretch", config={"displaylogo": False})
    st.caption("Orange dots are what the car actually reported. The green line is "
               "what will be written. Anywhere the green line sits above the "
               "dots with no dot nearby, the profile is interpolation, not "
               "measurement — which is what the corner cap is protecting you from.")

# --- write ----------------------------------------------------------------- #
st.markdown("### :material/save: Write the profile")
final_path = os.path.join(PROFILE_DIR, f"{chosen_key}.csv")
exists = os.path.exists(final_path)
st.write(f"Target file: `profiles/{chosen_key}.csv`"
         + ("  — **this replaces the profile the car follows today**" if exists else ""))

if st.button(":material/save: Build and write this profile", type="primary"):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    staged = final_path + ".staged"
    pb.write_rows(staged, pb.GRID_M.tolist(), v_ms.tolist(), baseline.sections)

    ok, checks = pb.validate_profile(staged, measured_time,
                                     os.path.join(PROFILE_DIR, f"{BASELINE_KEY}.csv"))
    for level, msg in checks:
        {"ok": st.success, "warn": st.warning, "error": st.error}[level](
            msg, icon={"ok": ":material/check_circle:", "warn": ":material/warning:",
                       "error": ":material/error:"}[level])

    if not ok:
        os.remove(staged)
        st.error("Not written — the staged file failed validation.",
                 icon=":material/error:")
    else:
        if exists:
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = os.path.join(BACKUP_DIR, f"{chosen_key}.{stamp}.csv.bak")
            with open(final_path, "rb") as a, open(backup, "wb") as b:
                b.write(a.read())
            st.caption(f"Previous profile backed up to "
                       f"`profiles/_backup/{os.path.basename(backup)}`")
        os.replace(staged, final_path)          # atomic

        cats[chosen_key]["radius_s"] = float(radius)
        save_sidecar(cats, provenance={chosen_key: {
            "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "trace_lap": int(pick), "summary_lap": int(pick) + int(offset),
            "measured_lap_time_s": measured_time,
            "n_samples": diag["n_used"], "max_gap_m": diag["max_gap_m"],
            "mean_spacing_m": diag["mean_spacing_m"],
            "coverage_pct": diag["coverage_pct"],
            "smoothing_window_m": diag["smoothing_window_m"],
            "corner_cap": diag["corner_cap"],
            "filled_ranges": diag["filled_ranges"],
        }})
        st.success(f"Wrote profiles/{chosen_key}.csv from trace lap {pick}.",
                   icon=":material/check_circle:")
        load_installed.clear()

        st.markdown("#### :material/rocket_launch: Getting it onto the car")
        st.warning(
            "**The car loads every profile once, at startup.** Replacing the "
            "file does nothing to a car that is already running — the HUD has "
            "to be restarted on the Pi.", icon=":material/warning:")
        st.code(
            "# on this laptop\n"
            "git add profiles/ && git commit -m \"profiles: measured at Zolder\" "
            "&& git push\n\n"
            "# on the car's Pi\n"
            "cd ~/Desktop/THE-RACE-main && git pull\n"
            "./deploy/stop_hud.sh && ./deploy/start_hud.sh", language="bash")
        st.caption("Then send the strategy from the pit dashboard and watch for "
                   "the car's ack — that is your confirmation it took effect.")

st.divider()
st.caption("Undo everything: `git checkout -- profiles/` on both machines, then "
           "restart the HUD. That works because the five original keys are never "
           "renamed. `python tools/generate_profiles.py` rebuilds the synthetic "
           "five from 210s.xlsx if git is not an option.")
