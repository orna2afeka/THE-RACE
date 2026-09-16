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
import profile_manage as pm                                 # noqa: E402
import speed_profile                                        # noqa: E402
from constants import (DEFAULT_STRATEGY_KEY,                # noqa: E402
                       SECTION_NAMES, MIN_LAPS_FOR_MEASURED)
from strategy_engine import SECTIONS_INFO                    # noqa: E402

try:
    import plotly.graph_objects as go
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

PROFILE_DIR = os.path.join(_REPO_ROOT, "profiles")
SIDECAR_PATH = os.path.join(PROFILE_DIR, "profiles.json")
BACKUP_DIR = os.path.join(PROFILE_DIR, "_backup")
BASELINE_KEY = DEFAULT_STRATEGY_KEY

# The nine sectors, exactly as the pit dashboard and the track map draw
# them: one definition of where S3 ends, or the energy breakdown and the
# sector timing beside it would disagree about the same piece of road.
SECTORS = [(sid, SECTION_NAMES.get(sid, f"S{sid}"),
            float(info["range"][0]), float(info["range"][1]))
           for sid, info in sorted(SECTIONS_INFO.items())]


st.set_page_config(page_title="Speed Profile Builder", layout="wide",
                   page_icon=":material/route:")


# --------------------------------------------------------------------------- #
# The profile matrix — saved in constants.py, drafted in profiles.json
# --------------------------------------------------------------------------- #
# SAVED is PROFILE_MATRIX in constants.py: what the pit dashboard reads.
# DRAFT is what this page shows: one row per CSV on disk, starting from its
# saved row, with any unsaved edits from profiles.json laid over it. Manage
# profiles and Build and write change the draft; only the Save button at the top
# of the page writes the code.
def load_saved_matrix():
    try:
        return pm.read_saved_matrix()
    except Exception as exc:                      # never let a bad file block work
        st.error(f"Cannot read PROFILE_MATRIX from constants.py ({exc}). Fix the "
                 f"file before saving from here.", icon=":material/error:")
        return None


def load_sidecar():
    """The DRAFT matrix: {key: {label, target_s, energy_wh}} for every CSV."""
    saved = load_saved_matrix() or {}
    edits = {}
    try:
        with open(SIDECAR_PATH, encoding="utf-8") as fh:
            edits = json.load(fh).get("categories") or {}
    except FileNotFoundError:
        pass
    except Exception as exc:
        st.warning(f"profiles.json unreadable ({exc}) — showing the saved matrix.",
                   icon=":material/warning:")

    cats = {}
    for key, path in speed_profile.available_profiles(PROFILE_DIR).items():
        row = dict(saved.get(key) or {})
        row.update(edits.get(key) or {})
        if not row.get("label"):
            row["label"] = key.replace("_", " ").title()
        if row.get("target_s") is None:
            try:
                lap_s = speed_profile.load_csv(path, lap_length_m=pb.LAP_M).lap_time_s()
                row["target_s"] = round(lap_s, 1)
            except Exception:
                row["target_s"] = 0.0
        row.setdefault("energy_wh", None)
        cats[key] = pm.normalise_entry(row)
    return cats


def save_sidecar(cats, provenance=None, drop_built=()):
    """Store the draft atomically. Provenance is merged, never replaced.

    Only rows that DIFFER from constants.py are stored as edits, so once Save
    has written the code the draft is empty again, and a hand edit to
    constants.py is never silently overridden by an old draft.

    `drop_built` removes provenance for keys whose file no longer came from a
    measured lap (regenerated or removed), so profiles.json never claims a
    curve was measured when it is not.
    """
    existing = {}
    try:
        with open(SIDECAR_PATH, encoding="utf-8") as fh:
            existing = json.load(fh)
    except Exception:
        pass
    saved = load_saved_matrix() or {}
    existing["categories"] = {
        k: pm.normalise_entry(v) for k, v in cats.items()
        if pm.normalise_entry(v) != saved.get(k)}
    built = existing.setdefault("built", {})
    if provenance:
        built.update(provenance)
    for key in drop_built:
        built.pop(key, None)
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
    """One row per DRIVE, plus the alignment proof. One pass over the store.

    Returns (DataFrame, offset, detail, db_mode).

    A row is a trace: (lap number, run). The lap number alone is not an
    identity -- it restarts whenever the car's counter is reset, so the same
    number comes back days later, and grouping by it welded three separate
    evenings into one "lap 1" whose time and energy were the MAX across all of
    them. See db.lap_traces.

    Each drive is then joined to the run that FOLLOWS it in time, because
    last_lap_* describes the lap just finished. Joined that way the alignment
    proof scores offset 1 at +1.000 with a median error of 0.6 s; joined by lap
    number on the same store it cannot tell the two offsets apart.
    """
    conn, mode = db.get_conn_ro()
    try:
        traces = pb.pair_traces(db.lap_traces(conn))
        offset, detail = pb.check_trace_alignment(traces)

        recs = []
        for t in traces:
            n = t["n_samples"]
            recs.append({
                "id": t["id"],
                "Trace lap": t["lap"],
                "Run": t["run"],
                "Lap time (s)": t["lap_time_s"],
                "Car distance (m)": t["distance_m"],
                "Trace distance (m)": t["trace_end_m"],
                "Energy (Wh)": t["energy_wh"],
                "Samples": n,
                "Speed %": (100.0 * t["n_speed"] / n) if n else 0.0,
                "Power %": (100.0 * t["n_power"] / n) if n else 0.0,
                "Max speed": t["v_max_kmh"],
                "When": datetime.datetime.fromtimestamp(t["t0"]).strftime("%d %b %H:%M"),
                "t0": t["t0"], "t1": t["t1"],
                # How the lap boundary was decided. "gps" is a real finish-line
                # crossing; "odometer" means the GPS trigger MISSED and the
                # distance backstop fired, which makes the lap's whole distance
                # axis an estimate — worth seeing before trusting a profile
                # built from it.
                "lap_source": t["lap_source"] or "—",
            })
        return pd.DataFrame(recs), offset, detail, mode
    finally:
        conn.close()


