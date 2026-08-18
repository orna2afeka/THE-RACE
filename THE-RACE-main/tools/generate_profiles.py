"""
generate_profiles.py — one speed profile per race strategy
===========================================================
Reads the team's baseline lap (Pit_Dashboard/210s.xlsx, 210.0 s) and produces a
target-speed profile for every strategy in the strategy matrix, writing them to
profiles/ for the car and the pit to load.

    python tools/generate_profiles.py            # generate
    python tools/generate_profiles.py --verify   # generate + check the results

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


def peak_accel_decel(dist, speed):
    """(max acceleration, max deceleration) in the baseline, both positive.

    Derived from the data rather than assumed, so a generated profile never asks
    for harder braking or sharper acceleration than this car has already been
    shown to do. "No worse than the baseline" is a guarantee we can actually
    justify; a made-up number is not.
    """
    up = down = 0.0
    for i in range(1, len(dist)):
        ds = dist[i] - dist[i - 1]
        if ds <= 0:
            continue
        # v² = u² + 2as  ->  a = (v² - u²) / 2s
        a = (speed[i] ** 2 - speed[i - 1] ** 2) / (2 * ds)
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
    args = ap.parse_args()

    df = load_baseline(args.baseline)
    dist = [float(x) for x in df[speed_profile.COL_DIST]]
    speed = [float(x) for x in df[speed_profile.COL_SPEED_MS]]
    section = [str(x) for x in df[speed_profile.COL_SECTION]]

    corners = find_corners(dist, speed, section)
    accel_limit, brake_limit = peak_accel_decel(dist, speed)
    base_time = lap_time(dist, speed)

    print(f"baseline: {os.path.basename(args.baseline)}  "
          f"{len(dist)} points, {dist[-1]:.0f} m, {base_time:.2f} s")
    print(f"limits taken from the baseline: accel {accel_limit:.2f}, "
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
