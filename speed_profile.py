"""
speed_profile.py — the target-speed curve, read identically by car and pit
==========================================================================
A speed profile is a table of distance-around-the-lap versus the speed the car
should be doing there. The pit uses it to show the driver's target and to judge
sector pace; the car uses it to display that target on the HUD and to warn about
corners before the driver can see them.

Shared at the repo root, next to drivetrain.py / track.py / limits.py, for the
reason those exist: every time this project kept one number in two places, the
two drifted. The speedometer read 3.3 % high against the pit, the lap length was
written out three times, and the temperature limits disagreed. A target speed
that differs between the driver's screen and the strategist's screen would be
the same bug with higher stakes — they would be talking past each other about
which one the car is actually failing to hit.

PROFILE FORMAT
Whatever `Pit_Dashboard/210s.xlsx` uses, because that is the file the team
already produces and the pit already loads:

    section   'Straight' | 'Turn'
    d(m)      distance around the lap, ascending, 0 .. ~4010
    V(m/s)    target speed  (NOT km/h — the loaders convert)
    Time(s)   cumulative lap time at that point (optional but kept)

The generated per-strategy profiles use the identical schema, so both existing
loaders read them with no code change.

WRAP-AROUND
A lap is a loop, so a look-ahead near the end of the lap has to continue past
the finish line into the start of the next one. Everything here treats distance
modulo the lap length rather than clamping at the last row — clamping is what
would make a corner 80 m after the line invisible to a driver 40 m before it.
"""

import bisect
import csv
import math
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(_HERE, "profiles")

# Column names, as produced by the team's Excel export.
COL_SECTION, COL_DIST, COL_SPEED_MS, COL_TIME = "section", "d(m)", "V(m/s)", "Time(s)"


