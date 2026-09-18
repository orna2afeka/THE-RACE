"""
energy_model.py — what each speed profile COSTS, from one measured run
=======================================================================
The arithmetic behind energy_matrix.py, kept in its own module with NO
Streamlit in it so every step can be exercised headlessly:

    python Pit_Dashboard/energy_model.py --self-check
    python Pit_Dashboard/energy_model.py --wh 117 --minutes 5
    python Pit_Dashboard/energy_model.py --wh 117 --minutes 5 --distance-m 2860
    python Pit_Dashboard/energy_model.py --from-db

WHAT PROBLEM THIS SOLVES
`energy_wh` is the one column of constants.PROFILE_MATRIX that cannot be
derived from a speed curve — lap time is integrated from the curve itself, but
what a lap COSTS needs a vehicle. The five numbers in there today (88/84/80/
76/72 Wh) are a placeholder ladder: 80 Wh plus or minus 5 % and 10 %, chosen to
mirror the profiles' target times. Nobody measured them. Every stop count and
every "laps remaining" the Strategy tab prints rests on them.

This turns ONE run the car actually did into all five numbers.

YOU DO NOT NEED THE SPEEDS. YOU NEED THE DISTANCE.
"117 Wh in 5 minutes" fixes the average POWER (1404 W) and nothing else. Wh per
LAP is what the strategy matrix is made of, and 117 Wh in 5 minutes is 81.9 Wh
per lap if the car covered 1.43 laps, 117 Wh per lap if it covered one, and
58.5 Wh per lap if it covered two. The speed trace is not needed anywhere —
only how far the car went, which the car already reports as `odometer_m` and
which the pit already stores. --from-db needs neither, because a completed lap
IS a known distance.

THE MODEL, AND WHY IT IS NOT A PERCENTAGE LADDER
A lap's energy splits into a part that depends on how far you go and a part
that depends on how fast:

    E_lap  =  a · L  +  b · A

    L = 4000 m, the same for every profile — rolling resistance, bearing drag
        and the drivetrain's own losses are paid PER METRE, so they do not fall
        when the driver goes slower. They only take longer.
    A = the integral of v² along the lap, computed from the profile's own CSV.
        Aerodynamic drag is the part strategy actually buys and sells.

Across the five profiles A spans +31 % (fast_189s) to -18 % (slow_231s) while L
does not move at all. That is the whole difference between this and a ±10 %
ladder, and it matters most at the slow end: the ladder says slow_231s costs
90 % of base, the model says ~94 %, because the ladder quietly assumes rolling
loss gets cheaper when you slow down. It does not.

Worth knowing: the existing ladder IS this model at an aero share of about 1/3,
on the fast side. The team's placeholder was not arbitrary — it was right about
the fast half and optimistic about the slow half.

ONE MEASUREMENT FIXES ONE COEFFICIENT
Two unknowns, a and b. One run gives one equation, so one assumption has to be
supplied: the AERO SHARE, what fraction of the anchor lap's energy is drag.
DEFAULT_AERO_SHARE below is 1/3 and says why.

TWO RUNS ON TWO DIFFERENT PROFILES REMOVE THE ASSUMPTION ENTIRELY. Then a and
b are both solved, least-squares, and nothing is assumed at all. That is what
--from-db reaches for as soon as the car has driven laps on more than one
profile, and it is the number to trust over anything typed in by hand.

WHICH ENERGY
The car's own integration: signed motor power, trapezoidal, regen subtracting
(LapTracker.update_energy). So these are MOTOR-side watt-hours net of regen,
the same basis as `last_lap_energy` — which is what makes the output directly
comparable with the "measured" column the Strategy tab already shows. It is NOT
pack-side: whatever the battery loses to its own internal resistance and the
controller loses as heat is not in here, and a pack sized off these numbers
alone would be sized short.

THIS MODULE NEVER WRITES ANYTHING. It prints a matrix and the reasoning behind
it. Putting the numbers into constants.py is a deliberate, separate act — by
hand, or through the Profile Builder's Save button, which validates and backs
up (profile_manage.write_saved_matrix).
"""

