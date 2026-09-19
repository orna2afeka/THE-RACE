# constants.py
# Centralized configuration and static values for the Afeka Pit Wall dashboard.

# =========================
# PHYSICAL / VEHICLE CONSTANTS
# =========================
# Drivetrain numbers are NOT defined here any more. They live in drivetrain.py
# at the repo root, shared with the car, because the pit and the driver HUD had
# drifted onto different gear ratios and wheel sizes and were reporting speeds
# 3.3 % apart for the same CAN frame. Re-exported below so every existing
# `from constants import GEAR_RATIO, ...` keeps working unchanged.
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from drivetrain import (          # noqa: E402  (path set up immediately above)
    GEAR_RATIO,
    # NOT the gear ratio, despite once being the same number - see the note in
    # drivetrain. db.py needs it to normalise the controller's speed field.
    CONTROLLER_SPEED_DIVISOR,
    CONTROLLER_SPEED_DIVISOR_LEGACY,
    RPM_REPORT_SCALE,
    WHEEL_CIRCUMFERENCE_METERS,
    TIRE_DIAMETER_METERS,
    MOTOR_POLE_PAIRS,
    speed_kmh,
)

# Circuit geometry is shared with the car too (track.py at the repo root), for
# the same reason as the drivetrain: the lap length was written out separately
# here and in SolarRace_OS/main.py, so the two ends could disagree about how
# long a lap is.
from track import (                # noqa: E402  (path set up above)
    TRACK_LENGTH_METERS,
    FINISH_LINE_LAT,
    FINISH_LINE_LON,
)
# Alarm thresholds, shared with the driver HUD (limits.py at the repo root) so
# the two screens never disagree about whether a reading is a warning.
#
# These are re-exported through this module rather than imported straight from
# `limits` by every consumer, because THIS file is what bootstraps the repo root
# onto sys.path (see the top). Importing `limits` directly from a module in
# this folder would work only because constants happens to be imported first --
# an invisible ordering dependency waiting to bite.
#
# The six loose MOTOR_TEMP_WARN/CRIT-style scalars and temp_condition() are gone.
# They were replaced by Threshold objects and one classify() call, because
# handing out bare numbers is exactly how three separate copies of the same
# comparison came to exist (one here, one in the HUD, one inlined in a tile).
from limits import (                # noqa: E402  (path set up above)
    NORMAL, WARNING, CRITICAL,
    TIER_COLOURS,
    classify,
    MOTOR_TEMP, CTRL_TEMP, CELL_TEMP,
    # A failed/disconnected thermistor reports a nonsense negative rather than
    # nothing at all — gate every per-cell temperature through this before
    # displaying it. See its docstring for why it is one-sided.
    plausible_cell_temp,
    # DS003 cell naming: C_A1..C_A13 / C_B1..C_B13, shared with the driver HUD
    # so one sensor can never carry two different names across the two screens.
    CELL_COUNT, cell_temp_label,
    THERMISTOR_GROUP_RANGES, THERMISTOR_GROUP_NAMES,
    THERMISTOR_GROUPED_COUNT, THERMISTOR_ID_MAX,
    SOC, PACK_VOLTAGE, BATT_CURRENT,
    MOTOR_CURRENT, POWER, SPEED,
    CELL_VOLTAGE,
)

TARGET_LAP_TIME_MIN = 3.5

