"""
profile_build.py — turn one measured lap into a speed profile
==============================================================
The arithmetic behind profile_builder.py, kept in its own module with NO
Streamlit in it so every step can be exercised headlessly:

    python Pit_Dashboard/profile_build.py        # self-check on synthetic laps

WHAT THIS IS FOR
The five profiles/*.csv the car follows are synthetic: tools/generate_profiles.py
scales one modelled lap (Pit_Dashboard/210s.xlsx) to five target times. Nobody
has ever driven them. This turns a lap the car really drove into the same file
format, so the target the driver chases is a lap that actually happened.

THE ONE THING THAT WILL BITE WHOEVER READS THIS NEXT
`calculated_lap` is the number of laps COMPLETED, so the samples tagged with it
are the lap being driven NEXT, while last_lap_time_s on those same rows is the
lap just FINISHED. Trace N pairs with summary N+1. check_lap_alignment() proves
that against live data rather than trusting this paragraph, because the failure
mode is silent: every profile filed under a neighbouring lap's time, and nothing
on screen looks wrong.

HONESTY, WHICH IS THE WHOLE POINT OF THE THING
The car reports roughly once a second. At racing speed that is 15-25 m between
samples, and the profile grid is 10 m — so a measured profile is INTERPOLATED UP
from coarser data, and a corner apex the car never sampled reads faster than the
speed it actually carried. Telling a driver to take a corner faster than the car
has been shown to take it is the one genuinely dangerous thing this file can do,
so apply_corner_cap() exists and the caller is expected to leave it on.
"""

import csv
import math
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import speed_profile  # noqa: E402  (path set up immediately above)
import track          # noqa: E402

# ── The grid ──────────────────────────────────────────────────────────────── #
# 0..4010 every 10 m: 402 points, byte-for-byte the same axis the existing five
# files use. The last two rows overshoot the 4000 m lap on purpose — they are the
# lap wrapping round, and base_210s.csv's own values confirm it (d=4000 repeats
# d=0's speed to three decimals). Matching the axis exactly is what lets a
# generated profile inherit the baseline's section labels index-for-index
# instead of trying to classify corners from a noisy measurement.
GRID_M = np.arange(0, 4011, 10, dtype=float)
LAP_M = float(track.TRACK_LENGTH_METERS)

# A hole this big is a dropout, not sampling. Two missed samples at racing speed.
MAX_GAP_M = 40.0
# How far a lap's measured length may sit from 4000 m before it is not a lap.
MAX_LENGTH_ERROR_M = 40.0
# Below this the car was parked, not driving.
MIN_MEDIAN_KMH = 5.0
# Rows written before the speed decode fix are ~50x too high (see db.py).
LEGACY_SPEED_KMH = 200.0
# Floor, shared with the synthetic generator so both kinds of profile agree.
MIN_SPEED_MS = 2.0

DEFAULT_SMOOTH_POINTS = 5          # 5 x 10 m = a 50 m window


