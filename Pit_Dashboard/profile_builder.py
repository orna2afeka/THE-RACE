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


st.set_page_config(page_title="Speed Profile Builder", layout="wide",
                   page_icon=":material/route:")


# --------------------------------------------------------------------------- #
# Categories — the five built-ins plus anything the team has added
# --------------------------------------------------------------------------- #
def _default_categories():
    return {s["key"]: {"label": s["label"],
                       "target_s": round(float(s["lap_time_min"]) * 60.0, 1),
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
def _col(row, name, default=None):
    """One column off a sqlite3.Row, tolerating its absence.

    Row raises IndexError for a key that is not in the result, which is a hard
    crash for what may be a cosmetic column. It happens for a mundane reason
    that will happen again: Streamlit re-runs the main script on every
    interaction but does NOT re-import modules, and this project sets
    fileWatcherType = "none", so an app left running while db.py gains a column
    keeps the OLD db module in memory and the new page code asks it for
    something it cannot return. Restarting the app fixes it; crashing over a
    display field is not a reasonable way to say so.
    """
    try:
        value = row[name]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


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
                # How the lap boundary was decided. "gps" is a real finish-line
                # crossing; "odometer" means the GPS trigger MISSED and the
                # distance backstop fired, which makes the lap's whole distance
                # axis an estimate — worth seeing before trusting a profile
                # built from it.
                "lap_source": _col(r, "lap_source", "—"),
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
# Demo laps — so the tool can be reviewed before the car has ever run at Zolder
# --------------------------------------------------------------------------- #
# Every lap below is INVENTED. The point is to see how the table, the buckets
# and the rejection reasons read while there is still time to change them, so
# the set deliberately includes the failures a real session produces: a
# telemetry hole, a lap the trigger cut short, a lap the GPS never triggered,
# and one carrying the 50x speeds of a pre-decode-fix database.
#
# Nothing here can be written to a real profile — see the guard on the write
# button — because a synthetic lap in profiles/base_210s.csv would be a lie the
# car would then drive to.
DEMO_LAPS = [
    # (lap_time_s, energy_wh, lap_source, flaw)
    (208.4, 79.6, "gps", None),
    (211.9, 82.1, "gps", None),
    (209.7, 78.9, "gps", None),
    (213.2, 84.7, "gps", None),
    (210.6, 81.3, "gps", "gap"),
    (207.9, 77.8, "gps", None),
    (231.4, 71.2, "gps", None),
    (229.8, 69.9, "gps", None),
    (233.1, 72.6, "gps", None),
    (190.2, 95.4, "gps", None),
    (188.7, 97.1, "gps", None),
    (191.5, 94.2, "odometer", None),
    (204.3, 80.2, "gps", "short"),
    (215.0, 83.4, "odometer", None),
    (198.6, 88.0, "gps", None),
    (212.4, 81.9, "gps", "legacy"),
]


def _demo_samples(trace_lap):
    """One demo lap's samples, in the shape fetch_lap_profile_samples returns."""
    idx = int(trace_lap)
    lap_time, _wh, source, flaw = DEMO_LAPS[idx % len(DEMO_LAPS)]
    length = 3860.0 if flaw == "short" else 4000.0
    rows = pb._synthetic_lap(lap_time_s=lap_time, spacing_m=19.0,
                             length_m=length, jitter=1.4, seed=idx + 1)
    if flaw == "gap":
        rows = [r for r in rows if not (1500.0 < r[1] < 1660.0)]
    if flaw == "legacy":
        rows = [(t, d, v * 50.0, source) for t, d, v, _s in rows]
    return [(r[0], r[1], r[2], source) for r in rows]


def _demo_laps_df():
    """The lap table, built from DEMO_LAPS rather than telemetry.db."""
    recs = []
    for i, (lap_time, wh, source, flaw) in enumerate(DEMO_LAPS):
        samples = _demo_samples(i)
        d, v, diag = pb.clean_samples(samples)
        recs.append({
            "Trace lap": i,
            "Lap time (s)": lap_time,
            "Car distance (m)": diag["length_m"] + 8.0,
            "Trace distance (m)": diag["length_m"],
            "Energy (Wh)": wh,
            "Samples": diag["n_used"],
            "Speed %": 100.0,
            "Spacing (m)": diag["mean_spacing_m"],
            "Max speed": diag["max_kmh"],
            "When": f"demo lap {i}",
            "lap_source": source,
        })
    return pd.DataFrame(recs)


# --------------------------------------------------------------------------- #
# The matrix — pure helpers, so the click mapping can be reasoned about
# --------------------------------------------------------------------------- #
# How far a lap may sit from its nearest profile before the cell is flagged.
# The five built-in targets are ~10.5 s apart, so for the middle columns a lap
# can never be much more than 5 s from its nearest target -- which means amber
# fires almost only at the ENDS of the ladder: a lap faster than fast_189s or
# slower than slow_231s. That is exactly the case worth flagging, because those
# are the laps whose profile would be named after a pace it does not run.
FAR_S = 5.0

# A Styler can set background-color, color and font-weight and nothing else --
# no icons, no borders. So the chosen mark is a TICK IN THE DISPLAY VALUE, and
# the colour only supports it: a tick survives a screenshot, a projector and a
# colour-blind reader, and it is still there if the CSS never arrives.
CHOSEN_CSS = "background-color:#00FFCC; color:#04140f; font-weight:700"
FAR_CSS = "background-color:#4a3410; color:#ffb84d; font-weight:600"
REJECT_CSS = "color:#64748b"


def profile_columns(cats):
    """The profile columns, target-ascending. Column name IS the profile key.

    Naming the column after the key rather than the label is deliberate: the
    column name is what a click returns, and the key is the filename that gets
    written and the string the car is sent. Keeping all three identical removes
    a whole class of mapping bug, and two profiles could share a label (labels
    come from profiles.json, which a human types into) while keys cannot.
    """
    return sorted(
        ({"key": k, "label": m.get("label") or k,
          "target_s": float(m.get("target_s") or 0.0)} for k, m in cats.items()),
        key=lambda c: c["target_s"])


def nearest_key(lap_time, columns):
    """Which profile a lap belongs to: simply the closest target.

    No radius and no "unassigned" bucket -- every lap that has a time lands
    somewhere and stays visible. A lap far from its nearest target is FLAGGED
    (amber, and the detail panel says how far in words) rather than hidden,
    because a lap you cannot see is a lap you cannot judge.
    """
    if lap_time is None or pd.isna(lap_time) or not columns:
        return None
    return min(columns, key=lambda c: abs(lap_time - c["target_s"]))["key"]


def quick_check(row):
    """Why this lap could not become a profile, from the grouped query alone.

    Cheap on purpose: gaps and coverage need the lap's own samples read, which
    happens only for the focused lap. So a lap can pass here and still be
    refused in the detail panel -- said plainly on screen rather than left to
    look like the tool contradicting itself.
    """
    why = []
    if row["Max speed"] > pb.LEGACY_SPEED_KMH:
        why.append("pre-decode-fix rows: speeds ~50x too high, not rescalable")
    if row["Speed %"] < 99.0:
        why.append(f"speed missing on {100 - row['Speed %']:.0f}% of samples")
    if row["Samples"] < 100:
        why.append(f"only {int(row['Samples'])} samples")
    if row["Lap time (s)"] is None or pd.isna(row["Lap time (s)"]):
        why.append("the car never reported a time for this lap")
    if abs((row["Trace distance (m)"] or 0) - pb.LAP_M) > pb.MAX_LENGTH_ERROR_M:
        why.append(f"{row['Trace distance (m)']:.0f} m, not ~{pb.LAP_M:.0f} m")
    return why


def lap_meta(laps_df, columns):
    """{lap: facts} — everything the matrix, the colours and the panel need."""
    by_key = {c["key"]: c for c in columns}
    out = {}
    for _, row in laps_df.iterrows():
        lap = int(row["Trace lap"])
        t = row["Lap time (s)"]
        t = None if (t is None or pd.isna(t)) else float(t)
        key = nearest_key(t, columns)
        out[lap] = {
            "time": t,
            "key": key,
            "delta": None if (t is None or key is None)
                     else t - by_key[key]["target_s"],
            "rejects": quick_check(row),
            "when": row.get("When", ""),
            "energy": row.get("Energy (Wh)"),
            "source": row.get("lap_source", "—"),
            "samples": int(row["Samples"]),
            "spacing": float(row["Spacing (m)"]),
        }
    return out


def build_matrix(laps_df, columns, meta, chosen):
    """(numeric view, display frame). Rows sorted by lap time, no-time laps last.

    Two frames because they do different jobs: the numeric one is the truth the
    logic reads, the display one carries the tick. They share an index, so a
    selection's row position means the same row in both.
    """
    laps = [int(r["Trace lap"]) for _, r in laps_df.iterrows()]
    laps.sort(key=lambda l: (meta[l]["time"] is None,
                             meta[l]["time"] if meta[l]["time"] is not None else 0.0))

    cols = [c["key"] for c in columns]
    view = pd.DataFrame({"Lap": laps})
    for k in cols:
        view[k] = [meta[l]["time"] if meta[l]["key"] == k else np.nan for l in laps]
    view = view.reset_index(drop=True)

    disp = view.copy()
    for k in cols:
        disp[k] = [
            "" if pd.isna(view.at[i, k])
            else (f"✓ {view.at[i, k]:.1f}" if chosen.get(k) == int(view.at[i, "Lap"])
                  else f"{view.at[i, k]:.1f}")
            for i in view.index]
    return view, disp


def style_matrix(view, disp, columns, meta, chosen):
    cols = [c["key"] for c in columns]

    def paint(_df):
        css = pd.DataFrame("", index=disp.index, columns=disp.columns)
        for i in view.index:
            lap = int(view.at[i, "Lap"])
            m = meta[lap]
            for k in cols:
                if pd.isna(view.at[i, k]):
                    continue
                if chosen.get(k) == lap:
                    css.at[i, k] = CHOSEN_CSS
                elif m["rejects"]:
                    css.at[i, k] = REJECT_CSS
                elif m["delta"] is not None and abs(m["delta"]) > FAR_S:
                    css.at[i, k] = FAR_CSS
        return css

    return disp.style.apply(paint, axis=None)


def resolve_click(cells, view, columns, meta):
    """(lap, profile_key) from a selection. Never raises, never guesses wrongly.

    A click anywhere on a row identifies the LAP; which profile it means depends
    on where in the row it landed. Clicking a column the lap does not belong to
    still opens that lap -- on its OWN column -- and the panel says so, rather
    than silently doing something else.
    """
    if not cells:
        return None, None
    row, col = cells[0]
    if row not in view.index:
        return None, None
    lap = int(view.at[row, "Lap"])
    own = meta[lap]["key"]
    keys = {c["key"] for c in columns}
    if col in keys and not pd.isna(view.at[row, col]):
        return lap, col
    return lap, own


def reset_selection():
    """Drop every choice. Bound to the demo toggle -- see the note there."""
    for k in ("pb_chosen", "pb_focus", "pb_focus_key"):
        st.session_state.pop(k, None)


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
st.title(":material/route: Speed Profile Builder")

st.session_state.setdefault("pb_chosen", {})
st.session_state.setdefault("pb_focus", None)
st.session_state.setdefault("pb_focus_key", None)

real_laps_df, real_offset, real_detail, db_mode = load_laps()
_has_real = (real_offset is not None and not real_laps_df.empty)

s1, s2 = st.columns([3, 1])
with s2:
    # on_change is REQUIRED, not tidiness. Choices live in session state now, so
    # without it you could choose three demo laps, switch demo off, and the write
    # panel would offer to write invented laps as real ones. write_chosen()
    # re-checks demo as well -- a disabled button is a UI property, the refusal
    # belongs in the write path.
    demo = st.toggle("Demo laps", value=not _has_real, key="demo_mode",
                     on_change=reset_selection,
                     help="Invented laps, for seeing how this reads before the "
                          "car has run at Zolder. Nothing built from them can "
                          "be written to a real profile.")

if demo:
    laps_df, offset, align_detail = _demo_laps_df(), 1, "demo data — not measured"
else:
    laps_df, offset, align_detail = real_laps_df, real_offset, real_detail

# --- the alignment gate: same behaviour as before, a fraction of the pixels -- #
if offset is None:
    st.error("Cannot verify how lap traces line up with lap times — there are "
             "not enough completed laps in the store yet. Build nothing from "
             "this data.", icon=":material/error:")
    st.caption(align_detail)
    st.stop()

with s1:
    bits = [f"{'DEMO — invented laps' if demo else 'Alignment verified'}"
            f" (trace N ↔ lap N+{offset})",
            f"store `{db_mode}`", f"{len(laps_df)} lap trace(s)"]
    st.caption(" · ".join(bits))
    with st.popover("what these mean"):
        if demo:
            st.warning("**Every number on this page is invented.** The car has "
                       "not run at Zolder yet. Some laps are deliberately "
                       "faulty so the rejection reasons can be reviewed. "
                       "Nothing built from them can be written.",
                       icon=":material/science:")
        st.write(
            "`calculated_lap` is the number of laps **completed**, so the "
            "samples carrying it are the lap being driven *next*, while "
            "`last_lap_time_s` on those same rows describes the lap just "
            "*finished*. Getting this join wrong files every profile under a "
            "neighbouring lap's time, and nothing on screen looks wrong — so it "
            "is measured from the data rather than assumed.")
        st.code(align_detail, language=None)
        st.write(f"Store opened `{db_mode}` — "
                 + ("read-only at the file level."
                    if db_mode == "ro" else
                    "writes refused at the SQL level (query_only)."))

if offset != 1:
    st.warning(f"Expected offset 1 from the car's code; this store says "
               f"{offset}. Investigate before building anything.",
               icon=":material/warning:")

if laps_df.empty:
    st.info("No laps recorded yet.", icon=":material/info:")
    st.stop()

cats = load_sidecar()
columns = profile_columns(cats)
meta = lap_meta(laps_df, columns)
chosen = st.session_state["pb_chosen"]
view, disp = build_matrix(laps_df, columns, meta, chosen)

cfg = {c["key"]: st.column_config.Column(
    width="small",
    help=f"{c['label']} — target {c['target_s']:.0f} s · writes "
         f"profiles/{c['key']}.csv") for c in columns}
cfg["Lap"] = st.column_config.Column(width="small", help="The car's lap number.")

event = st.dataframe(style_matrix(view, disp, columns, meta, chosen),
                     key="pb_matrix", on_select="rerun",
                     selection_mode="single-cell", hide_index=True,
                     column_config=cfg, width="stretch",
                     height=min(38 * len(view) + 45, 520))

st.caption(
    "Each lap sits under the profile its **lap time** is closest to. Click any "
    "lap to open it below. ✓ marks a lap you have chosen; :orange[amber] means "
    "the lap is more than "
    f"{FAR_S:.0f} s from that profile's target; grey means it cannot be built "
    "(the panel says why). Sorting clears the blue outline — your choices are "
    "kept, and listed under **Write** below.")

clicked_lap, clicked_key = resolve_click(list(event.selection.cells), view,
                                         columns, meta)
if clicked_lap is not None:
    st.session_state["pb_focus"] = clicked_lap
    st.session_state["pb_focus_key"] = clicked_key


# --------------------------------------------------------------------------- #
# The detail panel
# --------------------------------------------------------------------------- #
def lap_detail(lap, key, meta, columns, demo):
    by_key = {c["key"]: c for c in columns}
    m = meta[lap]
    with st.container(border=True):
        h1, h2 = st.columns([8, 1])
        with h2:
            if st.button("Close", key="pb_close", width="stretch"):
                st.session_state["pb_focus"] = None
                st.rerun()
        with h1:
            t = "—" if m["time"] is None else f"{m['time']:.1f} s"
            st.markdown(f"#### Lap {lap} · {t}"
                        + (f" · {m['when']}" if m["when"] else "")
                        + (f" · nearest profile `{m['key']}`" if m["key"] else ""))

        if m["key"] is None:
            st.warning("The car never reported a time for this lap, so it "
                       "cannot be placed under a profile or built into one. "
                       "This is usually the lap still in progress.",
                       icon=":material/info:")
            return

        # The honesty line. Always present, always in words -- colour is never
        # the only thing telling you a lap is a poor match for its column.
        target = by_key[key]["target_s"]
        d = (m["time"] - target) if m["time"] is not None else None
        if key != m["key"]:
            st.markdown(f":orange[Lap {lap} is a {m['time']:.1f} s lap — that "
                        f"puts it under `{m['key']}`, not `{key}`.]")
        word = "slower" if (d or 0) > 0 else "faster"
        line = f"{abs(d):.1f} s {word} than `{key}`'s {target:.0f} s target."
        st.markdown(f":orange[{line}]" if abs(d) > FAR_S else line)

        if m["rejects"]:
            st.error("Cannot be built: " + "; ".join(m["rejects"]),
                     icon=":material/error:")
            return

        c1, c2, c3 = st.columns(3)
        smooth_pts = c1.slider(
            "Smoothing window", 1, 11, pb.DEFAULT_SMOOTH_POINTS, 2,
            key=f"smooth_{key}",
            help="In 10 m grid points. 1 = raw. The window wraps across the "
                 "finish line so the start straight is not flattened.")
        corner_cap = c2.toggle(
            "Cap corner speeds", value=True, key=f"cap_{key}",
            help="Never target more speed through a turn than the profile the "
                 "car already follows. Leave this on: at ~1 Hz a corner apex "
                 "can be missed entirely, and the interpolation across the "
                 "miss reads FASTER than the car actually went.")
        allow_gaps = c3.toggle(
            "Allow telemetry gaps", value=False, key=f"gaps_{key}",
            help="Off by default. A gap is filled from the installed profile "
                 "and recorded in the file's provenance — it is not measured "
                 "data.")
        if not corner_cap:
            st.warning("Uncapped: an under-sampled apex can ask the driver to "
                       "take a corner faster than the car has been shown to "
                       "take it.", icon=":material/warning:")

        samples = _demo_samples(lap) if demo else load_lap_samples(lap)
        _src, baseline = installed_profile(key)
        try:
            v_ms, diag = pb.build_profile(samples, baseline,
                                          smooth_points=smooth_pts,
                                          corner_cap=corner_cap,
                                          allow_gaps=allow_gaps)
        except ValueError as exc:
            st.error(f"This lap cannot become a profile: {exc}",
                     icon=":material/error:")
            return

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Measured lap", f"{m['time']:.1f} s")
        m2.metric("Samples used", f"{diag['n_used']}")
        m3.metric("Mean spacing", f"{diag['mean_spacing_m']:.1f} m")
        m4.metric("Coverage", f"{diag['coverage_pct']:.0f} %")

        notes = []
        if diag["n_dropped_nonmonotonic"]:
            notes.append(f"{diag['n_dropped_nonmonotonic']} sample(s) dropped "
                         f"where the car was stationary or distance went "
                         f"backwards")
        if diag["filled_ranges"]:
            notes.append(f"**{len(diag['filled_ranges'])} gap(s) filled from "
                         f"the installed profile — not measured**: "
                         + ", ".join(f"{a:.0f}–{b:.0f} m"
                                     for a, b in diag["filled_ranges"]))
        if diag["capped_points"]:
            notes.append(f"{len(diag['capped_points'])} turn point(s) capped to "
                         f"the installed profile")
        if diag["clamped_points"]:
            notes.append(f"{len(diag['clamped_points'])} point(s) clamped to "
                         f"the {pb.MIN_SPEED_MS} m/s floor")
        if notes:
            st.info("  \n".join(f"· {n}" for n in notes), icon=":material/info:")

        if HAS_PLOTLY:
            d_raw, v_raw, _ = pb.clean_samples(samples)
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=list(baseline.distances_m),
                                     y=[s * 3.6 for s in baseline.speeds_ms],
                                     mode="lines", name=f"installed ({key})",
                                     line=dict(color="#94a3b8", width=2,
                                               dash="dot")))
            fig.add_trace(go.Scatter(x=d_raw, y=v_raw, mode="markers",
                                     name="measured samples",
                                     marker=dict(color="#FF9900", size=4,
                                                 opacity=0.55)))
            fig.add_trace(go.Scatter(x=pb.GRID_M, y=v_ms * 3.6, mode="lines",
                                     name="new profile",
                                     line=dict(color="#00FFCC", width=2.5)))
            for a, b in diag["filled_ranges"]:
                fig.add_vrect(x0=a, x1=b, line_width=0, fillcolor="#f87171",
                              opacity=0.20)
            fig.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0),
                              paper_bgcolor="rgba(0,0,0,0)",
                              plot_bgcolor="rgba(0,0,0,0)",
                              xaxis_title="distance around the lap (m)",
                              yaxis_title="km/h",
                              legend=dict(orientation="h", y=1.10))
            st.plotly_chart(fig, width="stretch",
                            config={"displaylogo": False})
            st.caption("Orange dots are what the car actually reported. The "
                       "green line is what will be written. Anywhere the green "
                       "line sits above the dots with no dot nearby, the "
                       "profile is interpolation, not measurement — which is "
                       "what the corner cap is protecting you from.")

        held = st.session_state["pb_chosen"].get(key)
        if held == lap:
            if st.button(f":material/close: Unchoose lap {lap}", key="pb_unchoose"):
                st.session_state["pb_chosen"].pop(key, None)
                st.rerun()
        else:
            label = (f":material/check: Choose lap {lap} for `{key}`"
                     + (f" (replaces lap {held})" if held is not None else ""))
            others = [k for k, v in st.session_state["pb_chosen"].items()
                      if v == lap and k != key]
            if others:
                st.warning(f"Lap {lap} is already chosen for "
                           f"`{', '.join(others)}` — choosing it here as well "
                           f"writes the same lap into two files.",
                           icon=":material/warning:")
            if st.button(label, key="pb_choose", type="primary"):
                st.session_state["pb_chosen"][key] = lap
                st.rerun()