# =========================
# STRATEGY MATRIX
# =========================
# What the Strategy tab plots and what the remote selector offers. `key` must
# match profiles/<key>.csv exactly, because that string is what the pit sends
# and the car looks up.
#
# THE LIST COMES FROM DISK NOW, not from a literal here. The car has always
# loaded whatever CSVs exist (speed_profile.load_all is a directory scan), while
# the pit only knew these five — so a profile measured at the track and written
# by profile_builder.py was invisible to the dropdown and therefore unreachable
# in a race, no matter that the car was holding it in memory ready to use.
#
# Only the things that CANNOT be derived from a speed curve are kept here. The
# label is a human name and energy_wh needs a vehicle model, so both are stored;
# lap time is a property of the curve itself and is always computed from it, so
# a profile rebuilt from a real lap immediately reports its real lap time
# everywhere instead of the target it was once named after. target_s is the
# builder's column for the profile, which is also the fallback lap time below.
#
# THE BLOCK BETWEEN THE MARKERS IS REWRITTEN by the Save button in the Speed
# Profile Builder (profile_manage.write_saved_matrix). Editing it by hand is
# fine — keep it a plain literal, and keep the two marker lines.
#
# ONE PROFILE NOW, AND IT IS A RACE LAP: lap33_290s is lap 33 of the race on
# 2026-09-19 exactly as driven (SolarRace_OS/lap 33.xlsx -- 285.6 s on the
# clock, NORMAL mode, 101.1 Wh, which is the energy_wh below and is MEASURED,
# not fitted). THE ROWS OF THAT WORKBOOK WERE ARRANGED BY THE CREW so that row 1
# is the start/finish line: GPS was down and laps were being cut by hand, so
# the car's own lap distance sat ~630 m out, and a build that trusted it put
# T1 at 39 km/h and T8/9 flat out. It replaced dor_265s/280s/300s, which were one practice in-lap
# scaled three ways and sat ~100 m out of phase at T12. Rebuild it with
#   python tools/build_dor_profiles.py --as-driven lap33_290s --file-order --axis track --verify
# More profiles are added from the matrix editor, not by hand here.
#
# (History) THE PREVIOUS THREE CAME FROM A LAP THE CAR DROVE, not from a desk model. They replace
# the five fast_189s..slow_231s rows, which were scaled from Pit_Dashboard/
# 210s.xlsx and commanded 92 km/h down the main straight — a target this car has
# never reached, so the HUD's target line and its Δ were noise all lap.
# tools/build_dor_profiles.py rebuilds them from SolarRace_OS/dor 17.xlsx
# (Zolder, 2026-09-18); run it with --verify after replacing that lap.
# energy_wh is energy_model fitted to that lap's own 116.3 Wh, and target_s is
# what each curve integrates to. Old CSVs are in profiles/_backup/.
# >>> PROFILE MATRIX >>>
PROFILE_MATRIX = {
    "lap33_290s": {"label": "Base", "energy_wh": 102.7, "target_s": 283.0},
}
# <<< PROFILE MATRIX <<<
DEFAULT_STRATEGY_KEY = "lap33_290s"

# Returned whenever the profiles cannot be read, so this module can never fail
# to import — collector.py and export.py import it too, and neither has any
# business crashing because a CSV is malformed.
_FALLBACK_STRATEGIES = [
    {"key": k, "label": m["label"], "lap_time_min": m["target_s"] / 60.0,
     "energy_wh": m.get("energy_wh")}
    for k, m in sorted(PROFILE_MATRIX.items(), key=lambda kv: kv[1]["target_s"])
]


# How far a profile's lap time must sit from the base before the label says so
# at all. Under this it is the base pace by another name, and "(+0%)" is noise.
LABEL_PCT_FLOOR = 0.5


def display_label(name, target_s, base_s):
    """"Fast" at 256.5 s against a 285 s base -> "Fast (-10%)".

    THE SUFFIX IS DERIVED, EVERY TIME, and the store holds only the name. It
    used to be part of the stored label -- "Base (285s)", "Fast (-10%)" -- text
    that was true when somebody typed it and silently wrong afterwards: the
    matrix carried a row reading "Base (285s)" while the profiles behind it
    lapped in 210, and now that the crew can edit lap times mid-race from the
    Strategy tab a baked-in number would go stale the moment they did.

    Percent, not the lap time itself: the lap time is already a column of its
    own in the matrix, and what the label is for is saying where this row sits
    relative to the pace the strategy is built around.
    """
    if not target_s or not base_s:
        return name
    pct = (float(target_s) / float(base_s) - 1.0) * 100.0
    return name if abs(pct) < LABEL_PCT_FLOOR else "%s (%+.0f%%)" % (name, pct)