# --------------------------------------------------------------------------- #
# The alignment proof
# --------------------------------------------------------------------------- #
def check_lap_alignment(overview, summary):
    """Work out empirically whether trace N pairs with summary N or N+1.

    `overview` is db.lap_overview() rows, `summary` is db.fetch_lap_summary().
    Returns (offset, detail) where offset is 0 or 1, or (None, detail) when the
    data cannot settle it.

    SCORED ON LAP TIME, NOT LAP DISTANCE, and that choice is load-bearing.
    Distance looks like the obvious discriminator and is nearly useless for it:
    every lap of a circuit is the same length, so on real racing data both
    offsets score about equally and the answer is a coin toss. Lap TIMES
    genuinely differ lap to lap -- traffic, driver, strategy -- so comparing
    each trace's own wall-clock span against the car's reported lap time picks
    the pairing out cleanly.

    (This was found by testing, not by reasoning: a synthetic store with four
    equal-length laps scored 10 m for both offsets and reported itself
    inconclusive, which is exactly the situation Zolder will produce.)

    Distance is still computed and reported as a secondary signal, because when
    the two disagree that is worth seeing.
    """
    by_lap = {int(r["lap"]): r for r in summary if r["lap"] is not None}
    traces = [r for r in overview if r["trace_lap"] is not None]

    def score(field, get_trace, get_summary):
        out = {}
        for offset in (0, 1):
            errs = []
            for r in traces:
                s_row = by_lap.get(int(r["trace_lap"]) + offset)
                a, b = get_trace(r), (get_summary(s_row) if s_row else None)
                if a is not None and b is not None and b > 0:
                    errs.append(abs(float(a) - float(b)))
            if errs:
                out[offset] = (float(np.median(errs)), len(errs))
        return out

    t_scores = score("time",
                     lambda r: (r["t1"] - r["t0"]) if (r["t0"] and r["t1"]) else None,
                     lambda s: s["lap_time_s"])
    d_scores = score("dist", lambda r: r["trace_end_m"], lambda s: s["distance_m"])

    parts = []
    if t_scores:
        parts.append("lap time: " + ", ".join(
            f"offset {o} median |trace span - lap time| = {v:.1f}s ({n} lap(s))"
            for o, (v, n) in sorted(t_scores.items())))
    if d_scores:
        parts.append("distance: " + ", ".join(
            f"offset {o} = {v:.0f}m" for o, (v, _n) in sorted(d_scores.items())))
    detail = " | ".join(parts) or "no completed laps to compare"

    scores = t_scores or d_scores
    if not scores:
        return None, detail
    best = min(scores, key=lambda o: scores[o][0])
    if len(scores) == 2:
        other = 1 - best
        if scores[best][0] > 0.5 * scores[other][0]:
            return None, (detail + "  -- the two offsets score too similarly to "
                          "be conclusive; do not build profiles from this store")
    return best, detail


# --------------------------------------------------------------------------- #
# Cleaning one lap's samples
# --------------------------------------------------------------------------- #
def clean_samples(samples):
    """(distance_m, speed_kmh, diagnostics) from raw per-lap rows.

    `samples` is an iterable of (device_ts, lap_distance_m, speed_kmh, ...) —
    db.fetch_lap_profile_samples rows or plain tuples.

    Drops rows with no speed, then keeps only STRICTLY increasing distance. A
    stationary car repeats the same distance, and the last of a repeated run is
    the sample motion resumed from, which is the same straddle rule
    _crossing_time uses in pit_dashboard.
    """
    d_raw, v_raw = [], []
    no_speed = 0
    for row in samples:
        d = row[1]
        v = row[2]
        if d is None:
            continue
        if v is None:
            no_speed += 1
            continue
        d_raw.append(float(d))
        v_raw.append(abs(float(v)))       # the controller's field can be signed

    kept_d, kept_v, dropped = [], [], 0
    for i, (d, v) in enumerate(zip(d_raw, v_raw)):
        if kept_d and d <= kept_d[-1]:
            # Same or backwards: replace, so a stationary run keeps its last.
            if d == kept_d[-1]:
                kept_v[-1] = v
            dropped += 1
            continue
        kept_d.append(d)
        kept_v.append(v)

    d = np.array(kept_d, dtype=float)
    v = np.array(kept_v, dtype=float)
    gaps = np.diff(d) if len(d) > 1 else np.array([])
    holes = [(float(d[i]), float(d[i + 1]))
             for i, g in enumerate(gaps) if g > MAX_GAP_M]
    covered = float(d[-1] - d[0]) - sum(b - a for a, b in holes) if len(d) > 1 else 0.0

    diag = {
        "n_used": int(len(d)),
        "n_no_speed": no_speed,
        "n_dropped_nonmonotonic": dropped,
        "length_m": float(d[-1]) if len(d) else 0.0,
        "max_gap_m": float(gaps.max()) if len(gaps) else 0.0,
        "mean_spacing_m": float(gaps.mean()) if len(gaps) else 0.0,
        "holes": holes,
        "coverage_pct": 100.0 * covered / LAP_M if len(d) > 1 else 0.0,
        "median_kmh": float(np.median(v)) if len(v) else 0.0,
        "max_kmh": float(v.max()) if len(v) else 0.0,
    }
    return d, v, diag


