"""
generate_profiles.py — SUPERSEDED. The solver lives on; the baseline does not.
==============================================================================
!! DO NOT RUN THIS TO PRODUCE THE RACE PROFILES. Use:

    python tools/build_dor_profiles.py --verify

Its baseline, Pit_Dashboard/210s.xlsx, is a desk model: it commands 92 km/h down
the main straight for a 210 s lap, and the car's own log says 50 km/h average and
a 285 s lap. The five profiles this script wrote (fast_189s, med_fast_199s,
base_210s, med_slow_220s, slow_231s) were retired on 2026-09-19 and replaced by
three built from a lap the car actually drove. Copies are in profiles/_backup/.

Running it would write those five files back into profiles/, where the car scans
the whole directory at startup and the pit's dropdown lists whatever it finds —
so the crew would be offered five undrivable strategies again, mid-race, with no
warning. Hence the --resurrect-the-old-five flag below.

WHAT IS STILL USED, AND BY WHOM
Everything below the baseline loading: find_corners, peak_accel_decel,
scale_profile, solve_for_target, lap_time and write_profile are imported by
tools/build_dor_profiles.py and are the solver for the real profiles. This file
is not dead code; only its main() is.

    python tools/generate_profiles.py --resurrect-the-old-five [--verify]

THE IDEA: CORNERS ARE NOT STRATEGY
The naive way to make a "10 % faster" lap is to multiply every speed by 1.1.
That produces a file the car cannot follow. Corner speeds are set by grip and
geometry — the 2400-2490 m hairpin is taken at 28.8 km/h because that is what
the tyres and the radius allow, and no strategy decision changes it. Scaling it
to 31.7 km/h would just tell the driver to crash.

What a driver actually varies between a fast and a slow lap is the STRAIGHTS:
how hard to push between corners, and therefore how late to brake. So:

    corner apex speeds   unchanged, always
    straight speeds      scaled by k
    braking zones        rebuilt so the car still arrives at each corner at its
                         unchanged apex speed, without braking harder than the
                         baseline ever did

k is then solved so the resulting lap time hits the strategy's target. Because
corners are fixed, k is larger than the naive ratio — a 10 % faster lap needs
noticeably more than 10 % more speed on the straights, which is exactly the
real-world point: time saved on a lap gets harder to find as you go faster.

TOPOLOGY
Taken from the baseline itself rather than assumed:
  * `section` column already labels Straight vs Turn
  * corners are the contiguous Turn runs
  * each corner's apex is its slowest sample
  * braking zones are the decelerating run leading into each corner
  * the peak deceleration anywhere in the baseline becomes the braking limit
"""

import argparse
import csv
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import speed_profile  # noqa: E402

BASELINE_XLSX = os.path.join(_REPO, "Pit_Dashboard", "210s.xlsx")
OUT_DIR = os.path.join(_REPO, "profiles")

# The strategy matrix, mirroring Pit_Dashboard/constants.py STRATEGIES. Kept as
# (key, label, lap_time_s) so the generated filenames and the pit's dropdown
# always name the same thing.
STRATEGIES = [
    ("fast_189s",      "Fast (-10%)",     189.0),
    ("med_fast_199s",  "Med-Fast (-5%)",  199.5),
    ("base_210s",      "Base (210s)",     210.0),
    ("med_slow_220s",  "Med-Slow (+5%)",  220.5),
    ("slow_231s",      "Slow (+10%)",     231.0),
]

# A profile is never allowed to ask for less than this. Below a walking pace the
# car is stopped, not slow, and dividing by it to get a lap time explodes.
MIN_SPEED_MS = 2.0


# --------------------------------------------------------------------------- #
# 1. Read and analyse the baseline
# --------------------------------------------------------------------------- #
def load_baseline(path=BASELINE_XLSX):
    import pandas as pd
    df = pd.read_excel(path)
    for col in (speed_profile.COL_DIST, speed_profile.COL_SPEED_MS,
                speed_profile.COL_SECTION):
        if col not in df.columns:
            raise SystemExit(f"{path} has no {col!r} column — got {list(df.columns)}")
    return df


def find_corners(dist, speed, section):
    """Contiguous 'Turn' runs -> [{i0, i1, apex_i, apex_ms, d0, d1}].

    Uses the file's own section labels rather than guessing corners from speed
    minima: the team already curated them, and a speed-minimum heuristic would
    also fire on a slow patch of straight.
    """
    corners, i = [], 0
    n = len(dist)
    while i < n:
        if str(section[i]).strip().lower() != "turn":
            i += 1
            continue
        j = i
        while j + 1 < n and str(section[j + 1]).strip().lower() == "turn":
            j += 1
        apex = min(range(i, j + 1), key=lambda k: speed[k])
        corners.append({"i0": i, "i1": j, "apex_i": apex,
                        "apex_ms": speed[apex], "d0": dist[i], "d1": dist[j]})
        i = j + 1
    return corners