def load_strategies():
    """The consumption matrix: one row per profile, fastest first.

    TWO NUMBERS MAKE A ROW -- a lap time and a Wh -- and both are read from
    PROFILE_MATRIX above. No speed curve is opened to build this list.

    That is a reversal, and the reason is the car. lap_time_min used to be
    integrated from the profile's own CSV and the stored `target_s` ignored on
    principle ("never a stored claim"), which is right while the curves
    describe what the car does. They no longer do: the CSVs command 189-231 s
    laps and the car is lapping near 285. Integrating them made the Strategy
    tab plan a race at a pace nothing was going to run, and no amount of
    measured energy could fix a row whose lap time was wrong.

    A profile on disk that is NOT in the matrix still gets its lap time from
    its curve -- that is the Profile Builder's output, whose CSV is the only
    thing anybody has said about it. So the builder keeps working, and the
    curves stay useful for the driver's target speed (speed_profile is still
    what the car flies, and 210s.xlsx still drives the live track readout).

    Never raises. On any problem the matrix's own five rows are returned.
    """
    base_s = (PROFILE_MATRIX.get(DEFAULT_STRATEGY_KEY) or {}).get("target_s")
    out = []
    for key, meta in PROFILE_MATRIX.items():
        target = meta.get("target_s")
        if not target:
            continue
        energy = meta.get("energy_wh")
        out.append({"key": key,
                    "label": display_label(
                        meta.get("label") or key.replace("_", " ").title(),
                        target, base_s),
                    # The stored name, without the derived suffix: what the
                    # matrix editor writes back, so a "(-10%)" is never baked
                    # into the store and re-derived from itself.
                    "name": meta.get("label") or key.replace("_", " ").title(),
                    "lap_time_min": float(target) / 60.0,
                    "energy_wh": energy,
                    # True when nobody has said what this profile costs, so
                    # the matrix can mark the number as unknown rather than
                    # printing a confident dash-free zero.
                    "energy_estimated": energy is None})
    try:
        import speed_profile
        import track
        for key, path in speed_profile.available_profiles().items():
            if key in PROFILE_MATRIX:
                continue
            prof = speed_profile.load_csv(path, name=key,
                                          lap_length_m=track.TRACK_LENGTH_METERS)
            name = key.replace("_", " ").title()
            out.append({"key": key, "label": name, "name": name,
                        "lap_time_min": prof.lap_time_s() / 60.0,
                        "energy_wh": None, "energy_estimated": True})
    except Exception as exc:     # noqa: BLE001 - importable above all else
        print(f"⚠️ speed profiles unreadable ({exc}); matrix rows only")
    out.sort(key=lambda s: s["lap_time_min"])
    return out or list(_FALLBACK_STRATEGIES)


# Laps the car must drive on a profile before the Strategy tab uses their
# median energy instead of the stored Wh/lap estimate (Pit_Web/api.py).
MIN_LAPS_FOR_MEASURED = 2

STRATEGIES = load_strategies()
STRATEGY_BY_LABEL = {s["label"]: s for s in STRATEGIES}


def set_profile_matrix(matrix):
    """Adopt a new matrix IN THIS PROCESS, after it has been written to disk.

    profile_manage.write_saved_matrix() puts the new numbers in constants.py,
    where they are read at import -- which used to mean the pit web server kept
    serving the old matrix until somebody restarted it. Mid-race that is not a
    delay, it is a wrong plan on the screen while the crew believes they have
    changed it.

    EVERYTHING IS MUTATED IN PLACE, never rebound, so a module that did
    `from constants import STRATEGIES` still sees the change.

    This does not write anything. Call it after the write succeeds.
    """
    PROFILE_MATRIX.clear()
    PROFILE_MATRIX.update({str(k): dict(v) for k, v in matrix.items()})
    STRATEGIES[:] = load_strategies()
    STRATEGY_BY_LABEL.clear()
    STRATEGY_BY_LABEL.update({s["label"]: s for s in STRATEGIES})
    return STRATEGIES
DATA_STALE_AFTER_S = 10.0   # latest sample older than this => collector likely down