def reject_reasons(diag, allow_gaps=False):
    """Why this lap should not become a profile. Empty list = usable."""
    out = []
    if diag["n_used"] < 100:
        out.append(f"only {diag['n_used']} usable samples")
    if diag["max_kmh"] > LEGACY_SPEED_KMH:
        out.append(f"speeds up to {diag['max_kmh']:.0f} km/h — pre-decode-fix "
                   f"rows, ~50x too high, not rescalable")
    if diag["median_kmh"] < MIN_MEDIAN_KMH:
        out.append(f"median speed {diag['median_kmh']:.1f} km/h — car was parked")
    if abs(diag["length_m"] - LAP_M) > MAX_LENGTH_ERROR_M:
        out.append(f"lap measured {diag['length_m']:.0f} m, not ~{LAP_M:.0f} m — "
                   f"the lap trigger fired early or late")
    if diag["holes"] and not allow_gaps:
        worst = max(b - a for a, b in diag["holes"])
        out.append(f"{len(diag['holes'])} gap(s), worst {worst:.0f} m — "
                   f"telemetry dropped and nothing back-fills it")
    return out


# --------------------------------------------------------------------------- #
# Measurement -> profile
# --------------------------------------------------------------------------- #
def resample(d, v_kmh):
    """Measured (distance, speed) onto GRID_M. Returns (v_kmh_on_grid, measured).

    `measured` marks which grid points sit inside a real sampled interval, so a
    hole can never be quietly presented as data. The wrap (`% LAP_M`) is what
    produces the 4000 and 4010 rows from the values at 0 and 10.
    """
    if len(d) < 2:
        raise ValueError("need at least two samples to resample a lap")
    x = GRID_M % LAP_M
    out = np.interp(x, d, v_kmh)

    measured = np.ones_like(out, dtype=bool)
    gaps = np.diff(d)
    for i, g in enumerate(gaps):
        if g > MAX_GAP_M:
            measured &= ~((x > d[i]) & (x < d[i + 1]))
    # Outside the sampled span entirely (the lap started late / ended early).
    measured &= (x >= d[0]) & (x <= d[-1])
    return out, measured


def smooth(v_kmh, window_points=DEFAULT_SMOOTH_POINTS):
    """Hann-window smoothing along the lap, WRAPPED at the finish line.

    Wrapped, not edge-padded: the start/finish straight is one continuous piece
    of road, and edge padding would flatten the fastest part of the lap at
    exactly the point look_ahead() most needs to be right.

    numpy only. pandas' rolling(win_type=...) pulls in scipy, which is not in
    requirements_pit.txt and would ImportError on the pit laptop.
    """
    w = int(window_points)
    if w <= 1:
        return v_kmh.copy()
    if w % 2 == 0:
        w += 1
    kernel = np.hanning(w + 2)[1:-1]
    kernel /= kernel.sum()

    per = v_kmh[:400]                      # 0..3990 is the periodic part
    half = w // 2
    padded = np.concatenate([per[-half:], per, per[:half]])
    sm = np.convolve(padded, kernel, mode="valid")
    return np.concatenate([sm, sm[:2]])    # rebuild 4000, 4010 from 0, 10