class SpeedProfile:
    """One strategy's target-speed curve.

    Deliberately plain: lists and bisect, no pandas. The car imports this inside
    the CAN worker's thread on a Raspberry Pi, and pulling pandas in for a
    400-row lookup table would cost seconds of startup and tens of megabytes for
    nothing.
    """

    def __init__(self, name, distances_m, speeds_ms, sections=None,
                 lap_length_m=None, source=None):
        if len(distances_m) < 2:
            raise ValueError(f"profile {name!r} needs at least two points")
        self.name = name
        self.source = source
        self.distances_m = list(distances_m)
        self.speeds_ms = list(speeds_ms)
        self.sections = list(sections) if sections else [""] * len(distances_m)
        # The lap is as long as the profile unless told otherwise. Using the
        # profile's own extent keeps wrap-around consistent with the data even
        # when the file runs slightly past the timing line (210s.xlsx ends at
        # 4010 m for a 4000 m lap).
        self.lap_length_m = float(lap_length_m or self.distances_m[-1])

    # ------------------------------------------------------------------ #
    def __len__(self):
        return len(self.distances_m)

    def _wrap(self, distance_m):
        """Distance folded into [0, lap_length). A lap is a loop."""
        if self.lap_length_m <= 0:
            return 0.0
        return float(distance_m) % self.lap_length_m

    def speed_ms_at(self, distance_m):
        """Target speed in m/s at a point on the lap (linear interpolation)."""
        d = self._wrap(distance_m)
        xs = self.distances_m
        i = bisect.bisect_left(xs, d)
        if i <= 0:
            return self.speeds_ms[0]
        if i >= len(xs):
            return self.speeds_ms[-1]
        if xs[i] == d:
            return self.speeds_ms[i]
        x0, x1 = xs[i - 1], xs[i]
        y0, y1 = self.speeds_ms[i - 1], self.speeds_ms[i]
        span = x1 - x0
        if span <= 0:
            return y0
        return y0 + (d - x0) * (y1 - y0) / span

    def speed_kmh_at(self, distance_m):
        """Target speed in km/h — what both dashboards actually display."""
        return self.speed_ms_at(distance_m) * 3.6

    def section_at(self, distance_m):
        """'Straight' or 'Turn' at a point on the lap."""
        d = self._wrap(distance_m)
        i = bisect.bisect_left(self.distances_m, d)
        i = min(max(i, 0), len(self.sections) - 1)
        return self.sections[i]

    def lap_time_s(self):
        """Time to complete one lap at these speeds: the integral of ds/v.

        This is the number that defines a strategy, so it is computed from the
        speeds rather than trusted from a stored column — a profile whose
        Time(s) column disagreed with its own speeds would silently misreport
        which strategy the car is running.
        """
        total = 0.0
        for i in range(1, len(self.distances_m)):
            ds = self.distances_m[i] - self.distances_m[i - 1]
            v = 0.5 * (self.speeds_ms[i] + self.speeds_ms[i - 1])
            if ds > 0 and v > 0:
                total += ds / v
        return total

    def average_kmh(self):
        t = self.lap_time_s()
        return (self.distances_m[-1] / t) * 3.6 if t > 0 else 0.0

    # ------------------------------------------------------------------ #
    def look_ahead(self, distance_m, window_m=175.0, min_drop_kmh=15.0):
        """Slowest point within `window_m` ahead — the turn-alert primitive.

        Returns (distance_ahead_m, min_speed_kmh, drop_kmh) for the slowest
        point in the window, or None when nothing there is enough slower to be
        worth telling the driver about.

        `min_drop_kmh` is what makes this usable. Without it the profile's
        ordinary ripple sets it off constantly — on the main straight the target
        wanders by a km/h or so, and an alert that fires for a 0.7 km/h dip is an
        alert the driver stops reading. 15 km/h is comfortably above the ripple
        and comfortably below every real corner here: the gentlest is T1 at
        94 → 83 km/h on entry, the sharpest T3 at 90 → 29.

        Scans WRAPPED, so a corner just after the finish line is still seen by a
        driver approaching the line — the one place a driver most needs warning
        is exactly where the data file happens to end.
        """
        here = self._wrap(distance_m)
        current = self.speed_ms_at(here)

        worst_d, worst_v = None, current
        # Step in profile-resolution increments so no sample is skipped.
        step = max(5.0, self._median_step())
        offset = step
        while offset <= window_m:
            d = self._wrap(here + offset)
            v = self.speed_ms_at(d)
            if v < worst_v:
                worst_v, worst_d = v, offset
            offset += step

        if worst_d is None:
            return None
        drop_kmh = (current - worst_v) * 3.6
        if drop_kmh < min_drop_kmh:
            return None
        return (worst_d, worst_v * 3.6, drop_kmh)

    def _median_step(self):
        if getattr(self, "_step_cache", None) is None:
            steps = [self.distances_m[i] - self.distances_m[i - 1]
                     for i in range(1, min(len(self.distances_m), 50))]
            steps = [s for s in steps if s > 0]
            self._step_cache = (sorted(steps)[len(steps) // 2] if steps else 10.0)
        return self._step_cache


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_csv(path, name=None, lap_length_m=None):
    """Load a generated profile CSV. Pure stdlib — see the class docstring."""
    distances, speeds, sections = [], [], []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                distances.append(float(row[COL_DIST]))
                speeds.append(float(row[COL_SPEED_MS]))
            except (KeyError, TypeError, ValueError):
                continue        # skip a malformed row rather than lose the file
            sections.append((row.get(COL_SECTION) or "").strip())
    return SpeedProfile(name or os.path.splitext(os.path.basename(path))[0],
                        distances, speeds, sections, lap_length_m, source=path)


def load_excel(path, name=None, lap_length_m=None, sheet=0):
    """Load the team's .xlsx baseline. Needs pandas, so the pit uses this and
    the car uses the generated CSVs."""
    import pandas as pd                     # local: the car never needs pandas
    df = pd.read_excel(path, sheet_name=sheet)
    return SpeedProfile(name or os.path.splitext(os.path.basename(path))[0],
                        df[COL_DIST].tolist(), df[COL_SPEED_MS].tolist(),
                        df[COL_SECTION].tolist() if COL_SECTION in df else None,
                        lap_length_m, source=path)


def available_profiles(directory=PROFILE_DIR):
    """{strategy key -> csv path} for everything generated into profiles/."""
    if not os.path.isdir(directory):
        return {}
    return {os.path.splitext(f)[0]: os.path.join(directory, f)
            for f in sorted(os.listdir(directory)) if f.endswith(".csv")}


def load_all(directory=PROFILE_DIR, lap_length_m=None):
    """Load every generated profile. Returns {key: SpeedProfile}.

    A profile that fails to parse is skipped with a warning rather than taking
    the others down — losing one strategy is recoverable mid-race, losing the
    target-speed display entirely is not.
    """
    out = {}
    for key, path in available_profiles(directory).items():
        try:
            out[key] = load_csv(path, name=key, lap_length_m=lap_length_m)
        except Exception as exc:
            print(f"⚠️ speed profile {key!r} failed to load: {exc}")
    return out


# --------------------------------------------------------------------------- #
# Self-check:  python3 speed_profile.py
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    profiles = load_all()
    if not profiles:
        print(f"No profiles in {PROFILE_DIR}.")
        print("Generate them with:  python tools/generate_profiles.py")
        raise SystemExit(0)

    print(f"{len(profiles)} profile(s) in {PROFILE_DIR}\n")
    print(f"  {'profile':<18} {'lap time':>9} {'avg':>9}")
    for key, p in profiles.items():
        print(f"  {key:<18} {p.lap_time_s():>8.1f}s {p.average_kmh():>8.1f}")

    key = "base_210s" if "base_210s" in profiles else next(iter(profiles))
    p = profiles[key]
    print(f"\n  target speed around the lap, {key}:")
    for d in (0, 450, 650, 1500, 1850, 2450, 3200, 3900):
        ahead = p.look_ahead(d)
        note = ""
        if ahead:
            note = (f"   ⚠ TURN in {ahead[0]:.0f} m — max {ahead[1]:.0f} km/h"
                    f"  (−{ahead[2]:.0f})")
        print(f"    {d:>5} m  {p.speed_kmh_at(d):>6.1f} km/h  "
              f"{p.section_at(d):<9}{note}")