# =========================
# TRACK SECTIONS
# =========================
SECTION_NAMES = {
    1: "Start / Turn 1", 2: "Turns 2 & 3",  3: "Uphill Straight",
    4: "Chicane",        5: "Middle Straight", 6: "Hairpins",
    7: "Back Straight",  8: "Slow Corner",   9: "Final Chicane",
}
SECTION_TURN_LABELS = {
    1: "T1", 2: "T2-3", 3: "T4", 4: "T5-6", 5: "T7",
    6: "T8-9", 7: "T10-11", 8: "T12", 9: "T15-16",
}

# =========================
# RISK LEVELS & COLORS
# =========================
# Spelled with the SHARED tier names, not the old "warn"/"crit" shorthand. The
# shorthand was a silent-failure trap in one direction: a "warn" string handed to
# anything expecting a tier fell through to "normal" and simply lost the colour,
# with no error to notice. Using the imported constants means a future
# divergence is a NameError at import instead.
#
# These stay a SEPARATE palette from TIER_COLOURS on purpose. Section risk is a
# static property of the circuit -- "this corner is dangerous" -- not a live
# measurement breach. Making them pixel-identical would teach the crew that
# amber means one thing when it means two.
SECTION_RISK = {
    1: NORMAL,  2: NORMAL, 3: NORMAL,
    4: WARNING, 5: NORMAL, 6: CRITICAL,
    7: NORMAL,  8: CRITICAL, 9: WARNING,
}
SECTION_COLORS = {NORMAL: "#00FFCC", WARNING: "#FF9900", CRITICAL: "#FF4444"}

# =========================
# FAULT / ERROR-CODE DECODING
# =========================
# These bit tables mirror the on-car parsers (SolarRace_OS/modules/bms_parser.py
# and mms_parser.py). The car normally sends the already-decoded label strings
# (bms_protections / mms_alerts), and the pit just shows those. But when that
# string is missing — older rows logged before the car emitted labels, a
# firmware mismatch, or a payload dropped in transit — the pit can decode the
# raw *_error_code bitmask itself, so a fault still reads in English ("Pack
# Overvoltage") instead of a bare "0x4". Keep these in sync with the car.

# JBD BMS protection bits (ID 0x102) -> bms_error_code
BMS_PROTECTION_BITS = [
    (0,  "Cell Overvoltage"),
    (1,  "Cell Undervoltage"),
    (2,  "Pack Overvoltage"),
    (3,  "Pack Undervoltage"),
    (4,  "Charge Over-temp"),
    (5,  "Charge Under-temp"),
    (6,  "Discharge Over-temp"),
    (7,  "Discharge Under-temp"),
    (8,  "Charge Overcurrent"),
    (9,  "Discharge Overcurrent"),
    (10, "Short Circuit Protection"),
    (11, "IC Error (Front-end)"),
]

# SiliXcon MMS error-word bits (ID 0x600, bytes 6-7) -> mms_error_code
MMS_ERROR_BITS = [
    (0, "Over-voltage Error"),    (1, "Under-voltage Error"),
    (2, "Controller Over-temp"),  (3, "Motor Over-temp"),
    (4, "Over-current Fault"),    (5, "Hall Sensor Fault"),
    (6, "Communication Fault"),   (7, "Hardware Fault"),
    (8, "Throttle Error"),        (9, "Phase Imbalance"),
]


def decode_error_bits(code, bit_defs):
    """Comma-joined English labels for the set bits in `code`.

    Returns "" when `code` is None/non-numeric/zero, or when it's a value whose
    set bits aren't in `bit_defs` (so the caller can fall back to showing the
    raw hex for genuinely unknown codes)."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return ""
    if not code:
        return ""
    return ", ".join(label for bit, label in bit_defs if code & (1 << bit))


# The team's drivers, for every place a name is CHOSEN rather than typed: the
# per-lap driver dropdown today. One list, served to the browser through
# /api/config, because a name typed "ido" in one place and picked as "Ido" in
# another is two drivers in the workbook's Driver column.
DRIVERS = ["Ido", "Dor", "Amit", "Omer", "Guy", "Tal"]