def apply_corner_cap(v_kmh, baseline_kmh, sections):
    """Never ask for more speed through a corner than the baseline allows.

    THE reason this module is safe to point at a driver. A corner apex three
    samples wide is easy to miss entirely at ~1 Hz, and the interpolation across
    the miss reads FASTER than the car actually went. Capped against the profile
    the car has been following, an under-sampled apex can only ever produce a
    target that is too slow, which costs lap time; uncapped it produces one that
    is too fast, which costs the car.

    Same rule as tools/generate_profiles.scale_profile.
    """
    out = v_kmh.copy()
    capped = []
    for i, sec in enumerate(sections):
        if str(sec).strip().lower().startswith("turn") and out[i] > baseline_kmh[i]:
            capped.append(float(GRID_M[i]))
            out[i] = baseline_kmh[i]
    return out, capped


def apply_floor(v_kmh):
    """Clamp to the shared minimum. Returns (speeds, clamped_distances)."""
    floor_kmh = MIN_SPEED_MS * 3.6
    clamped = [float(GRID_M[i]) for i, v in enumerate(v_kmh) if v < floor_kmh]
    return np.maximum(v_kmh, floor_kmh), clamped


def fill_holes(v_kmh, measured, baseline_kmh):
    """Fill unmeasured grid points from the installed profile, offset to meet the
    measurement at both edges of the hole.

    Only ever called when a human has explicitly accepted a lap with a gap. A
    straight line across the hole would invent a constant-speed section the car
    never drove; borrowing the baseline's SHAPE and shifting it to match the
    measured speeds either side keeps the corners in the hole looking like
    corners. The caller records the filled ranges so the file's provenance says
    which parts of it were never measured.
    """
    out = v_kmh.copy()
    if measured.all():
        return out, []
    filled = []
    i = 0
    n = len(out)
    while i < n:
        if measured[i]:
            i += 1
            continue
        j = i
        while j < n and not measured[j]:
            j += 1
        lo, hi = i - 1, j            # nearest measured points either side
        shift_lo = (out[lo] - baseline_kmh[lo]) if lo >= 0 else 0.0
        shift_hi = (out[hi] - baseline_kmh[hi]) if hi < n else shift_lo
        span = max(1, j - i + 1)
        for k in range(i, j):
            t = (k - i + 1) / span
            out[k] = baseline_kmh[k] + shift_lo * (1 - t) + shift_hi * t
        filled.append((float(GRID_M[i]), float(GRID_M[min(j, n - 1)])))
        i = j
    return out, filled


def build_profile(samples, baseline, smooth_points=DEFAULT_SMOOTH_POINTS,
                  corner_cap=True, allow_gaps=False):
    """One measured lap -> (speeds_ms on GRID_M, diagnostics).

    `baseline` is a speed_profile.SpeedProfile on the SAME grid — the currently
    installed profile for this key, used for the corner cap, for hole filling
    and for its section labels. Raises ValueError with the reasons when the lap
    is not usable.
    """
    d, v, diag = clean_samples(samples)
    reasons = reject_reasons(diag, allow_gaps=allow_gaps)
    if reasons:
        raise ValueError("; ".join(reasons))

    base_kmh = np.array([s * 3.6 for s in baseline.speeds_ms], dtype=float)
    if len(base_kmh) != len(GRID_M):
        raise ValueError(f"baseline has {len(base_kmh)} points, expected "
                         f"{len(GRID_M)} — it is not on the standard grid")

    v_grid, measured = resample(d, v)
    v_grid, filled = fill_holes(v_grid, measured, base_kmh)
    v_grid = smooth(v_grid, smooth_points)
    capped = []
    if corner_cap:
        v_grid, capped = apply_corner_cap(v_grid, base_kmh, baseline.sections)
    v_grid, clamped = apply_floor(v_grid)

    diag.update({
        "filled_ranges": filled,
        "capped_points": capped,
        "clamped_points": clamped,
        "smoothing_window_m": (int(smooth_points) if smooth_points > 1 else 0) * 10,
        "corner_cap": bool(corner_cap),
        "unmeasured_points": int((~measured).sum()),
    })
    return v_grid / 3.6, diag