import argparse
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
for _p in (_REPO_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import speed_profile   # noqa: E402  (path set up immediately above)
import track           # noqa: E402

LAP_M = float(track.TRACK_LENGTH_METERS)

# How long a simulated race runs, for the report line that says so. Read from
# strategy_engine, which owns the race rules — NOT copied, because a second
# 1440.0 in the tree is the drift that putting it there was meant to end. The
# fallback exists only so this module still imports where matplotlib does not.
try:
    from strategy_engine import RACE_DURATION_MIN as RACE_MIN
except Exception:                                # noqa: BLE001
    RACE_MIN = 1440.0

# ── The one assumption a single measurement cannot avoid ──────────────────── #
# What fraction of the BASE lap's energy is aerodynamic drag. 1/3 rather than a
# round 1/2 for a reason that can be checked: at 1/3 this model reproduces the
# team's existing 88/84/80 Wh for the fast three profiles almost exactly, so
# adopting it changes nothing anyone has been planning with on that side while
# correcting the slow side. It is a solar car at 60-75 km/h, which is squarely
# the regime where rolling and aero are the same order.
#
# REPLACE IT WITH A FIT. Two laps on two different profiles make this number
# unnecessary — see fit_from_measurements(). Until then it is the one place
# this whole calculation is guessing, and it is deliberately one line.
DEFAULT_AERO_SHARE = 1.0 / 3.0

# Below this the fit is refusing to do physics: a negative coefficient means
# "going faster costs less", or "distance is free". Both come from measurements
# too close together to separate the two effects, and the caller is told rather
# than handed a matrix that slopes the wrong way.
MIN_COEFFICIENT = 0.0


# --------------------------------------------------------------------------- #
# 1. What each profile demands, from its own curve
# --------------------------------------------------------------------------- #
def aero_integral(prof, lap_length_m=LAP_M):
    """Integral of v² ds along one lap, in m³/s².

    The shape of the drag bill. Multiplied by ½ρC_dA it would be joules; left
    unscaled here because C_dA is not known for this car and the measurement is
    what supplies the scale.

    STOPS AT THE LAP LENGTH. The CSVs run to 4010 m — the last two rows are the
    lap wrapping round (profile_build.GRID_M says so, and base_210s.csv repeats
    d=0's speed at d=4000). Integrating them would count 10 m of the next lap.
    """
    d, v = prof.distances_m, prof.speeds_ms
    total = 0.0
    for i in range(1, len(d)):
        d0, d1 = d[i - 1], d[i]
        if d0 >= lap_length_m:
            break
        if d1 > lap_length_m:                    # clip the final partial step
            frac = (lap_length_m - d0) / (d1 - d0)
            v1 = v[i - 1] + (v[i] - v[i - 1]) * frac
            d1 = lap_length_m
        else:
            v1 = v[i]
        total += ((v1 + v[i - 1]) / 2.0) ** 2 * (d1 - d0)
    return total


def load_profiles(lap_length_m=LAP_M):
    """{key: {label, lap_time_s, aero}} for every profile on disk.

    Scans the directory, exactly as the car does, so a profile the Builder
    wrote from a measured lap is costed alongside the five synthetic ones
    instead of being invisible here.
    """
    out = {}
    for key, path in speed_profile.available_profiles().items():
        prof = speed_profile.load_csv(path, name=key, lap_length_m=lap_length_m)
        out[key] = {"label": _label_for(key),
                    "lap_time_s": prof.lap_time_s(),
                    "aero": aero_integral(prof, lap_length_m)}
    return dict(sorted(out.items(), key=lambda kv: kv[1]["lap_time_s"]))


def _label_for(key):
    try:
        import constants as C
        meta = C.PROFILE_MATRIX.get(key) or {}
        if meta.get("label"):
            return meta["label"]
    except Exception:                            # noqa: BLE001
        pass
    return key.replace("_", " ").title()


# ── Drag against pace, for laps that are not one of the five ─────────────── #
# A measured lap is almost never a lap of a profile. The GPS trigger has to
# fire for a lap to be 4000 m at all; when it does not, lap_tracker cuts at
# ODOMETER_FORCE_LAP_M instead and the "lap" is 4200 m at whatever pace the car
# was doing. Costing that as though it were base_210s is how a bench session at
# 50 km/h ends up setting the energy budget for a race at 68.
#
# So a measurement is anchored to the pace it was ACTUALLY driven at, and its
# drag integral comes from this law rather than from a profile's CSV.
#
# WHY A POWER LAW. If a lap were simply the same shape driven k times faster,
# every v would scale by k and A would go as k^2 while the lap time went as
# 1/k — exactly A ∝ t^-2. It is not exactly that, because the corners do NOT
# scale (tools/generate_profiles.py exists to keep them fixed), so the real
# exponent comes out somewhat steeper. Fitting it from the five profiles rather
# than assuming -2 lets the curve the team actually generated speak for itself.
MIN_PROFILES_FOR_LAW = 2


def aero_law(profiles):
    """(C, n) for A ≈ C·t^-n, least squares in log-log across the profiles.

    Returns None when there are too few profiles to fit, in which case callers
    fall back to the nearest profile's own integral.
    """
    pts = [(p["lap_time_s"], p["aero"]) for p in profiles.values()
           if p["lap_time_s"] > 0 and p["aero"] > 0]
    if len(pts) < MIN_PROFILES_FOR_LAW:
        return None
    n = len(pts)
    sx = sum(math.log(t) for t, _ in pts)
    sy = sum(math.log(a) for _, a in pts)
    sxx = sum(math.log(t) ** 2 for t, _ in pts)
    sxy = sum(math.log(t) * math.log(a) for t, a in pts)
    det = n * sxx - sx * sx
    if abs(det) < 1e-12:
        return None
    slope = (n * sxy - sx * sy) / det
    intercept = (sy - slope * sx) / n
    return math.exp(intercept), -slope           # A = C * t^(-n)


# A pace within this of a profile's own lap time IS that profile, and gets
# that profile's exact drag integral rather than the law's fit of it. The law
# reproduces the five curves to about 1.4 %, which is small but pointless to
# carry when the exact number is right there: without this, saying "117 Wh at
# base_210s pace" came back as 81.5 Wh for base_210s instead of the 81.9 Wh
# that was put in, and an anchor that does not reproduce its own measurement
# is a thing people rightly stop trusting.
EXACT_PACE_TOLERANCE = 0.01


def aero_for_lap_time(lap_time_s, profiles, law=None):
    """The drag integral a lap at this pace implies.

    A profile's own pace returns that profile's own integral, exactly. Any
    other pace goes through aero_law(), which is what lets a 252 s bench lap
    be costed at all.

    Falls back to the closest profile when the law cannot be fitted, which
    keeps a one-profile installation working instead of failing.
    """
    for p in profiles.values():
        if abs(p["lap_time_s"] - lap_time_s) <= EXACT_PACE_TOLERANCE * p["lap_time_s"]:
            return p["aero"]
    law = law or aero_law(profiles)
    if law is None:
        nearest = min(profiles.values(),
                      key=lambda p: abs(p["lap_time_s"] - lap_time_s))
        return nearest["aero"]
    C, n = law
    return C * float(lap_time_s) ** (-n)


# ── Drag from LAP TIME ALONE, with no speed curve at all ──────────────────── #
# Everything above needs a CSV: aero_integral() walks a profile's own v(d).
# The pit's strategy endpoint has no business reading those files -- and the
# car reports exactly two numbers per lap, how long it took and what it cost,
# which is enough on their own.
#
# Take the lap as driven at a steady v = L/t. Then A = integral of v^2 ds is
# just L^3/t^2, and the whole matrix follows from lap times.
#
# THIS IS A COARSER SHAPE THAN THE CURVES, AND IT BARELY MATTERS. Against the
# five CSVs the steady figure is 26-34 % low in absolute terms -- but the
# absolute scale is what the measurement fixes (b is fitted, not assumed), so
# only the SPREAD across profiles survives into the answer. The curves' drag
# rises as t^-2.31, the steady lap's as t^-2.00, and anchoring both at the same
# measured base lap (130.6 Wh at 210 s) the two matrices differ by:
#
#     fast_189s  144.0 -> 140.8 Wh      med_slow_220s  126.4 -> 126.6 Wh
#     base_210s  130.6 -> 130.6 Wh      slow_231s      122.9 -> 123.0 Wh
#
# 2 % at the fast end and nothing at the slow end, against a measurement whose
# own laps scatter by more than that. The CSV path stays for the offline tool,
# where the curves are there to be read anyway.
def aero_from_lap_time(lap_time_s, lap_length_m=LAP_M):
    """The drag integral a STEADY lap at this pace implies, in m^3/s^2."""
    t = float(lap_time_s)
    if t <= 0:
        raise ValueError("lap time must be positive")
    return lap_length_m ** 3 / (t * t)


def profiles_from_lap_times(entries, lap_length_m=LAP_M):
    """{key: {label, lap_time_s, aero}} from lap times only -- no CSV read.

    The same shape load_profiles() returns, so measurements_from_db(),
    fit_from_measurements() and matrix() take it unchanged. `entries` is any
    iterable of dicts carrying `key`, `label` and `lap_time_s` or
    `lap_time_min` -- constants.STRATEGIES is one.

    Entries without a usable lap time are dropped rather than guessed at: a
    profile whose pace nobody knows cannot be costed by a model that knows
    only pace.
    """
    out = {}
    for e in entries:
        key = e.get("key")
        secs = e.get("lap_time_s")
        if secs is None and e.get("lap_time_min") is not None:
            secs = float(e["lap_time_min"]) * 60.0
        if not key or not secs or secs <= 0:
            continue
        out[key] = {"label": e.get("label") or _label_for(key),
                    "lap_time_s": float(secs),
                    "aero": aero_from_lap_time(secs, lap_length_m)}
    return dict(sorted(out.items(), key=lambda kv: kv[1]["lap_time_s"]))


def ladder_from_anchor(rows, anchor_key, target_s, energy_wh,
                       aero_share=DEFAULT_AERO_SHARE, lap_length_m=LAP_M):
    """The whole matrix regenerated from ONE row somebody typed.

    `rows` is the matrix as it stands -- dicts carrying `key` and `target_s` --
    and it supplies the PACE LADDER, each row's lap time as a ratio of the
    anchor's. So a matrix spaced -10/-5/0/+5/+10 % stays spaced that way, and
    one spaced some other way keeps ITS spacing. Nothing here parses a label
    for a percentage.

    LAP TIMES SCALE BY THAT RATIO. Wh DOES NOT. Pace is a decision and scales
    however the crew spaced it; energy is physics and comes from the model this
    module exists for --

        E = a.L + b.A,  A = L^3/t^2   (aero_from_lap_time)

    -- with a and b fixed by the anchor row alone, which means the anchor
    reproduces exactly what was typed and every other row follows from it.
    A flat percentage on Wh instead would say rolling drag, bearings and
    drivetrain losses get cheaper when the driver slows down. They do not; they
    are paid per metre and simply take longer. Anchored on 285 s / 145 Wh at a
    1/3 drag share, a +10 % pace costs 136.6 Wh by this model against the
    130.5 Wh a flat ladder claims -- 4 race laps, in the direction that
    matters, since slowing down is the lever the crew actually pulls.

    ONE ROW CANNOT SEPARATE a and b, so `aero_share` supplies the second
    equation (see DEFAULT_AERO_SHARE). Anything measured across two real paces
    belongs in fit_from_measurements() instead, which assumes nothing.

    Returns [{key, target_s, energy_wh}] in the order given. Raises ValueError
    on an anchor that is not in the matrix, or numbers that cannot be a lap.
    """
    rows = list(rows)
    target_s, energy_wh = float(target_s), float(energy_wh)
    if target_s <= 0:
        raise ValueError("lap time must be positive")
    if energy_wh <= 0:
        raise ValueError("energy per lap must be positive")
    share = float(aero_share)
    if not 0.0 < share < 1.0:
        raise ValueError("aero share must be between 0 and 1, got %r" % aero_share)
    anchor = next((r for r in rows if r.get("key") == anchor_key), None)
    if anchor is None:
        raise ValueError("%r is not in the matrix" % (anchor_key,))
    base = anchor.get("target_s")
    if not base or float(base) <= 0:
        raise ValueError("the anchor row has no lap time to scale from")

    a_aero = aero_from_lap_time(target_s, lap_length_m)
    b = share * energy_wh / a_aero
    a = (1.0 - share) * energy_wh / lap_length_m
    out = []
    for r in rows:
        t = r.get("target_s")
        if not t or float(t) <= 0:
            continue
        new_t = target_s * (float(t) / float(base))
        out.append({"key": r["key"],
                    "target_s": round(new_t, 2),
                    "energy_wh": round(a * lap_length_m
                                       + b * aero_from_lap_time(new_t, lap_length_m), 1)})
    return out


def profile_pace_range(profiles):
    """(fastest, slowest) lap time on disk — outside it, the law extrapolates."""
    times = [p["lap_time_s"] for p in profiles.values()]
    return (min(times), max(times)) if times else (None, None)


# --------------------------------------------------------------------------- #
# 2. A measurement
# --------------------------------------------------------------------------- #
class Measurement:
    """One run: energy spent, distance covered, and the DRAG it was done at.

    `distance_m` is the load-bearing field, not `seconds`. See the module
    docstring: without it an energy figure cannot become a per-lap figure.

    `aero` is the other load-bearing field, and it is a number, not a profile
    name. A run is costed by the pace it was ACTUALLY driven at — for a real
    measured lap that is aero_for_lap_time(), not the CSV of whatever profile
    the pit had last sent. `profile_key` survives only as a label for the
    report; nothing is computed from it.
    """

    def __init__(self, energy_wh, distance_m, aero, seconds=None,
                 profile_key=None, note="", lap_source=None):
        self.energy_wh = float(energy_wh)
        self.distance_m = float(distance_m)
        self.aero = float(aero)
        self.seconds = None if seconds is None else float(seconds)
        self.profile_key = profile_key
        self.note = note
        self.lap_source = lap_source
        if self.distance_m <= 0:
            raise ValueError("distance must be positive — see --distance-m")
        if self.aero <= 0:
            raise ValueError("drag integral must be positive")

    @property
    def energy_per_lap_wh(self):
        """The measurement scaled to one full 4000 m lap.

        A lap cut by the odometer fallback is 4200 m, so this is not a no-op
        even when the source says "one lap".
        """
        return self.energy_wh * LAP_M / self.distance_m

    @property
    def implied_lap_time_s(self):
        """What lap time this run was going at, per 4000 m, or None.

        Printed next to the profile it is labelled with, because the two
        disagreeing is the single most likely way to get a wrong answer here:
        a run recorded at 257 s pace and filed under base_210s would put the
        whole matrix out by the difference. Since `aero` is taken from THIS
        number rather than from the label, the mismatch is a warning about the
        label, not an error in the maths.
        """
        if self.seconds is None:
            return None
        return self.seconds * LAP_M / self.distance_m


def measurement_from_duration(energy_wh, seconds, distance_m=None,
                              laps=None, profile_key="base_210s",
                              profiles=None):
    """Build a Measurement from what someone actually has in front of them.

    Exactly one of `distance_m` or `laps` should be given. If NEITHER is, the
    run is assumed to have been driven at the named profile's own pace and the
    distance is derived from it — which is a real assumption, is the only one
    this module makes on the caller's behalf, and is reported as `note` so it
    reaches the printout instead of hiding in here.

    The drag comes from the pace the numbers imply, via aero_for_lap_time(). So
    if you say 117 Wh in 5 minutes over 4000 m, this is costed as a 300 s lap —
    which it is — and NOT as whatever profile you picked from the list.
    """
    profiles = profiles or load_profiles()
    if profile_key not in profiles:
        raise ValueError("unknown profile %r — have %s"
                         % (profile_key, ", ".join(profiles)))
    if distance_m is None and laps is not None:
        distance_m = float(laps) * LAP_M
    note = ""
    if distance_m is None:
        if seconds is None:
            raise ValueError("give a duration, or a distance")
        pace = profiles[profile_key]["lap_time_s"]
        distance_m = float(seconds) * LAP_M / pace
        note = ("distance ASSUMED: no distance given, so the run is taken to be "
                "at %s pace (%.1f s/lap), giving %.0f m in %.0f s"
                % (profile_key, pace, distance_m, seconds))
    m = Measurement(energy_wh, distance_m, profiles[profile_key]["aero"],
                    seconds, profile_key, note)
    pace = m.implied_lap_time_s
    if pace:                       # a duration was given: cost it at ITS pace
        m.aero = aero_for_lap_time(pace, profiles)
    return m


# --------------------------------------------------------------------------- #
# 3. The fit
# --------------------------------------------------------------------------- #
class EnergyModel:
    """E_lap = a·L + b·A. `a` is Wh per metre, `b` is Wh per (m³/s²)."""

    def __init__(self, a, b, basis, aero_share_assumed=None):
        self.a, self.b = float(a), float(b)
        self.basis = basis                       # human text: what fixed it
        self.aero_share_assumed = aero_share_assumed

    def energy_wh(self, aero, lap_length_m=LAP_M):
        return self.a * lap_length_m + self.b * aero

    def aero_share(self, aero, lap_length_m=LAP_M):
        """What fraction of THAT lap's energy is drag, per this fit."""
        total = self.energy_wh(aero, lap_length_m)
        return (self.b * aero / total) if total else None

    @property
    def assumed(self):
        """True when an aero share had to be supplied rather than measured."""
        return self.aero_share_assumed is not None


def fit_from_measurements(measurements, profiles,
                          aero_share=DEFAULT_AERO_SHARE):
    """Solve a and b from one or more runs.

    JUDGED ON DRAG, NOT ON LABELS. Two runs count as independent when their
    `aero` differs, which is the thing the fit actually needs. Two laps both
    labelled base_210s but driven at 265 s and 510 s ARE two independent
    points — and two laps at the same pace under different profile names are
    not, however they were labelled.

    ONE independent pace: one equation, two unknowns, so `aero_share` supplies
    the second. The runs are averaged per-lap first.

    TWO OR MORE independent paces: both coefficients are solved by least
    squares and `aero_share` is ignored entirely. This is the answer to trust;
    everything else in this module exists to be useful before you have it.
    """
    if not measurements:
        raise ValueError("no measurements")

    # Paces closer together than this cannot separate rolling from drag: the
    # two columns of the fit are near-parallel and the solution is noise.
    spread = max(m.aero for m in measurements) / min(m.aero for m in measurements)
    if spread < 1.02:
        aero = sum(m.aero for m in measurements) / len(measurements)
        e_lap = sum(m.energy_per_lap_wh for m in measurements) / len(measurements)
        share = float(aero_share)
        if not 0.0 < share < 1.0:
            raise ValueError("aero share must be between 0 and 1, got %r"
                             % aero_share)
        b = share * e_lap / aero
        a = (1.0 - share) * e_lap / LAP_M
        basis = ("%d run%s, all at effectively one pace — aero share ASSUMED "
                 "at %.0f%%" % (len(measurements),
                                "" if len(measurements) == 1 else "s",
                                share * 100.0))
        return EnergyModel(a, b, basis, aero_share_assumed=share)

    # Two unknowns, >= 2 independent rows: ordinary least squares, written out
    # rather than pulled from numpy so this module stays importable anywhere
    # (collector machines do not all have it, and the sum has four terms).
    sxx = sxy = sxz = syy = syz = 0.0
    for m in measurements:
        x, y = LAP_M, m.aero
        z = m.energy_per_lap_wh
        sxx += x * x; sxy += x * y; syy += y * y
        sxz += x * z; syz += y * z
    det = sxx * syy - sxy * sxy
    if abs(det) < 1e-9:
        raise ValueError("these runs cannot separate rolling from aero — "
                         "their paces are too alike")
    a = (sxz * syy - syz * sxy) / det
    b = (syz * sxx - sxz * sxy) / det
    paces = sorted(m.implied_lap_time_s for m in measurements
                   if m.implied_lap_time_s)
    span = ("%.0f–%.0f s/lap" % (paces[0], paces[-1])) if paces else "mixed paces"
    basis = ("%d runs spanning %s — both coefficients FITTED, nothing assumed"
             % (len(measurements), span))
    return EnergyModel(a, b, basis)


def matrix(model, profiles, lap_length_m=LAP_M, with_laps=True, current=None):
    """[{key, label, lap_time_s, energy_wh, aero_share, _laps, ...}], fastest
    first.

    `_laps` is what a whole race on that profile comes to, and `_laps_now` the
    same for the energy figures constants.py holds TODAY — so the report can
    show what adopting this matrix actually changes. Both come from the pit's
    own engine; see race_laps().
    """
    rows = [{"key": k,
             "label": p["label"],
             "lap_time_s": p["lap_time_s"],
             "aero": p["aero"],
             "energy_wh": model.energy_wh(p["aero"], lap_length_m),
             "aero_share": model.aero_share(p["aero"], lap_length_m),
             "_laps": None, "_stops": None, "_final_soc": None,
             "_laps_now": None}
            for k, p in profiles.items()]
    if not with_laps:
        return rows
    for key, plan in race_laps(rows).items():
        for r in rows:
            if r["key"] == key:
                r["_laps"] = plan.get("Total Laps")
                r["_stops"] = plan.get("Pit Strategy")
                r["_final_soc"] = plan.get("Final SoC")
    # The same race on the numbers already in constants.py. Only for profiles
    # that HAVE one: a profile the Builder just wrote has no stored cost, and
    # inventing a comparison for it would be worse than leaving the cell empty.
    current = current_matrix() if current is None else current
    if current:
        was_rows = [dict(r, energy_wh=current[r["key"]]) for r in rows
                    if current.get(r["key"]) is not None]
        for key, plan in race_laps(was_rows).items():
            for r in rows:
                if r["key"] == key:
                    r["_laps_now"] = plan.get("Total Laps")
    return rows


def problems(model, rows):
    """Everything about this result that should stop someone using it."""
    out = []
    if model.a <= MIN_COEFFICIENT:
        out.append("the per-metre term came out <= 0: the fit is saying "
                   "distance is free. Needs runs further apart in pace.")
    if model.b <= MIN_COEFFICIENT:
        out.append("the aero term came out <= 0: the fit is saying speed is "
                   "free. Needs runs further apart in pace.")
    ordered = sorted(rows, key=lambda r: r["lap_time_s"])
    for x, y in zip(ordered, ordered[1:]):
        if y["energy_wh"] > x["energy_wh"]:
            out.append("%s (%.0f s) costs more than the faster %s (%.0f s) — "
                       "that is backwards" % (y["key"], y["lap_time_s"],
                                              x["key"], x["lap_time_s"]))
    return out


# --------------------------------------------------------------------------- #
# 4. Measurements straight out of the store
# --------------------------------------------------------------------------- #
# How far a lap's own pace may sit from the profile it is LABELLED with before
# the label is worth doubting. The maths does not care — drag comes from the
# real pace — but a lap 20 % off the profile the pit thought it was flying is
# usually a GPS trigger that never fired, and the crew should know.
LABEL_PACE_TOLERANCE = 0.10


def measurements_from_db(conn, profiles, recent_laps=60, min_energy_wh=0.1):
    """One Measurement per lap the car actually completed, costed at ITS pace.

    Reads db.laps_measured(), not db.lap_energy_by_strategy(): the latter
    answers "what did a lap on profile X cost", which is only the right
    question when the lap really was flown at X's pace. Here every lap brings
    its own time and its own distance, so:

      * a 4200 m odometer-forced lap is scaled to 4000 m rather than counted
        as a lap of the track, and
      * its drag comes from aero_for_lap_time() at the pace it really ran,
        not from the CSV of the profile the pit had last sent.

    That is what makes a bench session at 50 km/h contribute honestly instead
    of dragging the whole matrix down to bench numbers. It is also what lets a
    session with varied pace fit BOTH coefficients with nothing assumed —
    laps at 265 s and 510 s are two independent points, whatever they are
    labelled.

    One Measurement per LAP, not a median per profile: throwing eight laps at
    the least-squares fit is strictly more information than throwing one
    median at it, and the caller can drop the bad ones.
    """
    import db
    law = aero_law(profiles)
    out = []
    for lap in db.laps_measured(conn, recent_laps=recent_laps):
        e, t, d = lap["energy_wh"], lap["lap_time_s"], lap["distance_m"]
        if e is None or e <= min_energy_wh or not t or not d:
            continue
        pace = t * LAP_M / d
        note = "lap %d" % lap["lap"]
        if lap["lap_source"] and lap["lap_source"] != "gps":
            note += ", cut by %s not the GPS line" % lap["lap_source"]
        out.append(Measurement(e, d, aero_for_lap_time(pace, profiles, law),
                               t, lap["strategy"], note, lap["lap_source"]))
    return out


def label_warnings(measurements, profiles):
    """Laps whose real pace does not match the profile they are filed under."""
    out = []
    for m in measurements:
        pace, key = m.implied_lap_time_s, m.profile_key
        if not pace or key not in profiles:
            continue
        nominal = profiles[key]["lap_time_s"]
        if abs(pace - nominal) / nominal > LABEL_PACE_TOLERANCE:
            out.append("%s ran at %.0f s/lap but is filed under %s (%.0f s) — "
                       "costed at its real pace, but the label is wrong"
                       % (m.note or "a lap", pace, key, nominal))
    return out


def extrapolation_warnings(measurements, profiles):
    """Runs outside the pace range the profiles actually cover."""
    lo, hi = profile_pace_range(profiles)
    out = []
    for m in measurements:
        pace = m.implied_lap_time_s
        if pace and (pace < lo * 0.95 or pace > hi * 1.05):
            out.append("%s ran at %.0f s/lap, outside the %.0f–%.0f s the "
                       "profiles cover — its drag is EXTRAPOLATED"
                       % (m.note or "a run", pace, lo, hi))
    return out


# --------------------------------------------------------------------------- #
# 5. What the matrix means in laps
# --------------------------------------------------------------------------- #
# Wh per lap is the input to a decision, not the decision. The number the crew
# argues about is LAPS, and a cost that looks like a rounding error turns into
# tens of laps once the charge stops are planned around it.
#
# RUN THROUGH THE PIT'S OWN ENGINE, deliberately. This calls the same
# strategy_engine.calculate_all_strategies() the Strategy tab calls, with the
# same battery, the same 3-stop regulation cap and the same charging curve. A
# second lap-count model living in this tool would eventually disagree with the
# dashboard, and the crew would have no way to tell which one to believe.
#
# So the laps below are exactly what the Strategy tab WILL show once these
# energy numbers are in constants.py. That is the point of putting them here:
# you see the consequence before you commit to the cause.


def race_laps(rows, time_left_min=None, available_wh=None, current_lap=0,
              track_length_km=None):
    """{profile key: strategy row} for a whole race on each profile.

    Defaults are a full race from a full pack starting at lap 0 — the question
    this tool is for. The pit's own tab answers the mid-race version, from the
    real clock and the real SoC.

    Returns {} rather than raising if the engine cannot be imported, so a
    matrix is still printed on a machine without matplotlib.
    """
    try:
        import strategy_engine as se
    except Exception:                            # noqa: BLE001
        return {}
    time_left_min = se.RACE_DURATION_MIN if time_left_min is None else time_left_min
    available_wh = se.BATTERY_FULL_WH if available_wh is None else available_wh
    track_km = track_length_km or (LAP_M / 1000.0)

    # Keyed by LABEL on the way out of the engine, so feed it labels that are
    # unique. Two profiles sharing a label would otherwise collide silently.
    table, by_label = [], {}
    for r in rows:
        label = r["key"]
        by_label[label] = r["key"]
        table.append({"label": label,
                      "lap_time_min": r["lap_time_s"] / 60.0,
                      "energy_wh": r["energy_wh"]})
    try:
        out = se.calculate_all_strategies(time_left_min, available_wh,
                                          current_lap, table, track_km)
    except Exception:                            # noqa: BLE001
        return {}
    return {by_label[row["Label"]]: row for row in out if row["Label"] in by_label}


# --------------------------------------------------------------------------- #
# 5. Reporting
# --------------------------------------------------------------------------- #
def render(model, rows, measurements, current=None):
    """The whole result as text. Shared by the CLI and the Streamlit app so the
    two can never describe the same numbers differently."""
    L = []
    L.append("MEASUREMENTS")
    for m in measurements:
        pace = m.implied_lap_time_s
        L.append("  %-16s %8.1f Wh over %7.0f m  ->  %6.1f Wh/lap%s"
                 % (m.profile_key or "run", m.energy_wh, m.distance_m,
                    m.energy_per_lap_wh,
                    "" if pace is None else "   at %.0f s/lap" % pace))
        if m.note:
            L.append("      %s" % m.note)

    L.append("")
    L.append("FIT: %s" % model.basis)
    L.append("  per metre  a = %.6f Wh/m   -> %.1f Wh a lap, whatever the pace"
             % (model.a, model.a * LAP_M))
    L.append("  drag       b = %.3e Wh/(m^3/s^2)" % model.b)
    if model.assumed:
        L.append("  ^ the split between those two is the ASSUMPTION, not a "
                 "measurement. Two laps on two different profiles remove it.")
    L.append("")
    head = "  %-16s %7s %10s %7s %7s %10s" % (
        "profile", "lap s", "Wh/lap", "aero", "LAPS", "stops")
    if current:
        head += " %9s %8s %7s" % ("Wh now", "change", "laps now")
    L.append(head)
    best = max((r["_laps"] or 0) for r in rows) if rows else 0
    for r in sorted(rows, key=lambda r: r["lap_time_s"]):
        n = r["_laps"]
        line = "  %-16s %7.1f %10.1f %6.0f%% %7s %10s" % (
            r["key"], r["lap_time_s"], r["energy_wh"],
            100.0 * (r["aero_share"] or 0.0),
            ("%d%s" % (n, " *" if n and n == best else "")) if n else "?",
            r["_stops"] or "?")
        if current:
            was = current.get(r["key"])
            line += ((" %9.1f %8.1f" % (was, r["energy_wh"] - was))
                     if was is not None else " %9s %8s" % ("-", "-"))
            line += " %7s" % (r["_laps_now"] if r["_laps_now"] else "-")
        L.append(line)
    if best:
        L.append("  * most laps. Simulated over %.0f h from a full pack, "
                 "through the pit's own strategy engine." % (RACE_MIN / 60.0))
    for title, items in (
            ("CHECK THE LABELS", label_warnings(measurements, profiles_of(rows))),
            ("EXTRAPOLATED", extrapolation_warnings(measurements, profiles_of(rows))),
            ("PROBLEMS - do not use this matrix", problems(model, rows))):
        if items:
            L.append("")
            L.append(title + ":")
            for it in items:
                L.append("  * %s" % it)
    return chr(10).join(L)


def profiles_of(rows):
    """The {key: {...}} shape the warning helpers want, back out of `rows`."""
    return {r["key"]: {"label": r["label"], "lap_time_s": r["lap_time_s"],
                       "aero": r["aero"]} for r in rows}


def current_matrix():
    """{key: energy_wh} as constants.py holds it now, for the change column."""
    try:
        import constants as C
        return {k: v.get("energy_wh") for k, v in C.PROFILE_MATRIX.items()
                if v.get("energy_wh") is not None}
    except Exception:                            # noqa: BLE001
        return {}


# --------------------------------------------------------------------------- #
# 6. CLI
# --------------------------------------------------------------------------- #
def _cli(argv=None):
    ap = argparse.ArgumentParser(
        description="Cost every speed profile from one run the car really did. "
                    "Prints; never writes.")
    ap.add_argument("--wh", type=float, help="energy the run used, in Wh")
    ap.add_argument("--minutes", type=float, help="how long the run lasted")
    ap.add_argument("--seconds", type=float, help="how long the run lasted")
    ap.add_argument("--distance-m", type=float,
                    help="how far the car went. THE ONE THAT MATTERS — without "
                         "it the pace has to be assumed")
    ap.add_argument("--laps", type=float, help="distance, as a lap count")
    ap.add_argument("--profile", default="base_210s",
                    help="which profile the run was driven on (default: base_210s)")
    ap.add_argument("--aero-share", type=float, default=DEFAULT_AERO_SHARE,
                    help="fraction of the anchor lap that is drag; ignored once "
                         "two profiles have been measured (default: %.2f)"
                         % DEFAULT_AERO_SHARE)
    ap.add_argument("--from-db", action="store_true",
                    help="take the measurements from telemetry.db instead — "
                         "assumes nothing, needs laps actually driven")
    ap.add_argument("--db", help="path to telemetry.db for --from-db")
    ap.add_argument("--from-matrix", action="store_true",
                    help="cost the profiles from constants.PROFILE_MATRIX lap "
                         "times instead of reading their CSVs (a steady lap at "
                         "that pace) - the pit's own basis, and the only one "
                         "available when the curves no longer describe the car")
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args(argv)

    if args.self_check:
        return _self_check()

    profiles = {} if args.from_matrix else load_profiles()
    if not profiles:
        # No CSVs (or --from-matrix): lap time and Wh are enough. See
        # aero_from_lap_time() for what is given up, which is about 2 %.
        try:
            import constants as C
            profiles = profiles_from_lap_times(C.STRATEGIES)
        except Exception as exc:                 # noqa: BLE001
            print("no profiles on disk and no matrix to fall back on (%s)" % exc)
            return 1
        if profiles:
            print("costed from LAP TIMES, no speed curves read "
                  "(constants.PROFILE_MATRIX)")
    if not profiles:
        print("no profiles on disk — nothing to cost")
        return 1

    if args.from_db:
        import sqlite3
        from contextlib import closing
        path = args.db
        if not path:
            from pit_config import SQLITE_PATH
            path = SQLITE_PATH
        with closing(sqlite3.connect("file:%s?mode=ro" % path, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            ms = measurements_from_db(conn, profiles)
        if not ms:
            print("no laps with an energy figure in %s — drive some, or pass "
                  "--wh/--minutes instead" % path)
            return 1
    else:
        if args.wh is None:
            ap.error("give --wh with --minutes/--seconds, or use --from-db")
        secs = args.seconds if args.seconds is not None else (
            args.minutes * 60.0 if args.minutes is not None else None)
        if secs is None and args.distance_m is None and args.laps is None:
            ap.error("give --minutes/--seconds, or a --distance-m/--laps")
        ms = [measurement_from_duration(args.wh, secs, args.distance_m,
                                        args.laps, args.profile, profiles)]

    model = fit_from_measurements(ms, profiles, args.aero_share)
    rows = matrix(model, profiles)
    print(render(model, rows, ms, current_matrix()))
    print("\nNothing was written. Put these into constants.PROFILE_MATRIX by "
          "hand, or through the Profile Builder's Save button.")
    return 1 if problems(model, rows) else 0


# --------------------------------------------------------------------------- #
# 7. Self-check
# --------------------------------------------------------------------------- #
def _self_check():
    """Proves the model against laps whose true cost is known, because it was
    invented — the only way to check a fit without a car."""
    ok = True

    def check(name, got, want, tol):
        nonlocal ok
        good = abs(got - want) <= tol
        ok = ok and good
        print("  %-52s %-8s got %.3f want %.3f" %
              (name, "ok" if good else "FAIL", got, want))

    profiles = load_profiles()
    print("profiles on disk: %d" % len(profiles))
    for k, p in profiles.items():
        print("  %-16s %7.1f s   aero %12.0f" % (k, p["lap_time_s"], p["aero"]))
    law = aero_law(profiles)
    print("drag law: A = %.3e * t^-%.3f" % law)
    print()

    def at(key, energy_wh):
        """A perfectly measured 4000 m lap of one profile."""
        return Measurement(energy_wh, LAP_M, profiles[key]["aero"],
                           profiles[key]["lap_time_s"], key)

    # 1. A known model, measured perfectly on two profiles, must come back.
    print("round trip: invent a car, 'measure' two of its laps, refit")
    a_true, b_true = 0.012, 8.0e-6
    keys = list(profiles)
    ms = [at(k, a_true * LAP_M + b_true * profiles[k]["aero"])
          for k in (keys[0], keys[-1])]
    m = fit_from_measurements(ms, profiles)
    check("refit recovers a", m.a, a_true, 1e-9)
    check("refit recovers b", m.b * 1e6, b_true * 1e6, 1e-6)
    check("and the base lap's cost", m.energy_wh(profiles["base_210s"]["aero"]),
          a_true * LAP_M + b_true * profiles["base_210s"]["aero"], 1e-9)
    print()

    # 2. One measurement + an assumed share must honour both the total and the
    #    share, exactly. This is the path every hand-typed run takes.
    print("single run: the anchor lap and the assumed share are both honoured")
    m1 = fit_from_measurements([at("base_210s", 81.9)], profiles, aero_share=0.4)
    base_aero = profiles["base_210s"]["aero"]
    check("anchor reproduces the measurement", m1.energy_wh(base_aero), 81.9, 1e-9)
    check("anchor's aero share is the one asked for",
          m1.aero_share(base_aero), 0.4, 1e-9)
    print()

    # 3. Distance, not time, is what sets Wh/lap. The module docstring's claim,
    #    as a test: the same 117 Wh over the same 5 minutes, three distances.
    print("117 Wh in 5 min is three different matrices, by distance alone")
    for laps, want in ((1.0, 117.0), (300.0 / 210.0, 81.9), (2.0, 58.5)):
        got = Measurement(117.0, laps * LAP_M, base_aero, 300.0).energy_per_lap_wh
        check("  %.2f laps -> Wh/lap" % laps, got, want, 0.1)
    print()

    # 4. The drag law must reproduce the profiles it was fitted from, or it has
    #    no business costing a lap that is not one of them.
    print("the drag law reproduces the curves it was fitted from")
    worst = max(abs(aero_for_lap_time(p["lap_time_s"], profiles, law) / p["aero"] - 1.0)
                for p in profiles.values())
    check("worst profile reproduced to within 3%", worst, 0.0, 0.03)
    check("slower lap => less drag",
          aero_for_lap_time(260.0, profiles, law) < aero_for_lap_time(210.0, profiles, law),
          True, 0)
    print()

    # 5. A lap driven at a pace no profile covers is costed at ITS pace, and
    #    flagged. This is the odometer-cut bench lap, which is what the store
    #    actually holds.
    print("a 4200 m bench lap at 265 s is not a lap of base_210s")
    bench = Measurement(149.0, 4200.0, aero_for_lap_time(265.3 * LAP_M / 4200.0,
                                                         profiles, law),
                        265.3, "base_210s", "lap 2", "odometer")
    check("scaled to 4000 m", bench.energy_per_lap_wh, 149.0 * 4000 / 4200, 0.01)
    check("its real pace", bench.implied_lap_time_s, 252.7, 0.5)
    check("drag below the slowest profile's",
          bench.aero < profiles["slow_231s"]["aero"], True, 0)
    check("the wrong label is flagged", len(label_warnings([bench], profiles)) == 1,
          True, 0)
    check("the extrapolation is flagged",
          len(extrapolation_warnings([bench], profiles)) == 1, True, 0)
    print()

    # 6. The ladder-vs-model claim in the docstring, checked rather than
    #    asserted: at a 1/3 share the fast side agrees and the slow side does not.
    print("at a 1/3 aero share, this model vs the +-5/+-10%% ladder")
    m3 = fit_from_measurements([at("base_210s", 80.0)], profiles,
                               aero_share=DEFAULT_AERO_SHARE)
    ladder = {"fast_189s": 88.0, "med_fast_199s": 84.0, "base_210s": 80.0,
              "med_slow_220s": 76.0, "slow_231s": 72.0}
    for k, want in ladder.items():
        if k not in profiles:
            continue
        got = m3.energy_wh(profiles[k]["aero"])
        print("  %-16s model %6.2f   ladder %6.2f   diff %+5.2f Wh"
              % (k, got, want, got - want))
    check("fast side agrees with the ladder (< 1 Wh)",
          abs(m3.energy_wh(profiles["fast_189s"]["aero"]) - 88.0), 0.0, 1.0)
    check("slow side is DEARER than the ladder (> 2 Wh)",
          m3.energy_wh(profiles["slow_231s"]["aero"]) - 72.0 > 2.0, True, 0)
    print()

    # 7. A fit that slopes the wrong way must be caught, not printed.
    print("a backwards result is refused")
    bad = [at(keys[0], 60.0), at(keys[-1], 90.0)]
    mb = fit_from_measurements(bad, profiles)
    found = problems(mb, matrix(mb, profiles))
    check("problems() flags it", len(found) > 0, True, 0)
    for f in found[:2]:
        print("      %s" % f)

    print()
    print("all energy-model checks passed" if ok else "FAILURES above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_cli())