@st.cache_data(ttl=60, show_spinner=False)
def load_trace_samples(lap, t0, t1):
    """One DRIVE's raw samples, as plain tuples so the cache can hash them.

    Bounded by time as well as lap number, so it returns the drive that was
    clicked rather than every drive that ever carried this lap number. The
    fifth element is motor power, which the energy breakdown integrates;
    everything else reads positions 1 and 2 only.
    """
    conn, _mode = db.get_conn_ro()
    try:
        rows = db.fetch_trace_samples(conn, int(lap), float(t0), float(t1))
    finally:
        conn.close()
    return [(r["device_ts"], r["lap_distance_m"], r["mms_vehicle_speed_kmh"],
             r["lap_source"], r["mms_power_W"]) for r in rows]


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
    # (lap number, run, lap_time_s, energy_wh, lap_source, flaw)
    #
    # LAP 7 APPEARS TWICE on purpose. A lap number is not an identity -- reset
    # the counter between practice and the race and the numbers start again --
    # so the table has to stay readable when two drives both call themselves
    # lap 7. Everything else here is one drive per number, which is what a
    # clean race looks like.
    (1,  0, 208.4, 79.6, "gps", None),
    (2,  0, 211.9, 82.1, "gps", None),
    (3,  0, 209.7, 78.9, "gps", None),
    (4,  0, 213.2, 84.7, "gps", None),
    (5,  0, 210.6, 81.3, "gps", "gap"),
    (6,  0, 207.9, 77.8, "gps", None),
    (7,  0, 231.4, 71.2, "gps", None),
    (7,  1, 229.8, 69.9, "gps", "interleaved"),
    (8,  0, 233.1, 72.6, "gps", None),
    (9,  0, 190.2, 95.4, "gps", None),
    (10, 0, 188.7, 97.1, "gps", None),
    (11, 0, 191.5, 94.2, "odometer", None),
    (12, 0, 204.3, 80.2, "gps", "short"),
    (13, 0, 215.0, 83.4, "odometer", "nopower"),
    (14, 0, 198.6, 88.0, "gps", None),
    (15, 0, 212.4, 81.9, "gps", "legacy"),
]

# A plausible car, so the demo's sector split is shaped like a real lap instead
# of flat. Same road-load model the 210 s baseline spreadsheet uses:
#     P = v x (mass x a + rolling + aero x v^2)
# fitted to 210s.xlsx at 250 kg, 11 N and 0.05 N/(m/s)^2 -- that fit reproduces
# its Power(W) column with a residual of 0.00 N, so it is the spreadsheet's own
# model rather than an invention.
DEMO_MASS_KG, DEMO_ROLL_N, DEMO_AERO = 250.0, 11.0, 0.05

# A car does not get back everything it sheds -- most braking goes into the
# friction brakes as heat. On the store's own traces regen came back at roughly
# a tenth of what the lap consumed (10-19 Wh against 110-160 Wh), and the
# largest regen ever recorded was about -4 kW. Without these two lines the
# model treats every deceleration as fully recovered, gross and regen very
# nearly cancel, and the scaling below then inflates BOTH: the demo showed
# "regen recovered 142.2 Wh" on a lap that used 79.6 Wh.
DEMO_REGEN_FRACTION = 0.35
DEMO_REGEN_MAX_W = -3500.0

# How far the pit integral sits above the car's own figure on real traces
# (measured: 1.04-1.21 across nine of them). Baked in so the demo shows the
# self-check reading the state it will read at Zolder, rather than a perfect
# 1.00 that nobody will ever see.
DEMO_PIT_BIAS = 1.06


def _demo_index():
    return {pb.trace_id(lap, run): row for row in DEMO_LAPS
            for lap, run in ((row[0], row[1]),)}


