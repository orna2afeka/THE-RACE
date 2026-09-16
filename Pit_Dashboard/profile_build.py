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

# ── Traces, and why a lap number is not an identity ───────────────────────── #
# Mirrors db.TRACE_JOIN_SLACK_S. Kept as a parameter rather than imported so
# this module stays free of the store, the same way write_rows() mirrors
# generate_profiles instead of importing tools/.
DEFAULT_JOIN_SLACK_S = 30.0

# Distance falling by more than this between consecutive samples of one trace.
# Inside a single drive the lap distance only rises, so a real backward step
# means two publishers are interleaved in the store -- three copies of the car
# code were once running at once under one device_id, and the worst affected
# trace has 1160 of these.
INTERLEAVE_BACK_M = 50.0
# One backward step is a lap trigger firing oddly. Three is a second writer.
INTERLEAVE_MIN_JUMPS = 3

# ── Energy ────────────────────────────────────────────────────────────────── #
# The longest interval worth integrating across. This is lap_tracker's own
# MAX_SAMPLE_GAP_S, and matching it matters: measured against the car's figures
# on nine real traces, integrating everything up to 5 s read 18% high, while the
# car's own 2 s rule read 9% high. Mirrored rather than imported -- this module
# must not depend on SolarRace_OS being importable from the pit.
ENERGY_MAX_DT_S = 2.0

# What the pit integral may NOT be used for: the lap's energy. The car's
# last_lap_energy is integrated at CAN frame rate; the store holds roughly two
# rows a second and 34-75% of consecutive rows repeat a held power value, so the
# pit sees a coarser signal and reads 4-21% high on the evidence available. The
# integral's job here is to say WHERE the energy went, not how much there was.
#
# These bounds are set from nine bench traces and should be tightened once laps
# from Zolder exist. They are deliberately wide: a false "untrusted" on every
# lap teaches people to ignore the flag.
ENERGY_RATIO_MIN = 0.80
ENERGY_RATIO_MAX = 1.25
# Below this fraction of the trace's own duration actually integrated, the shape
# has holes in it and the shares are not a distribution of the whole lap.
ENERGY_MIN_COVERAGE_PCT = 90.0


# --------------------------------------------------------------------------- #
# The alignment proof
# --------------------------------------------------------------------------- #
def _pearson(xs, ys):
    """Correlation coefficient, or None when it is undefined."""
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    sx = math.sqrt(sum(v * v for v in dx))
    sy = math.sqrt(sum(v * v for v in dy))
    if sx == 0 or sy == 0:            # a constant series has no correlation
        return None
    return sum(a * b for a, b in zip(dx, dy)) / (sx * sy)