focus = st.session_state.get("pb_focus")
if focus is not None and focus in meta:
    lap_detail(focus, st.session_state.get("pb_focus_key") or meta[focus]["key"],
               meta, columns, demo)


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #
def write_chosen(chosen, cats, offset, meta, demo):
    """Stage and validate everything, then replace nothing or all of it.

    All-or-nothing because the five profiles are a LADDER: a half-updated set is
    the failure where the pit's consumption matrix silently mixes measured and
    modelled rows while looking perfectly coherent. If one lap fails, unchoose
    it and write again.

    Phase B is still not atomic ACROSS files -- a permission error or a full
    disk can land between two os.replace calls -- so exactly which files changed
    is tracked and reported. Never claim more than happened.
    """
    if demo:                       # defence in depth; the button is also disabled
        st.error("Refusing to write: these are invented demo laps.",
                 icon=":material/error:")
        return

    for stale in os.listdir(PROFILE_DIR):
        if stale.endswith(".staged"):
            os.remove(os.path.join(PROFILE_DIR, stale))

    staged, failures = {}, []
    for key, lap in sorted(chosen.items()):
        samples = load_lap_samples(lap)
        _src, baseline = installed_profile(key)
        try:
            v_ms, diag = pb.build_profile(
                samples, baseline,
                smooth_points=st.session_state.get(f"smooth_{key}",
                                                   pb.DEFAULT_SMOOTH_POINTS),
                corner_cap=st.session_state.get(f"cap_{key}", True),
                allow_gaps=st.session_state.get(f"gaps_{key}", False))
        except ValueError as exc:
            failures.append((key, lap, [("error", str(exc))]))
            continue
        path = os.path.join(PROFILE_DIR, f"{key}.csv") + ".staged"
        pb.write_rows(path, pb.GRID_M.tolist(), v_ms.tolist(), baseline.sections)
        ok, checks = pb.validate_profile(
            path, meta[lap]["time"],
            os.path.join(PROFILE_DIR, f"{BASELINE_KEY}.csv"))
        if ok:
            staged[key] = (path, lap, diag, checks)
        else:
            failures.append((key, lap, checks))

    if failures:
        for path, *_ in staged.values():
            os.remove(path)
        st.error(f"Nothing was written. {len(failures)} profile(s) failed "
                 f"validation:", icon=":material/error:")
        for key, lap, checks in failures:
            st.markdown(f"**`{key}` (lap {lap})**")
            for level, msg in checks:
                st.caption(f"· {msg}")
        st.caption("Unchoose the failing lap(s) and write again.")
        return

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    replaced, provenance = [], {}
    try:
        for key, (path, lap, diag, _checks) in staged.items():
            final = os.path.join(PROFILE_DIR, f"{key}.csv")
            if os.path.exists(final):
                with open(final, "rb") as a, \
                     open(os.path.join(BACKUP_DIR, f"{key}.{stamp}.csv.bak"),
                          "wb") as b:
                    b.write(a.read())
            os.replace(path, final)
            replaced.append(key)
            provenance[key] = {
                "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "trace_lap": int(lap), "summary_lap": int(lap) + int(offset),
                "measured_lap_time_s": meta[lap]["time"],
                "n_samples": diag["n_used"], "max_gap_m": diag["max_gap_m"],
                "mean_spacing_m": diag["mean_spacing_m"],
                "coverage_pct": diag["coverage_pct"],
                "smoothing_window_m": diag["smoothing_window_m"],
                "corner_cap": diag["corner_cap"],
                "filled_ranges": diag["filled_ranges"],
            }
    except Exception as exc:
        st.error(f"Stopped part-way through writing: {exc}\n\n"
                 f"These files WERE replaced: "
                 f"{', '.join(replaced) if replaced else 'none'}. "
                 f"Undo with `git checkout -- profiles/`.",
                 icon=":material/error:")
        return

    save_sidecar(cats, provenance=provenance)   # one call: it merges `built`
    load_installed.clear()
    # Clear the choices. Leaving them set means the Write button stays armed
    # against files that already hold exactly this, so a stray second click
    # rewrites them and cuts another backup for no change -- and worse, the
    # panel would keep claiming work is outstanding when it is done.
    st.session_state["pb_chosen"] = {}
    st.success(f"Wrote {len(replaced)} profile(s): "
               + ", ".join(f"`{k}.csv`" for k in replaced),
               icon=":material/check_circle:")

    # A ladder that is no longer in order is not an error, but it is not what
    # anyone intends either -- say so rather than let the strategy matrix show
    # a "faster" profile that is slower than the one below it.
    try:
        times = {}
        for c in profile_columns(cats):
            p = speed_profile.load_csv(
                os.path.join(PROFILE_DIR, f"{c['key']}.csv"),
                lap_length_m=pb.LAP_M)
            times[c["key"]] = p.lap_time_s()
        order = [k for k, _ in sorted(times.items(), key=lambda kv: kv[1])]
        expected = [c["key"] for c in profile_columns(cats)]
        if order != expected:
            st.warning("The profiles are no longer in order fastest-to-slowest: "
                       + " < ".join(f"`{k}` {times[k]:.0f}s" for k in order)
                       + ". The strategy matrix reads them as a ladder.",
                       icon=":material/warning:")
    except Exception:
        pass

    st.markdown("#### :material/rocket_launch: Getting it onto the car")
    st.warning("**The car loads every profile once, at startup.** Replacing the "
               "file does nothing to a car that is already running — the HUD "
               "has to be restarted on the Pi.", icon=":material/warning:")
    st.code("# on this laptop\n"
            "git add profiles/ && git commit -m \"profiles: measured at Zolder\" "
            "&& git push\n\n"
            "# on the car's Pi\n"
            "cd ~/Desktop/THE-RACE-main && git pull\n"
            "./deploy/stop_hud.sh && ./deploy/start_hud.sh", language="bash")
    st.caption("Then send the strategy from the pit dashboard and watch for the "
               "car's ack — that is your confirmation it took effect.")


