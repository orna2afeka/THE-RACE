"""
build_dor_profiles.py — speed profiles from a lap the car actually drove
========================================================================
Replaces tools/generate_profiles.py's modelled baseline with a real one.

    python tools/build_dor_profiles.py            # write profiles/
    python tools/build_dor_profiles.py --verify   # write + check
    python tools/build_dor_profiles.py --dry-run  # print, write nothing

WHY THIS EXISTS
Pit_Dashboard/210s.xlsx is a desk model. It commands 92 km/h down the main
straight and a 210 s lap; the car's own log of lap 17 at Zolder says 50.4 km/h
average and 289.7 s. Every profile generated from that model told the driver to
do something the car cannot do, all lap, every lap — so the HUD's target was
noise and the Δ beside it was worse than nothing.

SolarRace_OS/dor 17.xlsx is one real lap, logged on the car on 2026-09-18, in
NORMAL MODE throughout. It is the ground truth here: corner speeds, the shape of
every straight and the braking points are taken from it and not adjusted.

THE THREE THINGS DONE TO IT, AND WHY EACH IS NECESSARY
1. THE AXIS IS THE CAR'S OWN ODOMETER (--axis, default `odometer`).
   The log measures the lap as 4060 m against a 4000 m circuit. track.py:63
   already names the cause: drivetrain.TIRE_DIAMETER_METERS has never been
   measured, and a 1.5 % tyre error is a 1.5 % distance error on every lap.

   That matters because of what the car does at runtime. LapTracker.
   profile_distance_m() hands speed_profile the RAW odometer lap distance, so
   the number indexing the curve is the inflated one. Writing the curve in true
   track metres would therefore put it out of phase by however far the odometer
   has over-read — nothing at the line, 60 m by the end of the lap — and the
   error lands at its worst exactly where it is least affordable: at the T15/16
   chicane the driver would be shown 46 km/h for a corner taken at 26.

   So the curve is indexed by odometer metres, the same units the lookup uses,
   and the two stay in phase all the way round. The cost is the last 60 m of the
   car's lap, which wraps onto the profile's first 60 m — both are the start/
   finish straight at within a few km/h of each other, so the wrap is invisible.

   --axis track instead rescales into surveyed track metres. Use it once the
   tyre constant has been measured and the odometer reads true; the profiles
   and the odometer have to agree, and it is the odometer that is wrong today.
   Either way --verify prints each corner against strategy_engine.
   TURN_START_TRACK_M, carried into whichever axis is in use.

2. THE LAST 230 m ARE REBUILT. From the T15/16 chicane apex the log shows the
   car crawling at 22-27 km/h for 310 m — 42 s, 15 % of the lap — under power,
   and the lap ENDS at 22.5 km/h having STARTED at 60.2. That is the in-lap to a
   stop, not a racing lap, and a profile has to close: a curve that ends at 22
   and begins at 60 tells the driver to brake to walking pace before the line
   every lap and then be at 60 again by magic. So from the chicane apex to the
   line the curve is rebuilt as an acceleration at the lap's OWN demonstrated
   acceleration limit, capped at the speed the lap starts with.
   The chicane itself — the braking and the apex — is kept exactly as driven.

3. CORNERS ARE FOUND IN THE TRACE, NOT ASSUMED. generate_profiles.py took its
   Turn labels from a column somebody curated by hand. There is no such column
   in a car log, so corners are the prominent speed minima: a dip worth at least
   PROMINENCE_KMH against the faster of its two shoulders. That finds where this
   car, with this driver, was actually grip-limited — which is the only thing
   that must not be scaled. Turns the driver took near flat (T3, T4, T7, T11 on
   this lap) stay Straight on purpose: there is time there and a strategy is
   allowed to ask for it.
   TURN_START_TRACK_M is used only to NAME each corner and to prove the
   detection landed on the real turns. --verify prints every pairing.

WHAT IS THEN VARIED
Nothing in the corners. Straights are scaled by a factor k solved per target
lap time, and the braking and acceleration zones are rebuilt from the lap's own
peak rates, by tools/generate_profiles.py's solver — this module deliberately
imports that code rather than growing a second copy of it.
"""