# --------------------------------------------------------------------------- #
# Validation — read the file back exactly the way the car will
# --------------------------------------------------------------------------- #
def validate_profile(path, measured_lap_time_s, baseline_path):
    """Checks on a WRITTEN profile. Returns (ok, [(level, message), ...]).

    Reloaded through speed_profile.load_csv rather than trusting what we just
    wrote, because that is the loader the car uses — and it SKIPS malformed rows
    rather than raising, so a truncated file loads as a silently-short profile.
    Counting the points is what catches that.
    """
    notes = []
    ok = True
    p = speed_profile.load_csv(path, lap_length_m=LAP_M)
    base = speed_profile.load_csv(baseline_path, lap_length_m=LAP_M)

    if len(p) != len(GRID_M):
        return False, [("error", f"{len(p)} points, expected {len(GRID_M)} — "
                                 f"the file is truncated or malformed")]
    notes.append(("ok", f"{len(p)} points on the standard 10 m grid"))

    integrated = p.lap_time_s()
    if measured_lap_time_s:
        err = integrated - float(measured_lap_time_s)
        # Not an equality test on purpose: lap_time_s integrates ds/v over the
        # smoothed grid, while the measured time is wall-clock between two lap
        # triggers over a distance that is not exactly 4000 m.
        level = "ok" if abs(err) <= 2.0 else ("warn" if abs(err) <= 10.0 else "error")
        ok &= level != "error"
        notes.append((level, f"integrated lap {integrated:.1f}s vs measured "
                             f"{float(measured_lap_time_s):.1f}s ({err:+.1f}s)"))
    else:
        notes.append(("ok", f"integrated lap {integrated:.1f}s"))

    if min(p.speeds_ms) < MIN_SPEED_MS - 1e-6:
        ok = False
        notes.append(("error", f"minimum {min(p.speeds_ms):.2f} m/s is below the "
                               f"{MIN_SPEED_MS} m/s floor"))

    # Corner alerts: a jittery profile fires look_ahead constantly. Comparing the
    # count against the baseline is a smoothness test built from existing code.
    def alerts(prof):
        return sum(1 for d in range(0, int(LAP_M), 10)
                   if prof.look_ahead(float(d), 175.0, 15.0))
    a_new, a_base = alerts(p), alerts(base)
    level = "ok" if a_new <= max(3 * a_base, a_base + 10) else "warn"
    notes.append((level, f"{a_new} corner alerts vs {a_base} in the baseline"))

    return ok, notes


def write_rows(path, distances_m, speeds_ms, sections):
    """The 6-column format the car reads, with Time(s) and a(m/s^2) re-derived.

    Mirrors tools/generate_profiles.write_profile so both kinds of profile are
    byte-compatible; kept here rather than imported so the builder never depends
    on tools/ being importable from Pit_Dashboard/.
    """
    n = len(distances_m)
    t = 0.0
    rows = []
    for i in range(n):
        v = speeds_ms[i]
        if i:
            ds = distances_m[i] - distances_m[i - 1]
            v_prev = speeds_ms[i - 1]
            t += ds / max(1e-6, 0.5 * (v + v_prev))
            a = (v * v - v_prev * v_prev) / (2.0 * ds) if ds else 0.0
        else:
            a = 0.0
        rows.append([sections[i] if i < len(sections) else "Straight",
                     int(round(distances_m[i])), round(v, 6), round(v * 3.6, 3),
                     round(a, 6), round(t, 6)])
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["section", "d(m)", "V(m/s)", "V(km/h)", "a(m/s^2)", "Time(s)"])
        w.writerows(rows)
    return t


