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
# onto sys.path (see the top). Importing `limits` directly from pit_dashboard.py
# would work only because constants happens to be imported first -- an invisible
# ordering dependency waiting to bite.
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
    # Solar charge current. Carries only a full_scale (no warn/crit) so the pit
    # tile and the driver's SOLAR IN gauge share one arc scale.
    SOLAR_CURRENT,
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
# everywhere instead of the target it was once named after.
_BUILTIN_STRATEGY_META = {
    "fast_189s":     ("Fast (-10%)",    88.0),
    "med_fast_199s": ("Med-Fast (-5%)", 84.0),
    "base_210s":     ("Base (210s)",    80.0),
    "med_slow_220s": ("Med-Slow (+5%)", 76.0),
    "slow_231s":     ("Slow (+10%)",    72.0),
}
DEFAULT_STRATEGY_KEY = "base_210s"

# Exactly what the five hardcoded entries used to be, to the digit. Returned
# whenever the profiles cannot be read, so this module can never fail to import
# — collector.py and export.py import it too, and neither has any business
# crashing because a CSV is malformed.
_FALLBACK_STRATEGIES = [
    {"key": "fast_189s",     "label": "Fast (-10%)",    "lap_time_min": 3.150, "energy_wh": 88.0},
    {"key": "med_fast_199s", "label": "Med-Fast (-5%)", "lap_time_min": 3.325, "energy_wh": 84.0},
    {"key": "base_210s",     "label": "Base (210s)",    "lap_time_min": 3.500, "energy_wh": 80.0},
    {"key": "med_slow_220s", "label": "Med-Slow (+5%)", "lap_time_min": 3.675, "energy_wh": 76.0},
    {"key": "slow_231s",     "label": "Slow (+10%)",    "lap_time_min": 3.850, "energy_wh": 72.0},
]

_SIDECAR_PATH = os.path.join(_REPO_ROOT, "profiles", "profiles.json")


def _sidecar_meta():
    """{key: {label, energy_wh}} written by profile_builder.py. Optional."""
    try:
        import json
        with open(_SIDECAR_PATH, encoding="utf-8") as fh:
            return json.load(fh).get("categories") or {}
    except Exception:
        return {}


def load_strategies():
    """Every speed profile on disk, newest information first.

    lap_time_min is ALWAYS integrated from the curve (SpeedProfile.lap_time_s),
    never a stored claim, so the Strategy tab's consumption matrix tells the
    truth the moment a profile is replaced by a measured lap.

    Never raises. On any problem the five original entries are returned
    unchanged, which is also exactly what this produces when the five original
    CSVs are the only ones present.
    """
    try:
        import speed_profile
        import track
        found = speed_profile.available_profiles()
        if not found:
            return list(_FALLBACK_STRATEGIES)
        side = _sidecar_meta()

        out = []
        for key, path in found.items():
            prof = speed_profile.load_csv(path, name=key,
                                          lap_length_m=track.TRACK_LENGTH_METERS)
            label, energy = _BUILTIN_STRATEGY_META.get(key, (None, None))
            meta = side.get(key) or {}
            label = meta.get("label") or label or key.replace("_", " ").title()
            energy = meta.get("energy_wh", energy)
            out.append({"key": key, "label": label,
                        "lap_time_min": prof.lap_time_s() / 60.0,
                        "energy_wh": energy,
                        # True when nobody has said what this profile costs, so
                        # the matrix can mark the number as unknown rather than
                        # printing a confident dash-free zero.
                        "energy_estimated": energy is None})
        out.sort(key=lambda s: s["lap_time_min"])
        return out or list(_FALLBACK_STRATEGIES)
    except Exception as exc:     # noqa: BLE001 - importable above all else
        print(f"⚠️ speed profiles unreadable ({exc}); using the built-in five")
        return list(_FALLBACK_STRATEGIES)


STRATEGIES = load_strategies()
STRATEGY_BY_LABEL = {s["label"]: s for s in STRATEGIES}
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