import argparse
import bisect
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for _p in (_REPO, os.path.join(_REPO, "Pit_Dashboard")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import speed_profile                                        # noqa: E402
import track                                                # noqa: E402
import generate_profiles as gen                             # noqa: E402
from strategy_engine import TURN_START_TRACK_M              # noqa: E402

# lap 33 of the 2026-09-19 race is what profiles/ is built from today. The
# workbook was arranged by the crew so that row 1 is the start/finish line, and
# the lap is stretched onto the surveyed 4000 m -- so BOTH flags are needed:
#     python tools/build_dor_profiles.py --as-driven lap33_290s --file-order --axis track --verify
# "dor 17.xlsx" (the practice lap the dor_* ladder came from) is still beside
# it; pass --source to build from that, or from any later lap export.
SOURCE_XLSX = os.path.join(_REPO, "SolarRace_OS", "lap 33.xlsx")
OUT_DIR = os.path.join(_REPO, "profiles")

# Column headings in the car's export, matched case-insensitively on a prefix
# so a later export that renames "(km/h)" to "(kph)" still loads.
# In a hand-arranged workbook, a backwards jump bigger than this is the paste
# seam, not the car reversing: lap 33 goes 4190 m -> 210 m there.
SEAM_JUMP_M = 1000.0
COL_TIME = "time"
COL_LAP = "lap"
COL_LAP_DIST = "lap distance"
COL_SPEED_KMH = "speed ("

# The output grid. 10 m matches the profiles the car has always loaded, and is
# finer than the log's own 5-10 m sample spacing at racing speed.
STEP_M = 10.0

# A dip must be worth this much against the faster of its two shoulders before
# it counts as a corner. Below it the car was not grip-limited, it was lifting.
PROMINENCE_KMH = 6.0

# Finding the crawl (note 2). A corner is a V — the car is at its apex speed for
# a few tens of metres and then goes. An in-lap is a PLATEAU: lap 17 sits inside
# a 23-28 km/h band for its last 310 m. So the crawl is the run at the end of
# the lap that stays within CRAWL_BAND of the lap's slowest point, and it is
# only treated as one if it is at least CRAWL_MIN_M long — otherwise what has
# been found is an ordinary last corner and the tail is left alone.
CRAWL_BAND = 1.25
CRAWL_MIN_M = 100.0
# ... and only if the lap fails to close by more than this. A lap that ends
# within a few km/h of where it started needs no rebuilding whatever its shape.
CLOSE_TOL_KMH = 5.0
# Two minima closer together than this are one corner taken in two parts.
MERGE_M = 80.0
# How far either side of an apex still counts as the corner.
APEX_BAND = 1.10

# The strategies. Named for the lap they came from, because "base_210s" naming a
# 280 s curve is exactly the staleness Pit_Dashboard/constants.py warns about.
STRATEGIES = [
    ("dor_265s", "Fast", 265.0),      # 4:25
    ("dor_280s", "Base", 280.0),      # 4:40
    ("dor_300s", "Slow", 300.0),      # 5:00
]


# --------------------------------------------------------------------------- #
# 1. The lap, out of the car's export
# --------------------------------------------------------------------------- #
def _col(headers, want):
    for i, h in enumerate(headers):
        if h and str(h).strip().lower().startswith(want):
            return i
    raise SystemExit(f"no column starting {want!r} — got {headers}")


def read_lap(path=SOURCE_XLSX, lap=None, file_order=False):
    """[(lap_distance_m, speed_kmh)] for one lap, in the order logged.

    `file_order` is for a workbook THE CREW HAS ARRANGED BY HAND, which is what
    lap 33.xlsx is. On that lap GPS was down and laps were being cut by a
    person, so the car's lap-distance zero sat ~630 m before the real line:
    its own distance column put T1 at "39 km/h" and T8/9 flat out. The crew
    cut and pasted the rows so that ROW 1 IS THE START/FINISH LINE, matched
    against a lap whose corners they know. Then neither the clock column nor
    the absolute lap distance means anything any more -- the rows are the lap,
    in order, and only the distance BETWEEN rows is used: it is accumulated
    from row 1, and the one big backwards jump (the paste seam, where the end
    of the lap meets its beginning) counts as no distance at all.
    """
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    rows = list(wb[wb.sheetnames[0]].iter_rows(values_only=True))
    head = rows[0]
    i_lap, i_d, i_v = (_col(head, COL_LAP), _col(head, COL_LAP_DIST),
                       _col(head, COL_SPEED_KMH))
    # IN THE ORDER DRIVEN, not the order filed. The pit's per-lap workbook is
    # not written in time order (lap 33's first row is its 67th sample), and
    # everything below reads "the last sample" as "the end of the lap".
    body = [r for r in rows[1:] if r[i_d] is not None and r[i_v] is not None]
    if not file_order:
        try:
            i_t = _col(head, COL_TIME)
            body.sort(key=lambda r: r[i_t])
        except SystemExit:
            pass                   # a car log with no clock column is in order
    laps = {}
    for r in body:
        laps.setdefault(r[i_lap], []).append((float(r[i_d]), float(r[i_v])))
    if file_order:
        for key, rows_ in laps.items():
            pos, out = 0.0, []
            for j, (d, v) in enumerate(rows_):
                if j:
                    step = d - rows_[j - 1][0]
                    pos += step if step > -SEAM_JUMP_M else 0.0
                out.append((pos, v))
            laps[key] = out
    if lap is None:
        if len(laps) != 1:
            raise SystemExit(f"{os.path.basename(path)} holds laps "
                             f"{sorted(laps)} — pass --lap to choose one")
        lap = next(iter(laps))
    if lap not in laps:
        raise SystemExit(f"lap {lap} not in {sorted(laps)}")
    return lap, laps[lap]


def to_track_grid(samples, axis="odometer"):
    """Resample to STEP_M over 0..TRACK_LENGTH. See AXIS below for `axis`.

    Linear interpolation, not nearest-sample: at 60 km/h the log steps 5-10 m
    and about a third of the 10 m bins would otherwise be empty, and carrying
    the previous value forward across those would flatten every braking ramp
    into a staircase that the acceleration limits below would then read as real.
    """
    # A LAP DOES NOT HAVE TO START AT ZERO. Since the lap is cut by the gate
    # or by a person and never by distance alone, the car's lap distance can
    # run 210 -> 4190 m: the count began 210 m past the line and carried on
    # past 4000 until the next cut. The runtime lookup folds that number into
    # [0, lap) (speed_profile._wrap), so the file is built the same way --
    # what the car logged at 4100 m is what it must be shown at 100 m.
    # `measured` is the distance DRIVEN, which for a lap from zero is the last
    # reading, as it always was.
    measured = samples[-1][0] - samples[0][0]
    scale = (track.TRACK_LENGTH_METERS / measured) if axis == "track" else 1.0
    by_d = {}
    for d, v in samples:                       # the log repeats a distance when
        x = (d * scale) % track.TRACK_LENGTH_METERS
        by_d.setdefault(x, []).append(v)       # two samples land in one metre
    xs = sorted(by_d)
    ys = [sum(by_d[x]) / len(by_d[x]) for x in xs]

    def at(x):
        if x <= xs[0]:
            return ys[0]
        if x >= xs[-1]:
            return ys[-1]
        j = bisect.bisect_right(xs, x)
        x0, x1, y0, y1 = xs[j - 1], xs[j], ys[j - 1], ys[j]
        return y0 + (y1 - y0) * (x - x0) / (x1 - x0)

    n = int(track.TRACK_LENGTH_METERS // STEP_M)
    dist = [i * STEP_M for i in range(n)]
    return measured, scale, dist, [at(d) for d in dist]


# --------------------------------------------------------------------------- #
# 2. Close the lap
# --------------------------------------------------------------------------- #
def rebuild_tail(dist, kmh, accel_ms2):
    """Replace the in-lap crawl with a normal run to the line. See note 2 above.

    Returns (speeds, apex_index, why) — everything up to and including the
    final corner's apex is untouched. apex_index is None when nothing was done.
    """
    n = len(dist)
    if abs(kmh[-1] - kmh[0]) <= CLOSE_TOL_KMH:
        return list(kmh), None, "lap already closes"

    # Walk back from the line for as long as the speed stays in the band around
    # the lap's slowest point. That run is the plateau; its FIRST point is where
    # the last real corner bottomed out and the crawl took over.
    floor = min(kmh) * CRAWL_BAND
    apex = n - 1
    while apex - 1 >= 0 and kmh[apex - 1] <= floor:
        apex -= 1
    if dist[-1] - dist[apex] < CRAWL_MIN_M:
        return list(kmh), None, "no crawl found"

    out = list(kmh)
    v0 = kmh[apex] / 3.6                       # m/s at the apex, as driven
    v_end = kmh[0] / 3.6                       # where the lap must arrive
    for i in range(apex + 1, n):
        ds = dist[i] - dist[apex]
        v = (v0 ** 2 + 2.0 * accel_ms2 * ds) ** 0.5
        out[i] = min(v, v_end) * 3.6
    return out, apex, (f"crawl over the last {dist[-1] - dist[apex]:.0f} m "
                       f"replaced by acceleration at {accel_ms2:.2f} m/s²")


# --------------------------------------------------------------------------- #
# 3. Corners
# --------------------------------------------------------------------------- #
def _smooth(v):
    return [sum(v[max(0, i - 1):i + 2]) / len(v[max(0, i - 1):i + 2])
            for i in range(len(v))]


def find_corner_sections(dist, kmh, turn_scale=1.0):
    """['Straight'|'Turn'] per grid point, plus the corners found. See note 3."""
    n = len(kmh)
    s = _smooth(kmh)

    def prominence(i):
        l = i
        while l > 0 and s[l - 1] >= s[i]:
            l -= 1
        r = i
        while r < n - 1 and s[r + 1] >= s[i]:
            r += 1
        return min(max(s[l:i + 1]), max(s[i:r + 1])) - s[i]

    apexes = []
    for i in range(1, n - 1):
        if s[i] <= s[i - 1] and s[i] < s[i + 1] and prominence(i) >= PROMINENCE_KMH:
            if apexes and dist[i] - dist[apexes[-1]] < MERGE_M:
                if s[i] < s[apexes[-1]]:
                    apexes[-1] = i
            else:
                apexes.append(i)

    section = ["Straight"] * n
    corners = []
    for a in apexes:
        i0 = i1 = a
        while i0 - 1 >= 0 and s[i0 - 1] <= s[a] * APEX_BAND and s[i0 - 1] >= s[i0]:
            i0 -= 1
        while i1 + 1 < n and s[i1 + 1] <= s[a] * APEX_BAND and s[i1 + 1] >= s[i1]:
            i1 += 1
        for i in range(i0, i1 + 1):
            section[i] = "Turn"
        corners.append({"i0": i0, "i1": i1, "apex_i": a,
                        "names": _turn_names(dist[i0], dist[i1], turn_scale),
                        "prom": prominence(a)})
    return section, corners


def _turn_names(d0, d1, turn_scale=1.0):
    """Which surveyed turns a detected corner covers. Naming only.

    `turn_scale` carries the surveyed metres into whatever axis the file is on
    (see AXIS): on the odometer axis a turn surveyed at 3605 m is reached when
    the car's own counter reads 3659.

    The window reaches back 150 m because TURN_START_TRACK_M is where the
    CURVATURE starts and the apex sits after it, while a detected corner starts
    where the SPEED has already fallen — which is later than the braking point
    but can still precede a paired turn's second half.
    """
    return [t for t, m in sorted(TURN_START_TRACK_M.items(), key=lambda kv: kv[1])
            if d0 - 150.0 <= m * turn_scale <= d1 + 40.0]


# --------------------------------------------------------------------------- #
# 4. The file's axis
# --------------------------------------------------------------------------- #
# The CSVs on disk run 0..4010 every 10 m — 402 points — and the Speed Profile
# Builder REQUIRES that axis (Pit_Dashboard/profile_build.GRID_M; build_profile
# refuses a baseline of any other length, because it inherits the section labels
# index-for-index rather than re-classifying corners from a noisy lap).
#
# But a lap is 4000 m, so the solving is done on the closed 0..4000 loop and the
# 4010 row is appended afterwards. That way "280 s" is the time round the actual
# circuit and not the time round 4010 m of it — which is a 0.7 s lie, small
# enough to survive review and big enough to matter over a 4 h race.
WRAP_TO_M = 4010.0


def add_wrap_row(dist, speed, section):
    """Append the 4010 m row: the lap starting again. See the note above."""
    step = dist[1] - dist[0]
    n = int(round((WRAP_TO_M - dist[-1]) / step))
    out_d, out_v, out_s = list(dist), list(speed), list(section)
    for i in range(1, n + 1):
        out_d.append(dist[-1] + i * step)
        out_v.append(speed[i])          # d=4010 carries d=10's speed
        out_s.append(section[i])
    return out_d, out_v, out_s


def lap_time_to(dist, speed, limit_m):
    """Integrate ds/v over 0..limit_m only, ignoring any wrap rows past it."""
    total = 0.0
    for i in range(1, len(dist)):
        if dist[i] > limit_m:
            break
        ds = dist[i] - dist[i - 1]
        v = 0.5 * (speed[i] + speed[i - 1])
        if ds > 0 and v > 0:
            total += ds / v
    return total


def build_baseline(path=SOURCE_XLSX, lap=None, axis="odometer", file_order=False):
    """The real lap as (dist, speed_ms, section) on the car's 10 m grid."""
    lap_no, samples = read_lap(path, lap, file_order)
    measured, scale, dist, kmh = to_track_grid(samples, axis)

    # The acceleration limit for the tail rebuild has to come from the part of
    # the lap that is real driving, so it is measured BEFORE the rebuild — and
    # from the log's own speeds, which is what gen.peak_accel_decel does.
    ms = [v / 3.6 for v in kmh]
    accel_limit, _ = gen.peak_accel_decel(dist, ms)

    kmh, apex_i, tail_note = rebuild_tail(dist, kmh, accel_limit)
    section, corners = find_corner_sections(
        dist, kmh, measured / track.TRACK_LENGTH_METERS
                   if axis == "odometer" else 1.0)

    # Close the loop: one extra row at the finish line carrying the speed the
    # lap starts at. Without it the integral of ds/v covers 3990 m, not 4000,
    # and every lap time comes out 0.25 % short.
    dist = dist + [track.TRACK_LENGTH_METERS]
    kmh = kmh + [kmh[0]]
    section = section + [section[0]]

    return {
        "lap": lap_no, "measured_m": measured, "scale": scale,
        "dist": dist, "speed_ms": [v / 3.6 for v in kmh],
        "section": section, "corners": corners, "tail_note": tail_note,
        "axis": axis, "turn_scale": measured / track.TRACK_LENGTH_METERS
                                    if axis == "odometer" else 1.0,
        "tail_from_m": (dist[apex_i] if apex_i is not None else None),
    }


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=SOURCE_XLSX)
    ap.add_argument("--lap", type=int, default=None)
    ap.add_argument("--axis", choices=("odometer", "track"), default="odometer",
                    help="what the d(m) column counts: the car's own odometer "
                         "(default, and what the runtime lookup uses) or "
                         "surveyed track metres. See note 1 in the docstring.")
    ap.add_argument("--as-driven", metavar="KEY", default=None,
                    help="write ONE profile, KEY.csv, that is the lap exactly "
                         "as driven -- no target time, nothing scaled. For a "
                         "lap that is already the pace the crew wants (lap 33) "
                         "rather than raw material for a ladder of paces.")
    ap.add_argument("--file-order", action="store_true",
                    help="the workbook's rows are already the lap, in order, "
                         "with row 1 on the start/finish line -- ignore its "
                         "clock and its absolute lap distance. See read_lap().")
    ap.add_argument("--label", default="Base",
                    help="the human label written beside --as-driven's key")
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    b = build_baseline(args.source, args.lap, args.axis, args.file_order)
    dist, speed, section = b["dist"], b["speed_ms"], b["section"]
    base_t = gen.lap_time(dist, speed)
    accel_limit, brake_limit = gen.peak_accel_decel(dist, speed)

    print(f"source: {os.path.basename(args.source)}  lap {b['lap']}")
    print(f"  axis: {b['axis']}  (the lap's odometer read "
          f"{b['measured_m']:.0f} m for a "
          f"{track.TRACK_LENGTH_METERS:.0f} m circuit"
          + (f"; distances scaled x{b['scale']:.5f})" if b['axis'] == 'track'
             else "; distances left as the car counts them)"))
    if b["tail_from_m"] is None:
        print(f"  tail untouched: {b['tail_note']}")
    else:
        print(f"  tail: {b['tail_note']}, from {b['tail_from_m']:.0f} m")
    print(f"  baseline now {base_t:.2f} s, "
          f"{(dist[-1] / base_t) * 3.6:.1f} km/h avg")
    print(f"  limits from the lap: accel {accel_limit:.2f}, "
          f"brake {brake_limit:.2f} m/s²")

    print(f"\n{len(b['corners'])} corner(s) — held at the speed they were driven:")
    for c in b["corners"]:
        names = "/".join(f"T{t}" for t in c["names"]) or "(unmatched)"
        print(f"  {names:<14} {dist[c['i0']]:>5.0f}-{dist[c['i1']]:<5.0f} m   "
              f"apex {speed[c['apex_i']] * 3.6:5.1f} km/h at "
              f"{dist[c['apex_i']]:.0f} m   dip {c['prom']:.1f} km/h")
    turn_m = sum(1 for s in section if s == "Turn") * STEP_M
    print(f"  {turn_m:.0f} m of {track.TRACK_LENGTH_METERS:.0f} m fixed, "
          f"the rest is scaled")

    corners = gen.find_corners(dist, speed, section)
    print(f"\n{'profile':<12} {'target':>8} {'achieved':>9} {'k':>6} "
          f"{'avg':>7} {'max':>7}")
    results = []
    # AS DRIVEN: the lap itself is the profile. k is 1 by construction and the
    # "target" is simply the time the curve takes, so verify() holds it to the
    # same closing, reloading and 402-point checks as a solved profile -- and
    # its accel/brake check becomes "the file is the lap", which it must be.
    ladder = STRATEGIES if not args.as_driven else []
    if args.as_driven:
        print(f"{args.as_driven:<12} {base_t:>7.1f}s {base_t:>8.2f}s {1.0:>6.3f} "
              f"{(dist[-1] / base_t) * 3.6:>6.1f} {max(speed) * 3.6:>6.1f}"
              f"   (as driven)")
        results.append((args.as_driven, args.label, base_t, base_t, list(speed)))
    for key, label, target_s in ladder:
        k, spd, t = gen.solve_for_target(dist, speed, corners, target_s,
                                         accel_limit, brake_limit)
        print(f"{key:<12} {target_s:>7.1f}s {t:>8.2f}s {k:>6.3f} "
              f"{(dist[-1] / t) * 3.6:>6.1f} {max(spd) * 3.6:>6.1f}")
        results.append((key, label, target_s, t, spd))

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    for key, label, target_s, t, spd in results:
        wd, wv, ws = add_wrap_row(dist, spd, section)
        path, _ = gen.write_profile(key, label, wd, wv, ws, target_s, args.out)
        print(f"wrote {os.path.relpath(path, _REPO)}  ({len(wd)} points to "
              f"{wd[-1]:.0f} m)")

    if args.verify:
        return verify(dist, speed, section, corners, results,
                      accel_limit, brake_limit, args.out, b["turn_scale"])
    return 0


def verify(dist, speed, section, corners, results, accel_limit, brake_limit,
           out_dir, turn_scale=1.0):
    print("\n--- verify ---")
    ok = True

    # Did the corners land on the surveyed turns? This is the check that the
    # odometry normalisation was right; if it were not, the detected apexes
    # would drift steadily away from TURN_START_TRACK_M around the lap.
    print("  detected corner vs surveyed turn start:")
    for c in gen.find_corners(dist, speed, section):
        names = _turn_names(c["d0"], c["d1"], turn_scale)
        if not names:
            print(f"    FAIL  corner at {c['d0']:.0f}-{c['d1']:.0f} m "
                  f"matches no surveyed turn")
            ok = False
            continue
        first = TURN_START_TRACK_M[names[0]] * turn_scale
        print(f"    ok    {'/'.join('T%d' % t for t in names):<14} "
              f"corner starts {c['d0']:.0f} m, T{names[0]} at {first:.0f} m "
              f"({c['d0'] - first:+.0f} m)")

    base_apex = {c["apex_i"]: speed[c["apex_i"]] for c in corners}
    for key, label, target_s, t, spd in results:
        errs = []
        if abs(t - target_s) > 0.5:
            errs.append(f"lap time off by {t - target_s:+.2f}s")
        for i, v in base_apex.items():
            if spd[i] > v + 1e-6:
                errs.append(f"corner at {dist[i]:.0f} m exceeds the driven "
                            f"speed ({spd[i] * 3.6:.1f} > {v * 3.6:.1f} km/h)")
                break
        worst_acc = worst_dec = 0.0
        for i in range(1, len(dist)):
            ds = dist[i] - dist[i - 1]
            if ds > 0:
                a = (spd[i] ** 2 - spd[i - 1] ** 2) / (2 * ds)
                worst_acc = max(worst_acc, a)
                worst_dec = max(worst_dec, -a)
        if worst_dec > brake_limit * (1 + 1e-6):
            errs.append(f"brakes harder than the lap did "
                        f"({worst_dec:.2f} > {brake_limit:.2f})")
        if worst_acc > accel_limit * (1 + 1e-6):
            errs.append(f"accelerates harder than the lap did "
                        f"({worst_acc:.2f} > {accel_limit:.2f})")
        # The curve has to close, or the car is told to brake before the line.
        if abs(spd[-1] - spd[0]) > 0.5:
            errs.append(f"lap does not close ({spd[-1] * 3.6:.1f} km/h at the "
                        f"line, {spd[0] * 3.6:.1f} at the start)")
        # Reload from disk: the file is what the car will actually fly. The
        # comparison stops at the timing line, because the file carries the
        # wrap rows past it that the Profile Builder's grid requires.
        p = speed_profile.load_csv(os.path.join(out_dir, f"{key}.csv"), name=key,
                                   lap_length_m=track.TRACK_LENGTH_METERS)
        reloaded = lap_time_to(p.distances_m, p.speeds_ms,
                               track.TRACK_LENGTH_METERS)
        if abs(reloaded - t) > 0.05:
            errs.append(f"reloaded file laps in {reloaded:.2f}s, "
                        f"not the {t:.2f}s generated")
        if len(p) != 402:
            errs.append(f"{len(p)} points — the Profile Builder needs 402")

        print(f"  {'FAIL' if errs else 'PASS'}  {key:<12} "
              + ("; ".join(errs) if errs else
                 f"{t:.2f}s, corners as driven, accel/brake <= the lap, "
                 f"closes, reloads clean"))
        ok = ok and not errs

    print("\nall profiles verified" if ok else "\n*** VERIFICATION FAILED ***")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