# An acceleration is treated as a spreadsheet artifact, not as data, when it is
# both large and ISOLATED — far bigger than the rows either side of it.
#
# Pit_Dashboard/210s.xlsx was assembled block by block, and five of the blocks
# were dropped in without matching the speed at the join. At 1190 m the baseline
# coasts up the hill at 36.7 km/h and the next row is a pasted "cruise at 90.0
# km/h, a = 0.00" block: 26.05 m/s², two and a half g, in ten metres. The same
# happens at 710, 2400, 2500 and 3000 m.
#
# The test is the neighbours, because that is what tells a join from a corner.
# Real braking ramps: the rows around the hardest one are also braking hard. A
# join is a single spike between two quiet rows — the five above are 13-26 m/s²
# next to neighbours of at most 1.00 m/s².
SPIKE_FLOOR_MS2 = 5.0     # below this, nothing is worth questioning
SPIKE_RATIO = 3.0         # ... and it must dwarf both neighbours by this much


def _accelerations(dist, speed):
    """[(index, distance, a)] from v² = u² + 2as, one per interval."""
    out = []
    for i in range(1, len(dist)):
        ds = dist[i] - dist[i - 1]
        if ds > 0:
            out.append((i, dist[i], (speed[i] ** 2 - speed[i - 1] ** 2) / (2 * ds)))
    return out


def find_discontinuities(dist, speed):
    """Rows where the baseline steps rather than accelerates. See the note above."""
    acc = _accelerations(dist, speed)
    bad = []
    for n, (i, d, a) in enumerate(acc):
        prev = abs(acc[n - 1][2]) if n else 0.0
        nxt = abs(acc[n + 1][2]) if n + 1 < len(acc) else 0.0
        neighbour = max(prev, nxt)
        if abs(a) > SPIKE_FLOOR_MS2 and abs(a) > SPIKE_RATIO * neighbour:
            bad.append({"i": i, "d": d, "a": a, "neighbour": neighbour})
    return bad


def peak_accel_decel(dist, speed):
    """(max acceleration, max deceleration) in the baseline, both positive.

    Derived from the data rather than assumed, so a generated profile never asks
    for harder braking or sharper acceleration than this car has already been
    shown to do. "No worse than the baseline" is a guarantee we can actually
    justify; a made-up number is not.

    !!️ The guarantee is only worth anything if the baseline is clean. It is not:
    the five join artifacts above used to set these limits themselves, so the
    generator was calibrating "no harder than the baseline" against 26.05 and
    20.31 m/s² and every profile it wrote passed its own safety check while
    demanding two g of braking at four corners. A driver cannot follow that — the
    HUD would call them slow at every corner for driving as hard as the car can.
    Excluding the five artifacts, the baseline's real limits are ±4.56 m/s².
    """
    up = down = 0.0
    skip = {b["i"] for b in find_discontinuities(dist, speed)}
    for i, d, a in _accelerations(dist, speed):
        if i in skip:
            continue
        up = max(up, a)
        down = max(down, -a)
    return (up if up > 0 else 2.0), (down if down > 0 else 2.0)