def _demo_samples(tid):
    """One demo drive's samples, shaped like db.fetch_trace_samples rows.

    Timestamps are REAL SECONDS built from the speed trace, not the sample
    index pb._synthetic_lap hands back. The energy breakdown integrates power
    over dt, so an index axis would make every demo lap's Wh meaningless while
    still looking entirely plausible.
    """
    lap, run, lap_time, wh, source, flaw = _demo_index()[tid]
    length = 3860.0 if flaw == "short" else 4000.0

    # Shaped from the INSTALLED base profile, not from a periodic synthetic
    # lap. pb._synthetic_lap repeats every 1000 m, so its corners land at 650,
    # 1650, 2650 and 3650 m and miss Zolder's sector boundaries completely: the
    # braking fell inside S1 and S6 and the table showed S1 at 1% and S6 at a
    # NEGATIVE share. That reads as a broken feature rather than as invented
    # data, which defeats the point of having a demo at all.
    rng = np.random.default_rng(lap * 10 + run + 1)
    _base_src, base = installed_profile(BASELINE_KEY)
    k = (base.lap_time_s() or lap_time) / lap_time      # faster lap = more speed
    grid = np.arange(0.0, length, 19.0)
    speeds = (np.interp(grid, list(base.distances_m),
                        [v * 3.6 for v in base.speeds_ms]) * k
              + rng.normal(0.0, 1.4, size=len(grid)))
    rows = [(float(i), float(dd), float(max(vv, 5.0)), source)
            for i, (dd, vv) in enumerate(zip(grid, speeds))]

    # distance + speed -> a real time axis
    timed, t = [], 0.0
    for i, (_idx, d, v_kmh, _s) in enumerate(rows):
        if i:
            v_prev = max(rows[i - 1][2], 1.0) / 3.6
            v_now = max(v_kmh, 1.0) / 3.6
            t += (d - rows[i - 1][1]) / max(0.5, 0.5 * (v_prev + v_now))
        timed.append([t, d, v_kmh])

    powers = []
    for i, (_ts, _d, v_kmh) in enumerate(timed):
        v = max(v_kmh, 1.0) / 3.6
        if 0 < i < len(timed) - 1:
            dt = timed[i + 1][0] - timed[i - 1][0]
            a = ((timed[i + 1][2] - timed[i - 1][2]) / 3.6 / dt) if dt > 0 else 0.0
        else:
            a = 0.0
        p = v * (DEMO_MASS_KG * a + DEMO_ROLL_N + DEMO_AERO * v * v)
        if p < 0:
            p = max(p * DEMO_REGEN_FRACTION, DEMO_REGEN_MAX_W)
        powers.append(p)

    raw = 0.0
    for (ta, _da, _va), (tb, _db, _vb), pa, pbw in zip(timed, timed[1:],
                                                       powers, powers[1:]):
        dt = tb - ta
        if 0 < dt < pb.ENERGY_MAX_DT_S:
            raw += 0.5 * (pa + pbw) * dt / 3600.0
    scale = (wh * DEMO_PIT_BIAS / raw) if raw > 0 else 1.0
    powers = [p * scale for p in powers]

    out = [(ts, d, v, source, p) for (ts, d, v), p in zip(timed, powers)]

    if flaw == "gap":
        out = [r for r in out if not (1500.0 < r[1] < 1660.0)]
    if flaw == "legacy":
        out = [(ts, d, v * 50.0, source, p) for ts, d, v, _s, p in out]
    if flaw == "nopower":
        # The motor stopped reporting for a third of the lap. The shares would
        # then describe only the part that WAS reported, which is exactly the
        # thing the coverage check exists to refuse.
        out = [(ts, d, v, source, (None if 1200.0 < d < 2600.0 else p))
               for ts, d, v, _s, p in out]
    if flaw == "interleaved":
        # A second publisher: what three copies of the car code running at once
        # actually looks like in the store.
        for k in range(6):
            i = 5 + k * 9
            out.insert(i, (out[i][0], 40.0 + k, 25.0, source, 400.0))
    return out