# --------------------------------------------------------------------------- #
# Self-check
# --------------------------------------------------------------------------- #
def _synthetic_lap(lap_time_s=210.0, spacing_m=20.0, hole=None, stationary=False,
                   length_m=4000.0, jitter=0.0, seed=1):
    """A fake lap shaped like a real one: fast straights, four slow corners."""
    rng = np.random.default_rng(seed)
    d = np.arange(0.0, length_m, spacing_m)
    base = 90.0 - 55.0 * np.exp(-((d % 1000 - 650) / 90.0) ** 2)
    v = base * (210.0 / lap_time_s)
    if jitter:
        v = v + rng.normal(0.0, jitter, size=len(v))
    rows = [(float(i), float(dd), float(vv), "gps") for i, (dd, vv) in enumerate(zip(d, v))]
    if stationary:
        rows = rows[:50] + [(0.0, rows[50][1], 0.0, "gps")] * 8 + rows[50:]
    if hole:
        lo, hi = hole
        rows = [r for r in rows if not (lo < r[1] < hi)]
    return rows


def _self_check():
    base_path = os.path.join(_REPO_ROOT, "profiles", "base_210s.csv")
    baseline = speed_profile.load_csv(base_path, lap_length_m=LAP_M)
    print(f"baseline: {len(baseline)} points, "
          f"integrated lap {baseline.lap_time_s():.1f}s")

    ok = True

    # 1. A clean lap builds, and lands near its own lap time.
    v_ms, diag = build_profile(_synthetic_lap(210.0, jitter=1.5), baseline)
    got = speed_profile.SpeedProfile("t", GRID_M.tolist(), v_ms.tolist(),
                                     baseline.sections, lap_length_m=LAP_M)
    print(f"1. clean lap      -> {len(v_ms)} pts, integrated {got.lap_time_s():.1f}s, "
          f"coverage {diag['coverage_pct']:.0f}%, capped {len(diag['capped_points'])}")
    ok &= len(v_ms) == 402

    # 2. A stationary run is deduped, not averaged in.
    _, diag2 = build_profile(_synthetic_lap(stationary=True), baseline)
    print(f"2. stationary run -> dropped {diag2['n_dropped_nonmonotonic']} "
          f"non-monotonic sample(s)")
    ok &= diag2["n_dropped_nonmonotonic"] > 0

    # 3. A hole is refused by default and marked when accepted.
    holed = _synthetic_lap(hole=(1500.0, 1800.0))
    try:
        build_profile(holed, baseline)
        print("3. hole           -> NOT REJECTED  ** FAIL **")
        ok = False
    except ValueError as exc:
        print(f"3. hole           -> rejected: {exc}")
    _, diag3 = build_profile(holed, baseline, allow_gaps=True)
    print(f"   accepted anyway -> filled {diag3['filled_ranges']}")
    ok &= bool(diag3["filled_ranges"])

    # 4. A short/long lap is refused.
    try:
        build_profile(_synthetic_lap(length_m=4400.0), baseline)
        print("4. 4400 m lap     -> NOT REJECTED  ** FAIL **")
        ok = False
    except ValueError as exc:
        print(f"4. 4400 m lap     -> rejected: {exc}")

    # 5. Legacy 50x speeds are refused rather than rescaled.
    legacy = [(t, d, v * 50.0, s) for t, d, v, s in _synthetic_lap()]
    try:
        build_profile(legacy, baseline)
        print("5. legacy speeds  -> NOT REJECTED  ** FAIL **")
        ok = False
    except ValueError as exc:
        print(f"5. legacy speeds  -> rejected: {exc}")

    # 6. The corner cap really binds.
    fast = _synthetic_lap(150.0)
    _, diag6 = build_profile(fast, baseline, corner_cap=True)
    print(f"6. corner cap     -> capped {len(diag6['capped_points'])} turn point(s)")
    ok &= len(diag6["capped_points"]) > 0

    # 7. Round trip through the real writer + the car's own loader.
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "_profile_selfcheck.csv")
    write_rows(tmp, GRID_M.tolist(), v_ms.tolist(), baseline.sections)
    good, notes = validate_profile(tmp, 210.0, base_path)
    print(f"7. write+validate -> ok={good}")
    for level, msg in notes:
        print(f"     [{level}] {msg}")
    os.remove(tmp)
    ok &= good

    print("\nSELF-CHECK", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_self_check())