def _score_offsets(series):
    """(offset, detail) from {offset: [(trace_span_s, reported_lap_time_s), ...]}.

    The scoring half of check_lap_alignment, split out so the trace-aware
    version uses exactly the same rule rather than a second copy of it.
    """
    pairs = {}
    for offset, rows in series.items():
        xs = [a for a, _ in rows]
        ys = [b for _, b in rows]
        if xs:
            errs = sorted(abs(a - b) for a, b in rows)
            pairs[offset] = {"corr": _pearson(xs, ys), "n": len(xs),
                             "err": errs[len(errs) // 2]}

    if not pairs:
        return None, "no completed laps to compare"

    detail = " | ".join(
        f"offset {o}: correlation "
        + ("n/a" if v["corr"] is None else f"{v['corr']:+.3f}")
        + f", median |span - lap time| {v['err']:.1f}s over {v['n']} lap(s)"
        for o, v in sorted(pairs.items()))

    if len(pairs) == 2 and all(v["corr"] is not None for v in pairs.values()):
        best = max(pairs, key=lambda o: pairs[o]["corr"])
        other = 1 - best
        b, o = pairs[best]["corr"], pairs[other]["corr"]
        if b > 0.6 and (b - o) > 0.25:
            return best, detail
        return None, (detail + "  -- neither offset tracks the lap times well "
                      "enough to be sure; do not build profiles from this store")

    best = min(pairs, key=lambda o: pairs[o]["err"])
    if len(pairs) == 2:
        other = 1 - best
        if pairs[best]["err"] > 0.5 * pairs[other]["err"]:
            return None, (detail + "  -- the two offsets score too similarly to "
                          "be conclusive; do not build profiles from this store")
    return best, detail


def trace_id(lap, run):
    """The identity of one drive. A string, because it goes into session state,
    into the sidecar's provenance and into a DataFrame cell."""
    return f"L{int(lap)}R{int(run)}"


def pair_traces(traces, join_slack_s=DEFAULT_JOIN_SLACK_S):
    """db.lap_traces() rows -> one dict per drive, joined to its own figures.

    JOINED BY TIME, NOT BY ARITHMETIC. `last_lap_*` describes the lap just
    finished, so a trace's time and energy live on the rows of the NEXT lap --
    but only the next lap OF THE SAME DRIVE. Looking up lap+1 by number is what
    put a lap 4 driven at 17:20 together with a lap 5 driven at 19:18 and
    produced an energy figure 28x out.

    On this project's store the two are not close: joined by time the alignment
    correlates +0.99999 at offset 1, against -0.39 at offset 0.
    """
    by_lap = {}
    for r in traces:
        by_lap.setdefault(int(r["trace_lap"]), []).append(r)
    for runs in by_lap.values():
        runs.sort(key=lambda r: r["t0"])

    out = []
    for r in traces:
        lap = int(r["trace_lap"])
        nxt = None
        for cand in by_lap.get(lap + 1, []):
            # A small negative allowance: the crossing sample can land on either
            # side of the trace's last row when two rows share a timestamp.
            if r["t1"] - 5.0 <= cand["t0"] <= r["t1"] + float(join_slack_s):
                nxt = cand
                break
        out.append({
            "id": trace_id(lap, r["run"]),
            "lap": lap,
            "run": int(r["run"]),
            "t0": float(r["t0"]), "t1": float(r["t1"]),
            "span_s": float(r["t1"]) - float(r["t0"]),
            "n_samples": int(r["n_samples"] or 0),
            "n_speed": int(r["n_speed"] or 0),
            "n_power": int(r["n_power"] or 0),
            "trace_end_m": float(r["trace_end_m"] or 0.0),
            "v_max_kmh": float(r["v_max_kmh"] or 0.0),
            "lap_source": r["lap_source"],
            # This drive's real figures, off the run that follows it.
            "lap_time_s": (float(nxt["carried_lap_time_s"])
                           if nxt and nxt["carried_lap_time_s"] is not None else None),
            "energy_wh": (float(nxt["carried_energy_wh"])
                          if nxt and nxt["carried_energy_wh"] is not None else None),
            "regen_wh": (float(nxt["carried_regen_wh"])
                         if nxt and nxt["carried_regen_wh"] is not None else None),
            "distance_m": (float(nxt["carried_distance_m"])
                           if nxt and nxt["carried_distance_m"] is not None else None),
            # What this drive's OWN rows carry -- the previous lap's figures.
            # Only ever used to score offset 0 in the alignment proof.
            "own_lap_time_s": (float(r["carried_lap_time_s"])
                               if r["carried_lap_time_s"] is not None else None),
            "next_id": trace_id(lap + 1, nxt["run"]) if nxt else None,
        })
    return out


def check_trace_alignment(paired):
    """The alignment proof over drives rather than lap numbers.

    Same correlation rule as check_lap_alignment; the difference is entirely in
    how a trace is matched to a reported lap time. Offset 1 means "the run that
    follows this one in time", not "lap number + 1".
    """
    series = {0: [], 1: []}
    for t in paired:
        if t["span_s"] <= 0:
            continue
        if t["own_lap_time_s"] is not None:
            series[0].append((t["span_s"], t["own_lap_time_s"]))
        if t["lap_time_s"] is not None:
            series[1].append((t["span_s"], t["lap_time_s"]))
    return _score_offsets(series)


def count_backward_jumps(samples, back_m=INTERLEAVE_BACK_M):
    """How many times lap distance falls within one trace. See INTERLEAVE_BACK_M.

    Not the same thing as clean_samples' non-monotonic drop count, which also
    counts a stationary car repeating a distance. This counts only real backward
    steps, which a single publisher cannot produce.
    """
    jumps = 0
    last = None
    for row in samples:
        d = row[1]
        if d is None:
            continue
        d = float(d)
        if last is not None and d < last - float(back_m):
            jumps += 1
        last = d
    return jumps


def sector_of(distance_m, sectors, lap_m=LAP_M):
    """Which sector a distance falls in. `sectors` is [(id, name, start, end)]."""
    d = float(distance_m) % float(lap_m)
    for sid, name, a, b in sectors:
        if a <= d < b:
            return sid
    return sectors[-1][0] if sectors else None


def sector_energy(samples, sectors, max_dt_s=ENERGY_MAX_DT_S, lap_m=LAP_M):
    """Where one trace's energy went. Integrates the car's own signal its way.

    `samples` are db.fetch_trace_samples rows: (ts, distance, speed, source,
    power). Trapezoidal on SIGNED motor power, intervals longer than max_dt_s
    dropped and counted -- lap_tracker.update_energy's exact rule, because the
    figure this is checked against was produced by it.

    Each interval's energy is booked to the sector its MIDPOINT falls in. At
    roughly 0.5 s between rows that is 10-25 m of road, which is fine for the
    600-800 m sectors and coarse for the 100 m ones -- S4 and S6 get three or
    four intervals each, so read those as indicative.
    """
    pts = []
    for row in samples:
        ts, d, p = row[0], row[1], (row[4] if len(row) > 4 else None)
        if ts is None or d is None or p is None:
            continue
        pts.append((float(ts), float(d), float(p)))

    per_sector = {sid: {"net": 0.0, "gross": 0.0, "regen": 0.0}
                  for sid, _n, _a, _b in sectors}
    net = gross = regen = 0.0
    integrated_s = dropped_s = 0.0
    curve = []
    peak_w = peak_regen_w = 0.0

    if pts:
        curve.append((pts[0][1], 0.0))
    for (ta, da, pa), (tb, dbb, pb_) in zip(pts, pts[1:]):
        dt = tb - ta
        if dt <= 0:
            continue
        if dt >= float(max_dt_s):
            # Say so rather than absorb it: a dropout makes the SHAPE wrong,
            # which is the only thing this function is for.
            dropped_s += dt
            curve.append((dbb, net))
            continue
        avg_w = 0.5 * (pa + pb_)
        wh = avg_w * dt / 3600.0
        net += wh
        if wh >= 0:
            gross += wh
        else:
            regen += -wh
        peak_w = max(peak_w, pa, pb_)
        peak_regen_w = min(peak_regen_w, pa, pb_)
        integrated_s += dt

        sid = sector_of((da + dbb) / 2.0, sectors, lap_m)
        if sid in per_sector:
            per_sector[sid]["net"] += wh
            if wh >= 0:
                per_sector[sid]["gross"] += wh
            else:
                per_sector[sid]["regen"] += -wh
        curve.append((dbb, net))

    span_s = (pts[-1][0] - pts[0][0]) if len(pts) > 1 else 0.0
    rows = []
    for sid, name, a, b in sectors:
        length_m = (b - a) if b > a else (lap_m - a + b)
        e = per_sector[sid]
        rows.append({
            "id": sid, "name": name, "start_m": a, "end_m": b,
            "length_m": length_m,
            "net_wh": e["net"], "gross_wh": e["gross"], "regen_wh": e["regen"],
            "wh_per_km": (e["net"] / (length_m / 1000.0)) if length_m else None,
            # Shares of a NEGATIVE total are meaningless, and a lap that
            # recovered more than it spent is a real (if odd) thing to record.
            "share_pct": (100.0 * e["net"] / net) if net > 0 else None,
        })

    return {
        "net_wh": net, "gross_wh": gross, "regen_wh": regen,
        "n_power": len(pts),
        "integrated_s": integrated_s, "dropped_s": dropped_s, "span_s": span_s,
        "coverage_pct": (100.0 * integrated_s / span_s) if span_s > 0 else 0.0,
        "avg_power_w": (net * 3600.0 / integrated_s) if integrated_s > 0 else None,
        "peak_power_w": peak_w if pts else None,
        "peak_regen_w": peak_regen_w if pts else None,
        "sectors": rows,
        "curve": curve,
    }


def energy_trust(breakdown, car_energy_wh, jumps=0):
    """(trusted, ratio, reasons) for one trace's breakdown.

    The self-check the whole feature rests on. It does NOT ask whether the pit
    integral equals the car's number -- it cannot, and pretending otherwise is
    how a plausible wrong breakdown gets believed. It asks whether the integral
    is close enough, and complete enough, for its SHAPE to be a fair
    distribution of the car's total.
    """
    reasons = []
    ratio = None
    if jumps >= INTERLEAVE_MIN_JUMPS:
        reasons.append(f"{jumps} backward distance steps — more than one "
                       f"publisher is interleaved in this trace")
    if not breakdown or breakdown["n_power"] < 2:
        reasons.append("the car reported no motor power for this lap")
        return False, None, reasons
    if breakdown["coverage_pct"] < ENERGY_MIN_COVERAGE_PCT:
        reasons.append(f"only {breakdown['coverage_pct']:.0f}% of the lap was "
                       f"integrated ({breakdown['dropped_s']:.0f}s dropped as "
                       f"gaps longer than {ENERGY_MAX_DT_S:.0f}s)")
    if car_energy_wh is None:
        reasons.append("the car reported no energy for this lap, so there is "
                       "nothing to check the breakdown against")
    elif car_energy_wh > 0:
        ratio = breakdown["net_wh"] / float(car_energy_wh)
        if not (ENERGY_RATIO_MIN <= ratio <= ENERGY_RATIO_MAX):
            reasons.append(f"integrated {breakdown['net_wh']:.1f} Wh against the "
                           f"car's {float(car_energy_wh):.1f} Wh (ratio "
                           f"{ratio:.2f}, outside {ENERGY_RATIO_MIN:.2f}"
                           f"-{ENERGY_RATIO_MAX:.2f})")
    return (not reasons), ratio, reasons


def check_lap_alignment(overview, summary):
    """Work out empirically whether trace N pairs with summary N or N+1.

    `overview` is db.lap_overview() rows, `summary` is db.fetch_lap_summary().
    Returns (offset, detail) where offset is 0 or 1, or (None, detail) when the
    data genuinely cannot settle it.

    SCORED BY CORRELATION, and the two rejected alternatives are worth stating
    because each was tried and each failed on real-shaped data:

      * Lap DISTANCE is useless. Every lap of a circuit is the same length, so
        both offsets score identically and the answer is a coin toss.
      * Median ERROR between a trace's own duration and the reported lap time
        looks right and quietly breaks on a car that is driving WELL. Laps of
        208 +/- 2 s make the two offsets score 0.9 s and 1.8 s -- close enough
        to be called inconclusive, so the better the driving, the more likely
        the tool refuses to run. That is precisely backwards.

    Correlation does not care about the magnitude of the differences, only
    whether the two series move together. At the true offset each trace's span
    IS that lap's time, so they track almost exactly; at the wrong offset a
    trace is being compared against a neighbouring lap, and neighbouring lap
    times are close to independent. It stays decisive on consistent laps, which
    is the case that matters.

    Falls back to median error when correlation is undefined (fewer than three
    laps, or every lap identical to the millisecond), and returns None rather
    than guess when neither can separate them.

    SUPERSEDED by pair_traces() + check_trace_alignment(), which match a trace
    to the run that actually FOLLOWS it instead of to lap number + 1. Kept
    because db.lap_overview() and db.fetch_lap_summary() are still live, and
    anything pairing those two needs this; it shares _score_offsets() with the
    trace version so there is only ever one scoring rule to reason about.

    On a store where lap numbers repeat, this is the weaker proof: against the
    same data the trace version scores offset 1 at +1.000 with a median error of
    0.6 s, while this one compares traces against whichever drive happened to
    reuse the number.
    """
    by_lap = {int(r["lap"]): r for r in summary if r["lap"] is not None}
    traces = [r for r in overview if r["trace_lap"] is not None]

    series = {0: [], 1: []}
    for offset in (0, 1):
        for r in traces:
            s_row = by_lap.get(int(r["trace_lap"]) + offset)
            if not s_row or s_row["lap_time_s"] is None:
                continue
            if not r["t0"] or not r["t1"]:
                continue
            series[offset].append((float(r["t1"]) - float(r["t0"]),
                                   float(s_row["lap_time_s"])))
    return _score_offsets(series)


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
    _crossing_time uses in Pit_Web/api.py.
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

    numpy only. pandas' rolling(win_type=...) pulls in scipy, which is in
    neither Pit_Web/requirements_web.txt nor requirements_profiles.txt and
    would ImportError on the pit laptop.
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


def _synthetic_trace(power_w=1000.0, seconds=200.0, dt=0.5, lap_m=4000.0,
                     gap_at=None, gap_s=3.0, interleave=0):
    """A drive's worth of samples: (ts, distance, speed, source, power).

    Constant power and constant speed on purpose -- the energy is then known in
    closed form, so the test asserts a number rather than whatever the code
    happens to produce.
    """
    v_ms = lap_m / seconds
    rows = []
    t = 0.0
    while t <= seconds + 1e-9:
        rows.append((t, min(v_ms * t, lap_m), v_ms * 3.6, "gps", power_w))
        t += dt
    if gap_at is not None:
        # Drop every sample inside a window, leaving one interval of gap_s.
        rows = [r for r in rows if not (gap_at < r[0] < gap_at + gap_s)]
    for k in range(interleave):
        # A second publisher: same instant, but way back down the lap.
        rows.insert(3 + k * 7, (rows[3 + k * 7][0], 50.0, 30.0, "gps", power_w))
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

    # 8. A lap number used by two different drives pairs each with ITS OWN next
    #    run. This is the bug that put a lap driven at 17:20 together with a lap
    #    time recorded at 19:18 and produced an energy figure 28x out.
    def _row(lap, run, t0, t1, ct=None, ce=None):
        return {"trace_lap": lap, "run": run, "t0": t0, "t1": t1,
                "n_samples": 500, "n_speed": 500, "n_power": 500,
                "trace_end_m": 4000.0, "v_max_kmh": 90.0, "lap_source": "gps",
                "carried_lap_time_s": ct, "carried_energy_wh": ce,
                "carried_regen_wh": None, "carried_distance_m": None}
    morning = [_row(1, 0, 1000.0, 1210.0), _row(2, 0, 1210.0, 1421.0, 210.0, 80.0)]
    evening = [_row(1, 1, 9000.0, 9220.0), _row(2, 1, 9220.0, 9435.0, 220.0, 95.0)]
    paired = pair_traces(morning + evening)
    by_id = {t["id"]: t for t in paired}
    got_m = by_id["L1R0"]["lap_time_s"], by_id["L1R0"]["energy_wh"]
    got_e = by_id["L1R1"]["lap_time_s"], by_id["L1R1"]["energy_wh"]
    print(f"8. two drives, one lap number -> morning {got_m}, evening {got_e}")
    if got_m != (210.0, 80.0) or got_e != (220.0, 95.0):
        print("   ** FAIL ** a trace took the other drive's figures")
        ok = False

    # 9. The alignment proof still picks offset 1, on drives.
    off, det = check_trace_alignment(paired)
    print(f"9. trace alignment -> offset {off}")
    ok &= off == 1

    # 10. Interleaved publishers are detected, clean traces are not accused.
    clean_jumps = count_backward_jumps(_synthetic_trace())
    dirty_jumps = count_backward_jumps(_synthetic_trace(interleave=5))
    print(f"10. backward steps -> clean {clean_jumps}, interleaved {dirty_jumps}")
    ok &= clean_jumps == 0 and dirty_jumps >= INTERLEAVE_MIN_JUMPS

    # 11. Energy is the closed-form answer, and the sectors add back up to it.
    sectors = [(1, "A", 0.0, 1000.0), (2, "B", 1000.0, 2000.0),
               (3, "C", 2000.0, 3000.0), (4, "D", 3000.0, 4000.0)]
    br = sector_energy(_synthetic_trace(power_w=1000.0, seconds=200.0), sectors)
    want = 1000.0 * 200.0 / 3600.0
    sums = sum(r["net_wh"] for r in br["sectors"])
    print(f"11. constant 1000 W for 200 s -> {br['net_wh']:.2f} Wh "
          f"(exact {want:.2f}), sectors sum {sums:.2f}, coverage "
          f"{br['coverage_pct']:.0f}%")
    ok &= abs(br["net_wh"] - want) < 0.05
    ok &= abs(sums - br["net_wh"]) < 1e-6
    # Equal sectors on a constant-speed lap: each quarter takes a quarter.
    ok &= all(abs(r["net_wh"] - want / 4.0) < 0.2 for r in br["sectors"])

    # 12. A dropout is refused and counted, not absorbed into the total.
    holed = sector_energy(_synthetic_trace(gap_at=100.0, gap_s=3.0), sectors)
    print(f"12. 3 s dropout -> {holed['dropped_s']:.1f}s dropped, coverage "
          f"{holed['coverage_pct']:.0f}%, {holed['net_wh']:.2f} Wh")
    ok &= holed["dropped_s"] >= 3.0
    ok &= holed["coverage_pct"] < 100.0
    ok &= holed["net_wh"] < br["net_wh"]

    # 13. Regen is separated from consumption rather than netted away silently.
    regen_rows = [(t, d, v, s_, (-2000.0 if 50.0 < t < 100.0 else 1000.0))
                  for t, d, v, s_, _p in _synthetic_trace()]
    rb = sector_energy(regen_rows, sectors)
    print(f"13. with regen -> net {rb['net_wh']:.1f} Wh, gross {rb['gross_wh']:.1f}, "
          f"regen {rb['regen_wh']:.1f}, peak regen {rb['peak_regen_w']:.0f} W")
    ok &= rb["regen_wh"] > 25.0 and rb["net_wh"] < rb["gross_wh"]

    # 14. The trust rule: what it accepts, and every way it refuses.
    good = energy_trust(br, br["net_wh"] / 1.08)          # the real-world bias
    bad_ratio = energy_trust(br, br["net_wh"] / 2.0)
    # A 3 s dropout costs 1.5% of a 200 s lap, which is CORRECTLY still trusted
    # -- so the coverage refusal has to be provoked with a real hole.
    gappy = sector_energy(_synthetic_trace(gap_at=50.0, gap_s=40.0), sectors)
    bad_cover = energy_trust(gappy, gappy["net_wh"])
    bad_mixed = energy_trust(br, br["net_wh"], jumps=INTERLEAVE_MIN_JUMPS)
    no_car = energy_trust(br, None)
    no_power = energy_trust(sector_energy([], sectors), 100.0)
    print(f"14. trust -> ratio 1.08 {good[0]}, ratio 2.0 {bad_ratio[0]}, "
          f"low coverage {bad_cover[0]}, interleaved {bad_mixed[0]}, "
          f"no car figure {no_car[0]}, no power {no_power[0]}")
    ok &= good[0] is True
    ok &= not any(x[0] for x in (bad_ratio, bad_cover, bad_mixed, no_car, no_power))

    print("\nSELF-CHECK", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_self_check())