if chosen:
    with st.container(border=True):
        st.markdown(f"### :material/save: Write {len(chosen)} profile(s)")
        by_key = {c["key"]: c for c in columns}
        for key in sorted(chosen, key=lambda k: by_key[k]["target_s"]):
            lap = chosen[key]
            m = meta.get(lap, {})
            c1, c2 = st.columns([9, 1])
            d = m.get("delta")
            time_txt = "—" if m.get("time") is None else f"{m['time']:.1f} s"
            delta_txt = "" if d is None else f" (Δ {d:+.1f} s)"
            replaces = ("  — **replaces the profile the car follows today**"
                        if os.path.exists(os.path.join(PROFILE_DIR, f"{key}.csv"))
                        else "")
            c1.markdown(f"`{key}` ← **lap {lap}** · {time_txt}{delta_txt}"
                        f" → `profiles/{key}.csv`{replaces}")
            if c2.button("✕", key=f"pb_drop_{key}", help=f"Unchoose {key}"):
                st.session_state["pb_chosen"].pop(key, None)
                st.rerun()

        if demo:
            st.button(f":material/save: Build and write {len(chosen)} profile(s)",
                      type="primary", disabled=True)
            st.caption(":orange[Disabled while Demo laps is on] — a made-up lap "
                       "written into `profiles/` is a target the car would "
                       "actually drive to.")
        elif st.button(f":material/save: Build and write {len(chosen)} profile(s)",
                       type="primary"):
            write_chosen(dict(chosen), cats, offset, meta, demo)

st.divider()
st.caption("Undo everything: `git checkout -- profiles/` on both machines, then "
           "restart the HUD. That works because the five original keys are never "
           "renamed. `python tools/generate_profiles.py` rebuilds the synthetic "
           "five from 210s.xlsx if git is not an option.")