# --------------------------------------------------------------------------- #
# 2. Build one strategy's profile
# --------------------------------------------------------------------------- #
def scale_profile(dist, speed, corners, k, accel_limit, brake_limit):
    """Scale straights by k, cap corners at their baseline speed, then make the
    whole lap physically drivable.

    Corners are a CAP, not a fixed value: the baseline corner speed is the most
    grip allows, so a faster strategy may never exceed it — but a slower
    strategy is free to go under it. Holding corners rigidly at the baseline
    while scaling the straights DOWN was wrong: it left the car quicker through
    the corner than on the straight after it, which the profile could only
    express as violent braking at corner exit. (The first version of this did
    exactly that, and the slow strategies came out braking at 22 m/s² against a
    20.3 m/s² baseline.)

    The two passes at the end are the standard way to make a speed profile
    achievable, and they are also what implements "adjust braking points":

      backward  no point may be faster than braking distance to the next allows
      forward   no point may be faster than accelerating from the previous allows

    Braking points therefore fall out of the physics rather than being placed by
    hand — a faster strategy naturally brakes later, a slower one earlier.
    """
    corner_idx = set()
    for c in corners:
        for i in range(c["i0"], c["i1"] + 1):
            corner_idx.add(i)

    out = []
    for i, v in enumerate(speed):
        target = v * k
        if i in corner_idx:
            target = min(target, v)     # never quicker than the baseline corner
        out.append(max(MIN_SPEED_MS, target))

    # Backward pass — braking. Walking from the end, the fastest we may be `ds`
    # before a point where we must be at v is sqrt(v² + 2·a_brake·ds).
    for i in range(len(out) - 2, -1, -1):
        ds = dist[i + 1] - dist[i]
        if ds <= 0:
            continue
        v_max = math.sqrt(out[i + 1] ** 2 + 2 * brake_limit * ds)
        if out[i] > v_max:
            out[i] = v_max

    # Forward pass — acceleration, which is what removes the corner-exit spike.
    for i in range(1, len(out)):
        ds = dist[i] - dist[i - 1]
        if ds <= 0:
            continue
        v_max = math.sqrt(out[i - 1] ** 2 + 2 * accel_limit * ds)
        if out[i] > v_max:
            out[i] = v_max

    return out


def lap_time(dist, speed):
    """Integrate ds/v over the lap."""
    total = 0.0
    for i in range(1, len(dist)):
        ds = dist[i] - dist[i - 1]
        v = 0.5 * (speed[i] + speed[i - 1])
        if ds > 0 and v > 0:
            total += ds / v
    return total


def solve_for_target(dist, speed, corners, target_s, accel_limit, brake_limit):
    """Bisect on the straight-scaling factor until the lap time hits target.

    Bisection rather than a closed form because the braking rebuild makes lap
    time a non-linear function of k: pushing the straights higher also lengthens
    the braking zones, which gives some of the time back. That feedback is the
    whole reason a 10 % quicker lap needs more than 10 % more straight-line
    speed, and it is why the naive multiply-everything approach silently misses
    its own target.
    """
    lo, hi = 0.2, 5.0
    best = None
    for _ in range(80):
        k = 0.5 * (lo + hi)
        cand = scale_profile(dist, speed, corners, k, accel_limit, brake_limit)
        t = lap_time(dist, cand)
        best = (k, cand, t)
        if abs(t - target_s) < 0.01:
            break
        if t > target_s:        # too slow -> need more speed
            lo = k
        else:
            hi = k
    return best