def _demo_laps_df():
    """The lap table, built from DEMO_LAPS rather than telemetry.db."""
    recs = []
    for lap, run, lap_time, wh, source, _flaw in DEMO_LAPS:
        tid = pb.trace_id(lap, run)
        samples = _demo_samples(tid)
        _d, _v, diag = pb.clean_samples(samples)
        n_power = sum(1 for r in samples if r[4] is not None)
        recs.append({
            "id": tid,
            "Trace lap": lap,
            "Run": run,
            "Lap time (s)": lap_time,
            "Car distance (m)": diag["length_m"] + 8.0,
            "Trace distance (m)": diag["length_m"],
            "Energy (Wh)": wh,
            "Samples": diag["n_used"],
            "Speed %": 100.0,
            "Power %": 100.0 * n_power / max(1, len(samples)),
            "Max speed": diag["max_kmh"],
            "When": f"demo {lap}.{run}",
            "t0": 0.0, "t1": lap_time,
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
    """{trace id: facts} — everything the matrix, the colours and the panel need.

    Keyed by TRACE ID, not by lap number: two drives can both be lap 7.
    """
    by_key = {c["key"]: c for c in columns}
    # A lap number more than one drive claims gets a date on it. Unique numbers
    # -- a clean race -- keep the bare number, so the common case stays plain.
    counts = {}
    for _, row in laps_df.iterrows():
        counts[int(row["Trace lap"])] = counts.get(int(row["Trace lap"]), 0) + 1

    def _num(value):
        return None if value is None or pd.isna(value) else float(value)

    out = {}
    for _, row in laps_df.iterrows():
        tid = row["id"]
        t = _num(row["Lap time (s)"])
        key = nearest_key(t, columns)
        lap_no = int(row["Trace lap"])
        out[tid] = {
            "id": tid,
            "lap": lap_no,
            "run": int(row["Run"]),
            "label": (f"{lap_no}" if counts.get(lap_no, 0) < 2
                      else f"{lap_no} · {row.get('When', '')}"),
            "time": t,
            "key": key,
            "delta": None if (t is None or key is None)
                     else t - by_key[key]["target_s"],
            "rejects": quick_check(row),
            "when": row.get("When", ""),
            "energy": _num(row.get("Energy (Wh)")),
            "distance": _num(row.get("Car distance (m)")),
            "power_pct": float(row.get("Power %", 0.0) or 0.0),
            "source": row.get("lap_source", "—"),
            "samples": int(row["Samples"]),
            "t0": float(row.get("t0", 0.0) or 0.0),
            "t1": float(row.get("t1", 0.0) or 0.0),
        }
    return out


def efficiency_rank(tid, meta):
    """(rank, n) of this drive among the drives under the same profile.

    By the CAR's energy figure, never the pit integral — the pit reads a few
    percent high and the bias is not identical lap to lap, so ranking on it
    could reorder two laps that are genuinely a whisker apart.
    """
    m = meta[tid]
    if m["key"] is None or m["energy"] is None:
        return None
    peers = [x for x in meta.values()
             if x["key"] == m["key"] and x["energy"] is not None]
    if len(peers) < 2:
        return None
    peers.sort(key=lambda x: x["energy"])
    return [p["id"] for p in peers].index(tid) + 1, len(peers)


def build_matrix(laps_df, columns, meta, chosen):
    """(numeric view, display frame). Rows sorted by lap time, no-time laps last.

    Two frames because they do different jobs: the numeric one is the truth the
    logic reads, the display one carries the tick and the energy. They share an
    index, so a selection's row position means the same row in both.

    `view` also carries `_id`, which `disp` does not: the row position a click
    returns has to resolve to a DRIVE, and the visible Lap column is a label
    that two rows can legitimately share.
    """
    ids = [r["id"] for _, r in laps_df.iterrows()]
    ids.sort(key=lambda i: (meta[i]["time"] is None,
                            meta[i]["time"] if meta[i]["time"] is not None else 0.0))

    cols = [c["key"] for c in columns]
    view = pd.DataFrame({"_id": ids, "Lap": [meta[i]["label"] for i in ids]})
    for k in cols:
        view[k] = [meta[i]["time"] if meta[i]["key"] == k else np.nan for i in ids]
    view = view.reset_index(drop=True)

    disp = view.drop(columns=["_id"]).copy()
    for k in cols:
        cells = []
        for i in view.index:
            tid = view.at[i, "_id"]
            if pd.isna(view.at[i, k]):
                cells.append("")
                continue
            wh = meta[tid]["energy"]
            body = f"{view.at[i, k]:.1f} · " + ("—" if wh is None else f"{wh:.0f} Wh")
            cells.append(f"✓ {body}" if chosen.get(k) == tid else body)
        disp[k] = cells
    return view, disp


def style_matrix(view, disp, columns, meta, chosen):
    cols = [c["key"] for c in columns]

    def paint(_df):
        css = pd.DataFrame("", index=disp.index, columns=disp.columns)
        for i in view.index:
            tid = view.at[i, "_id"]
            m = meta[tid]
            for k in cols:
                if pd.isna(view.at[i, k]):
                    continue
                if chosen.get(k) == tid:
                    css.at[i, k] = CHOSEN_CSS
                elif m["rejects"]:
                    css.at[i, k] = REJECT_CSS
                elif m["delta"] is not None and abs(m["delta"]) > FAR_S:
                    css.at[i, k] = FAR_CSS
        return css

    return disp.style.apply(paint, axis=None)


def resolve_click(cells, view, columns, meta):
    """(trace id, profile_key) from a selection. Never raises, never guesses.

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
    lap = view.at[row, "_id"]
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
# Manage profiles — add, edit, remove. The file work is in profile_manage.py.
# --------------------------------------------------------------------------- #
AFTER_CHANGE = ("Press **Save to constants.py** at the top to put the label and "
                "Wh/lap into the code, then restart the **Pit Web** window. The "
                "car loads the curves at startup, so commit `profiles/`, pull on "
                "the Pi and restart the HUD before sending a new curve to the car.")


WH_HELP = ("Estimated energy per lap on this profile. The Strategy tab uses it "
           "until the car has driven " + str(MIN_LAPS_FOR_MEASURED) + " laps on "
           "the profile, then switches to what the car measured. Leave empty if "
           "unknown.")


def _measured_keys():
    """Keys whose current CSV was built from a real lap, per profiles.json."""
    try:
        with open(SIDECAR_PATH, encoding="utf-8") as fh:
            return set((json.load(fh).get("built") or {}).keys())
    except Exception:
        return set()


def _finish(message):
    """Close the dialog and say what happened at the top of the page."""
    load_installed.clear()
    st.session_state["pm_flash"] = message
    st.rerun()


@st.dialog("Manage profiles", width="large")
def manage_profiles():
    cats = load_sidecar()
    columns = profile_columns(cats)
    targets = {c["key"]: c["target_s"] for c in columns}
    labels = {c["key"]: c["label"] for c in columns}
    measured = _measured_keys()

    def describe(k):
        tag = "measured" if k in measured else "modelled"
        return f"{labels[k]} · {targets[k]:.1f} s · {k} ({tag})"

    st.dataframe(pd.DataFrame([{
        "Key": c["key"], "Label": c["label"], "Target (s)": round(c["target_s"], 1),
        "Curve": "measured lap" if c["key"] in measured else "scaled baseline",
        "Wh/lap estimate": cats[c["key"]].get("energy_wh"),
    } for c in columns]), hide_index=True, width="stretch")
    st.caption("Curves are written to `profiles/` straight away. Labels and "
               "Wh/lap reach `constants.py` only when you press **Save to "
               "constants.py** at the top of the page.")

    add_tab, edit_tab, remove_tab = st.tabs([":material/add: Add",
                                             ":material/edit: Edit",
                                             ":material/delete: Remove"])

    with add_tab:
        a1, a2, a3 = st.columns([2, 1, 1])
        label = a1.text_input("Label", key="pm_add_label",
                              placeholder="e.g. Eco Push")
        target = a2.number_input("Target lap time (s)", pm.MIN_TARGET_S,
                                 pm.MAX_TARGET_S, 205.0, 0.5, key="pm_add_target")
        energy = a3.number_input("Wh per lap", 1.0, 500.0, None, 0.5,
                                 key="pm_add_wh", placeholder="unknown",
                                 help=WH_HELP)
        suggested = pm.suggest_key(label or "profile", target)
        key = st.text_input(
            "Key", key="pm_add_key", placeholder=suggested,
            help="The file name and the name the car is sent. It can never be "
                 "renamed later, because recorded laps point at it. Leave empty "
                 f"to use `{suggested}`.").strip() or suggested
        st.caption("The curve is the baseline lap scaled to this time, the same "
                   "way the original five were made: corners never faster than "
                   "the baseline, braking never harder. Replace it with a real "
                   "lap in the builder whenever you have one."
                   + ("" if energy is not None else
                      " :orange[Without a Wh/lap estimate the Strategy tab leaves "
                      "it out until the car has driven "
                      f"{MIN_LAPS_FOR_MEASURED} laps on it.]"))
        problem = ((None if label.strip() else "enter a label")
                   or pm.key_problem(key, cats.keys())
                   or pm.target_problem(target, targets))
        if problem and label.strip():
            st.error(problem, icon=":material/error:")
        if st.button(f":material/add: Add `{key}`", type="primary",
                     disabled=problem is not None, key="pm_add_go"):
            try:
                with st.spinner("Scaling the baseline…"):
                    info = pm.write_scaled(key, target)
            except Exception as exc:
                st.error(f"Nothing was added: {exc}", icon=":material/error:")
            else:
                cats[key] = {"label": label.strip(), "target_s": round(target, 1),
                             "energy_wh": energy}
                save_sidecar(cats)
                _finish(f"Added **{label.strip()}** as `profiles/{key}.csv` "
                        f"({info['lap_s']:.1f} s, {info['avg_kmh']:.1f} km/h "
                        f"average, {info['max_kmh']:.0f} km/h max). "
                        + AFTER_CHANGE)

    with edit_tab:
        sel = st.selectbox("Profile", [c["key"] for c in columns],
                           format_func=describe, key="pm_edit_sel")
        e1, e2, e3 = st.columns([2, 1, 1])
        new_label = e1.text_input("Label", labels[sel], key=f"pm_edit_label_{sel}")
        new_target = e2.number_input("Target lap time (s)", pm.MIN_TARGET_S,
                                     pm.MAX_TARGET_S,
                                     min(max(float(targets[sel]), pm.MIN_TARGET_S),
                                         pm.MAX_TARGET_S),
                                     0.5, key=f"pm_edit_target_{sel}")
        old_wh = cats[sel].get("energy_wh")
        new_wh = e3.number_input("Wh per lap", 1.0, 500.0, old_wh, 0.5,
                                 key=f"pm_edit_wh_{sel}", placeholder="unknown",
                                 help=WH_HELP)
        retarget = abs(new_target - targets[sel]) >= 0.05
        problem = ((None if new_label.strip() else "enter a label")
                   or (pm.target_problem(new_target,
                                         {k: t for k, t in targets.items() if k != sel})
                       if retarget else None))
        confirmed = True
        if retarget:
            st.info(f"A new target regenerates the curve from the baseline. The "
                    f"key stays `{sel}` even if its name mentions the old time, "
                    f"because the car and recorded laps know it by that name. The "
                    f"old file is copied to `profiles/_backup/`.",
                    icon=":material/info:")
            if sel in measured:
                confirmed = st.checkbox(
                    f"Replace the curve measured from a real lap with a scaled "
                    f"baseline", key=f"pm_edit_confirm_{sel}")
        if problem:
            st.error(problem, icon=":material/error:")
        rewh = (new_wh is None) != (old_wh is None) or (
            new_wh is not None and abs(new_wh - old_wh) >= 0.05)
        changed = retarget or rewh or new_label.strip() != labels[sel]
        if st.button(":material/save: Save", type="primary", key="pm_edit_go",
                     disabled=bool(problem) or not changed or not confirmed):
            info, failed = None, None
            if retarget:
                try:
                    with st.spinner("Scaling the baseline…"):
                        info = pm.write_scaled(sel, new_target)
                except Exception as exc:
                    failed = exc
            if failed is not None:
                st.error(f"Nothing was changed: {failed}", icon=":material/error:")
            else:
                cats[sel]["label"] = new_label.strip()
                cats[sel]["energy_wh"] = new_wh
                if retarget:
                    cats[sel]["target_s"] = round(new_target, 1)
                save_sidecar(cats, drop_built=[sel] if retarget else ())
                _finish(f"Updated `{sel}`"
                        + (f": new curve laps in {info['lap_s']:.1f} s. "
                           if info else ". ")
                        + AFTER_CHANGE)

    with remove_tab:
        removable = [c["key"] for c in columns if c["key"] not in pm.PROTECTED_KEYS]
        st.caption(f"`{DEFAULT_STRATEGY_KEY}` cannot be removed: it is the car's "
                   f"startup profile and the reference every built lap is "
                   f"checked against.")
        if not removable:
            st.info("Nothing to remove.", icon=":material/info:")
        else:
            gone = st.selectbox("Profile", removable, format_func=describe,
                                key="pm_remove_sel")
            st.warning(f"Deletes `profiles/{gone}.csv` (a copy goes to "
                       f"`profiles/_backup/`). A car that still has it loaded keeps "
                       f"driving it until the HUD restarts; after that, sending "
                       f"`{gone}` is refused by the car.",
                       icon=":material/warning:")
            sure = st.checkbox(f"Remove {labels[gone]}", key=f"pm_remove_ok_{gone}")
            if st.button(":material/delete: Remove", type="primary",
                         disabled=not sure, key="pm_remove_go"):
                try:
                    backup = pm.remove_profile(gone)
                except Exception as exc:
                    st.error(f"Nothing was removed: {exc}", icon=":material/error:")
                else:
                    cats.pop(gone, None)
                    save_sidecar(cats, drop_built=[gone])
                    st.session_state.get("pb_chosen", {}).pop(gone, None)
                    if st.session_state.get("pb_focus_key") == gone:
                        st.session_state["pb_focus_key"] = None
                    _finish(f"Removed `{gone}`"
                            + (f" (backup: `{os.path.relpath(backup, _REPO_ROOT)}`). "
                               if backup else ". ")
                            + AFTER_CHANGE)


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
st.title(":material/route: Speed Profile Builder")


def _fmt(x):
    return "—" if x is None else f"{x:g}"


def save_matrix_bar():
    """The Save button: writes the draft matrix into constants.py."""
    saved = load_saved_matrix()
    draft = load_sidecar()
    with st.container(border=True):
        b1, b2 = st.columns([4, 1], vertical_alignment="center")
        diff = [] if saved is None else pm.matrix_diff(saved, draft)
        if saved is None:
            b1.caption("Save is unavailable until constants.py can be read.")
        elif not diff:
            b1.caption(":material/check_circle: Profile matrix saved — "
                       "`constants.py` matches what this page shows.")
        else:
            b1.markdown(f":orange[**{len(diff)} unsaved change(s)** to the profile "
                        f"matrix.] The pit dashboard keeps the old labels and "
                        f"Wh/lap until you save.")
            lines = []
            for kind, key, d in diff:
                if kind == "added":
                    lines.append(f"- **add** `{key}`: {d['label']}, "
                                 f"{_fmt(d['target_s'])} s, {_fmt(d['energy_wh'])} Wh")
                elif kind == "removed":
                    lines.append(f"- **remove** `{key}` ({d['label']})")
                else:
                    lines.append(f"- **change** `{key}`: " + ", ".join(
                        f"{f} {_fmt(a) if f != 'label' else a} → "
                        f"{_fmt(b) if f != 'label' else b}"
                        for f, (a, b) in d.items()))
            with b1.expander("What Save will write"):
                st.markdown("\n".join(lines))
        if b2.button(":material/save: Save to constants.py", type="primary",
                     disabled=not diff, width="stretch", key="pm_save_matrix"):
            try:
                pm.write_saved_matrix(draft)
            except Exception as exc:
                st.error(f"constants.py was not changed: {exc}",
                         icon=":material/error:")
            else:
                save_sidecar(draft)          # stores no edits: draft == saved now
                st.session_state["pm_flash"] = (
                    f"Saved {len(diff)} change(s) to `Pit_Dashboard/constants.py` "
                    f"(old copy in `profiles/_backup/`). Restart the **Pit Web** "
                    f"window to use them, and commit `constants.py` together with "
                    f"`profiles/`.")
                st.rerun()


save_matrix_bar()

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
    # Before the alignment and empty-store stops below, so profiles can be
    # managed on a laptop that has no laps recorded yet.
    if st.button(":material/tune: Manage profiles", key="pm_open"):
        manage_profiles()

_flash = st.session_state.pop("pm_flash", None)
if _flash:
    st.success(_flash, icon=":material/check_circle:")

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
    width="medium",
    help=f"{c['label']} — target {c['target_s']:.0f} s · writes "
         f"profiles/{c['key']}.csv") for c in columns}
cfg["Lap"] = st.column_config.Column(
    width="small",
    help="The car's lap number. Where one number was used by more than one "
         "drive — a counter reset between sessions — the date tells them apart.")

event = st.dataframe(style_matrix(view, disp, columns, meta, chosen),
                     key="pb_matrix", on_select="rerun",
                     selection_mode="single-cell", hide_index=True,
                     column_config=cfg, width="stretch",
                     height=min(38 * len(view) + 45, 520))

st.caption(
    "Each lap sits under the profile its **lap time** is closest to, and each "
    "cell reads `lap time · energy`. The energy is the car's own figure for "
    "that lap, not anything re-derived here. Click any lap to open it below. ✓ marks a lap you have chosen; :orange[amber] means "
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
def energy_panel(tid, m, samples, meta):
    """Where this lap's energy went, and whether that breakdown means anything.

    THE CAR'S NUMBER IS THE TOTAL. The pit integral is here to say WHERE the
    energy went, not how much there was, and the difference is not academic:
    the store holds roughly two rows a second where the car integrates every
    CAN frame, and 34-75% of consecutive rows repeat a held power value. On
    nine real traces the pit read 4-21% high. So the headline figures are the
    car's, the sector split is the pit's, and the line between them says
    whether the two agree closely enough for the split to be believed.
    """
    br = pb.sector_energy(samples, SECTORS)
    # jumps=0: an interleaved trace never reaches this panel, it is refused
    # above with a louder message than "untrusted breakdown".
    trusted, ratio, why = pb.energy_trust(br, m["energy"], jumps=0)

    st.markdown("##### :material/bolt: Energy")
    car_wh = m["energy"]
    dist_km = (m["distance"] or pb.LAP_M) / 1000.0
    e1, e2, e3, e4 = st.columns(4)
    e1.metric("Energy (car)", "—" if car_wh is None else f"{car_wh:.1f} Wh")
    e2.metric("Per km", "—" if car_wh is None else f"{car_wh / dist_km:.1f} Wh/km")
    e3.metric("Average power",
              "—" if (car_wh is None or not m["time"])
              else f"{car_wh * 3600.0 / m['time']:.0f} W")
    e4.metric("Regen recovered", f"{br['regen_wh']:.1f} Wh",
              help="Integrated here from motor power. The car has never "
                   "populated last_lap_regen_energy — it is 0% of every row in "
                   "the store — so there is no figure of its own to show.")

    rank = efficiency_rank(tid, meta)
    if rank:
        st.caption(f"Energy rank **{rank[0]} of {rank[1]}** among the laps "
                   f"under `{m['key']}`, lowest Wh first.")

    if trusted:
        st.caption(
            f":green[Breakdown checked.] Integrated {br['net_wh']:.1f} Wh here "
            f"against the car's {car_wh:.1f} Wh (ratio {ratio:.2f}), covering "
            f"{br['coverage_pct']:.0f}% of the lap. Reading slightly high is "
            f"expected and does not distort the shares — treat the Wh below as "
            f"the SHAPE of the lap, and the car's total as the total.")
    else:
        st.warning("**Breakdown not trusted** — " + "; ".join(why) + ".",
                   icon=":material/warning:")

    st.dataframe(
        pd.DataFrame([{
            "Sector": f"S{r['id']} {r['name']}",
            "Wh": round(r["net_wh"], 1),
            "Wh/km": (round(r["wh_per_km"], 1)
                      if r["wh_per_km"] is not None else None),
            "Share %": (round(r["share_pct"], 1)
                        if r["share_pct"] is not None else None),
            "Regen (Wh)": round(r["regen_wh"], 1),
        } for r in br["sectors"]]),
        hide_index=True, width="stretch",
        column_config={"Share %": st.column_config.ProgressColumn(
            "Share", format="%.0f%%", min_value=0.0, max_value=100.0)})
    st.caption("Sectors are the same nine the pit dashboard and the track map "
               "use. S4 and S6 are barely 100 m long, so at ~0.5 s between "
               "samples they get three or four readings each — read those two "
               "as indicative, not measured.")

    if HAS_PLOTLY and len(br["curve"]) > 1:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=[p[0] for p in br["curve"]],
                                 y=[p[1] for p in br["curve"]],
                                 mode="lines", name="cumulative Wh",
                                 line=dict(color="#FF9900", width=2.5)))
        for sid, _name, a, _b in SECTORS:
            fig.add_vline(x=a, line_width=1, line_dash="dot",
                          line_color="rgba(148,163,184,0.45)")
            fig.add_annotation(x=a, yref="paper", y=1.0, showarrow=False,
                               text=f"S{sid}", xanchor="left", yanchor="bottom",
                               font=dict(size=10, color="#94a3b8"))
        fig.update_layout(height=260, margin=dict(l=0, r=0, t=22, b=0),
                          paper_bgcolor="rgba(0,0,0,0)",
                          plot_bgcolor="rgba(0,0,0,0)",
                          xaxis_title="distance around the lap (m)",
                          yaxis_title="Wh used since the line",
                          showlegend=False)
        st.plotly_chart(fig, width="stretch", config={"displaylogo": False})
        st.caption("Cumulative energy since the finish line. Steep is where the "
                   "lap costs you; flat or falling is regen coming back.")


def lap_detail(tid, key, meta, columns, demo):
    by_key = {c["key"]: c for c in columns}
    m = meta[tid]
    with st.container(border=True):
        h1, h2 = st.columns([8, 1])
        with h2:
            if st.button("Close", key="pb_close", width="stretch"):
                st.session_state["pb_focus"] = None
                st.rerun()
        with h1:
            t = "—" if m["time"] is None else f"{m['time']:.1f} s"
            st.markdown(f"#### Lap {m['lap']} · {t}"
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
            st.markdown(f":orange[Lap {m['lap']} is a {m['time']:.1f} s lap — "
                        f"that puts it under `{m['key']}`, not `{key}`.]")
        word = "slower" if (d or 0) > 0 else "faster"
        line = f"{abs(d):.1f} s {word} than `{key}`'s {target:.0f} s target."
        st.markdown(f":orange[{line}]" if abs(d) > FAR_S else line)

        # Read ONCE, reused by the energy panel, the preview and the write.
        # Deliberately before the build rejections: a lap the trigger cut short
        # still burned energy, and where it went is worth seeing even though
        # that lap can never become a profile.
        samples = (_demo_samples(tid) if demo
                   else load_trace_samples(m["lap"], m["t0"], m["t1"]))

        jumps = pb.count_backward_jumps(samples)
        if jumps >= pb.INTERLEAVE_MIN_JUMPS:
            st.error(
                f"**This is not one drive.** Lap distance falls back {jumps} "
                f"times inside it, which one car cannot do. Two or more copies "
                f"of the car software were publishing at the same time under "
                f"one device id, and their samples are interleaved here — "
                f"nothing about this trace can be trusted, not the speed, not "
                f"the energy, not the lap time.", icon=":material/error:")
            return

        energy_panel(tid, m, samples, meta)

        if m["rejects"]:
            st.error("Cannot be built: " + "; ".join(m["rejects"]),
                     icon=":material/error:")
            return

        st.markdown("##### :material/tune: Build")
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

        m1, m2, m3 = st.columns(3)
        m1.metric("Measured lap", f"{m['time']:.1f} s")
        m2.metric("Samples used", f"{diag['n_used']}")
        m3.metric("Coverage", f"{diag['coverage_pct']:.0f} %")

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
            st.info("  \n".join(f"· {x}" for x in notes), icon=":material/info:")

        if HAS_PLOTLY:
            d_raw, v_raw, _ = pb.clean_samples(samples)
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=list(baseline.distances_m),
                                     y=[sp * 3.6 for sp in baseline.speeds_ms],
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
        if held == tid:
            if st.button(f":material/close: Unchoose lap {m['lap']}",
                         key="pb_unchoose"):
                st.session_state["pb_chosen"].pop(key, None)
                st.rerun()
        else:
            prev = meta.get(held, {}).get("label", held)
            label = (f":material/check: Choose lap {m['lap']} for `{key}`"
                     + (f" (replaces lap {prev})" if held is not None else ""))
            others = [k for k, v in st.session_state["pb_chosen"].items()
                      if v == tid and k != key]
            if others:
                st.warning(f"Lap {m['lap']} is already chosen for "
                           f"`{', '.join(others)}` — choosing it here as well "
                           f"writes the same lap into two files.",
                           icon=":material/warning:")
            if st.button(label, key="pb_choose", type="primary"):
                st.session_state["pb_chosen"][key] = tid
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
    for key, tid in sorted(chosen.items()):
        m = meta[tid]
        samples = load_trace_samples(m["lap"], m["t0"], m["t1"])

        # Re-checked here and not only in the panel. The panel is a UI state a
        # user can have scrolled past; this is the last point before a file the
        # car will drive to gets overwritten.
        jumps = pb.count_backward_jumps(samples)
        if jumps >= pb.INTERLEAVE_MIN_JUMPS:
            failures.append((key, m["label"], [("error",
                f"{jumps} backward distance steps — more than one publisher is "
                f"interleaved in this trace, so its samples are not one lap")]))
            continue

        _src, baseline = installed_profile(key)
        try:
            v_ms, diag = pb.build_profile(
                samples, baseline,
                smooth_points=st.session_state.get(f"smooth_{key}",
                                                   pb.DEFAULT_SMOOTH_POINTS),
                corner_cap=st.session_state.get(f"cap_{key}", True),
                allow_gaps=st.session_state.get(f"gaps_{key}", False))
        except ValueError as exc:
            failures.append((key, m["label"], [("error", str(exc))]))
            continue
        path = os.path.join(PROFILE_DIR, f"{key}.csv") + ".staged"
        pb.write_rows(path, pb.GRID_M.tolist(), v_ms.tolist(), baseline.sections)
        ok, checks = pb.validate_profile(
            path, m["time"],
            os.path.join(PROFILE_DIR, f"{BASELINE_KEY}.csv"))
        if ok:
            staged[key] = (path, tid, diag, checks,
                           pb.sector_energy(samples, SECTORS))
        else:
            failures.append((key, m["label"], checks))

    if failures:
        for path, *_ in staged.values():
            os.remove(path)
        st.error(f"Nothing was written. {len(failures)} profile(s) failed "
                 f"validation:", icon=":material/error:")
        for key, label, checks in failures:
            st.markdown(f"**`{key}` (lap {label})**")
            for level, msg in checks:
                st.caption(f"· {msg}")
        st.caption("Unchoose the failing lap(s) and write again.")
        return

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    replaced, provenance = [], {}
    try:
        for key, (path, tid, diag, _checks, br) in staged.items():
            m = meta[tid]
            final = os.path.join(PROFILE_DIR, f"{key}.csv")
            if os.path.exists(final):
                with open(final, "rb") as a, \
                     open(os.path.join(BACKUP_DIR, f"{key}.{stamp}.csv.bak"),
                          "wb") as b:
                    b.write(a.read())
            os.replace(path, final)
            replaced.append(key)
            trusted, ratio, _why = pb.energy_trust(br, m["energy"])
            provenance[key] = {
                "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
                # WHICH DRIVE, not just which lap number. Two drives can both
                # be lap 7, and a year from now the number alone would not say
                # which one this file came from.
                "trace_lap": int(m["lap"]), "trace_run": int(m["run"]),
                "drive_started": (datetime.datetime.fromtimestamp(m["t0"])
                                  .isoformat(timespec="seconds") if m["t0"] else None),
                "summary_lap": int(m["lap"]) + int(offset),
                "measured_lap_time_s": m["time"],
                "measured_energy_wh": m["energy"],
                "energy_wh_per_km": (m["energy"] / ((m["distance"] or pb.LAP_M) / 1000.0)
                                     if m["energy"] is not None else None),
                "energy_check": {"pit_integral_wh": round(br["net_wh"], 2),
                                 "ratio_to_car": (round(ratio, 3)
                                                  if ratio is not None else None),
                                 "coverage_pct": round(br["coverage_pct"], 1),
                                 "trusted": bool(trusted)},
                "n_samples": diag["n_used"], "max_gap_m": diag["max_gap_m"],
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

    # The lap the curve came from is the best estimate there is of what this
    # profile costs, so it replaces the stored Wh/lap in the draft. It reaches
    # constants.py (and the Strategy tab) on Save, like every other change.
    for key in replaced:
        wh = meta[staged[key][1]]["energy"]
        if wh is not None:
            cats[key]["energy_wh"] = round(float(wh), 1)
    save_sidecar(cats, provenance=provenance)   # one call: it merges `built`
    load_installed.clear()
    # Clear the choices. Leaving them set means the Write button stays armed
    # against files that already hold exactly this, so a stray second click
    # rewrites them and cuts another backup for no change -- and worse, the
    # panel would keep claiming work is outstanding when it is done.
    st.session_state["pb_chosen"] = {}
    st.success(f"Wrote {len(replaced)} profile(s): "
               + ", ".join(f"`{k}.csv`" for k in replaced)
               + ". Their measured Wh/lap is now in the matrix — press **Save to "
               "constants.py** at the top to put it in the code.",
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
            tid = chosen[key]
            m = meta.get(tid, {})
            c1, c2 = st.columns([9, 1])
            d = m.get("delta")
            time_txt = "—" if m.get("time") is None else f"{m['time']:.1f} s"
            delta_txt = "" if d is None else f" (Δ {d:+.1f} s)"
            wh = m.get("energy")
            wh_txt = "" if wh is None else f" · {wh:.0f} Wh"
            replaces = ("  — **replaces the profile the car follows today**"
                        if os.path.exists(os.path.join(PROFILE_DIR, f"{key}.csv"))
                        else "")
            c1.markdown(f"`{key}` ← **lap {m.get('label', tid)}** · {time_txt}"
                        f"{delta_txt}{wh_txt} → `profiles/{key}.csv`{replaces}")
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