# --------------------------------------------------------------------------- #
# 3. Write
# --------------------------------------------------------------------------- #
def write_profile(key, label, dist, speed, section, target_s, out_dir=OUT_DIR):
    """Write the generated profile in the SAME schema as the baseline, so both
    existing loaders read it with no code change."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{key}.csv")

    # Re-integrate time and re-derive acceleration so the file is internally
    # consistent — a stored Time(s) that disagreed with the speeds beside it
    # would be a trap for anyone reading the file later.
    t = 0.0
    rows = []
    for i in range(len(dist)):
        if i > 0:
            ds = dist[i] - dist[i - 1]
            v = 0.5 * (speed[i] + speed[i - 1])
            if ds > 0 and v > 0:
                t += ds / v
        if i == 0:
            a = 0.0
        else:
            ds = dist[i] - dist[i - 1]
            a = ((speed[i] ** 2 - speed[i - 1] ** 2) / (2 * ds)) if ds > 0 else 0.0
        rows.append({
            speed_profile.COL_SECTION: section[i],
            speed_profile.COL_DIST: int(dist[i]),
            speed_profile.COL_SPEED_MS: round(speed[i], 6),
            "V(km/h)": round(speed[i] * 3.6, 3),
            "a(m/s^2)": round(a, 6),
            speed_profile.COL_TIME: round(t, 6),
        })

    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return path, t


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", default=BASELINE_XLSX)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--verify", action="store_true",
                    help="check each generated profile against its target")
    ap.add_argument("--resurrect-the-old-five", action="store_true",
                    help="write the retired 189-231 s profiles back into "
                         "profiles/ (see the module docstring — you almost "
                         "certainly want tools/build_dor_profiles.py instead)")
    args = ap.parse_args()

    if not args.resurrect_the_old_five:
        print(__doc__.strip())
        print("\nRefusing to run. Nothing was written.")
        return 2

    df = load_baseline(args.baseline)
    dist = [float(x) for x in df[speed_profile.COL_DIST]]
    speed = [float(x) for x in df[speed_profile.COL_SPEED_MS]]
    section = [str(x) for x in df[speed_profile.COL_SECTION]]

    corners = find_corners(dist, speed, section)
    accel_limit, brake_limit = peak_accel_decel(dist, speed)
    base_time = lap_time(dist, speed)

    print(f"baseline: {os.path.basename(args.baseline)}  "
          f"{len(dist)} points, {dist[-1]:.0f} m, {base_time:.2f} s")

    # Say it out loud every run. These are defects in the team's spreadsheet,
    # and the generator works around them rather than fixing them at source —
    # so the only place anyone will ever find out is here.
    bad = find_discontinuities(dist, speed)
    if bad:
        print(f"\n!! {len(bad)} discontinuity/ies in the baseline "
              f"(block joins, not driving) - excluded from the limits:")
        for b in bad:
            print(f"    {b['d']:>5.0f} m   a = {b['a']:+7.2f} m/s²   "
                  f"(neighbouring rows at most {b['neighbour']:.2f})")
        print("    The braking/acceleration passes below smooth these out, so the")
        print("    generated profiles are drivable even though the baseline is not.")

    print(f"\nlimits taken from the baseline: accel {accel_limit:.2f}, "
          f"brake {brake_limit:.2f} m/s²")
    print(f"\n{len(corners)} corner(s) detected — apex speeds are held fixed:")
    for n, c in enumerate(corners, 1):
        print(f"  T{n}  {c['d0']:>5.0f}-{c['d1']:<5.0f} m   "
              f"apex {c['apex_ms'] * 3.6:5.1f} km/h at {dist[c['apex_i']]:.0f} m")

    print(f"\n{'profile':<16} {'target':>8} {'achieved':>9} {'k':>6} "
          f"{'avg':>8} {'straight max':>13}")
    results = []
    for key, label, target_s in STRATEGIES:
        k, spd, t = solve_for_target(dist, speed, corners, target_s,
                                     accel_limit, brake_limit)
        path, written_t = write_profile(key, label, dist, spd, section,
                                        target_s, args.out)
        smax = max(spd) * 3.6
        avg = (dist[-1] / t) * 3.6
        print(f"{key:<16} {target_s:>7.1f}s {t:>8.2f}s {k:>6.3f} "
              f"{avg:>7.1f} {smax:>12.1f}")
        results.append((key, label, target_s, t, spd, path))

    print(f"\nwritten to {args.out}")

    if args.verify:
        print("\n--- verify ---")
        ok = True
        base_apex = {c["apex_i"]: speed[c["apex_i"]] for c in corners}
        # A tolerance, not equality: these are floats compared against values
        # rebuilt through two sqrt passes, and 1e-9 would fail on rounding noise
        # while telling us nothing about whether the car can drive the profile.
        TOL = 1e-6
        for key, label, target_s, t, spd, path in results:
            errs = []
            if abs(t - target_s) > 0.5:
                errs.append(f"lap time off by {t - target_s:+.2f}s")
            # Corners are a CAP: never faster than the baseline. A slower
            # strategy is allowed to be under it.
            for i, v in base_apex.items():
                if spd[i] > v + TOL:
                    errs.append(f"corner apex at {dist[i]:.0f} m exceeds the "
                                f"grip limit ({spd[i] * 3.6:.1f} > {v * 3.6:.1f} km/h)")
                    break
            worst_dec = worst_acc = 0.0
            for i in range(1, len(dist)):
                ds = dist[i] - dist[i - 1]
                if ds > 0:
                    a = (spd[i] ** 2 - spd[i - 1] ** 2) / (2 * ds)
                    worst_dec = max(worst_dec, -a)
                    worst_acc = max(worst_acc, a)
            if worst_dec > brake_limit * (1 + 1e-6):
                errs.append(f"brakes harder than baseline "
                            f"({worst_dec:.2f} > {brake_limit:.2f})")
            if worst_acc > accel_limit * (1 + 1e-6):
                errs.append(f"accelerates harder than baseline "
                            f"({worst_acc:.2f} > {accel_limit:.2f})")
            if min(spd) < MIN_SPEED_MS - TOL:
                errs.append("speed below the floor")

            # Reload from disk: the file is what the car will actually fly.
            p = speed_profile.load_csv(path, name=key)
            if abs(p.lap_time_s() - t) > 0.05:
                errs.append("reloaded file disagrees with what was generated")

            print(f"  {'FAIL' if errs else 'PASS'}  {key:<16} "
                  + ("; ".join(errs) if errs else
                     f"{t:.2f}s, corners within grip, accel/brake <= baseline, "
                     f"reloads clean"))
            ok = ok and not errs
        print("\nall profiles verified" if ok else "\n*** VERIFICATION FAILED ***")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
