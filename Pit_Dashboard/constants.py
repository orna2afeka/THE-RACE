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
    SOC, PACK_VOLTAGE, BATT_CURRENT,
    MOTOR_CURRENT, POWER, SPEED,
)

TARGET_LAP_TIME_MIN = 3.5

# =========================
# STRATEGY MATRIX
# =========================
# The five race strategies: what the Strategy tab plots, what the remote
# selector offers, and what tools/generate_profiles.py builds a speed profile
# for. Hoisted here from a literal inside _strategy_fragment so all three name
# the same things — `key` must match the generated profiles/<key>.csv exactly,
# because that string is what the pit sends and the car looks up.
STRATEGIES = [
    {"key": "fast_189s",     "label": "Fast (-10%)",    "lap_time_min": 3.150, "energy_wh": 88.0},
    {"key": "med_fast_199s", "label": "Med-Fast (-5%)", "lap_time_min": 3.325, "energy_wh": 84.0},
    {"key": "base_210s",     "label": "Base (210s)",    "lap_time_min": 3.500, "energy_wh": 80.0},
    {"key": "med_slow_220s", "label": "Med-Slow (+5%)", "lap_time_min": 3.675, "energy_wh": 76.0},
    {"key": "slow_231s",     "label": "Slow (+10%)",    "lap_time_min": 3.850, "energy_wh": 72.0},
]
STRATEGY_BY_LABEL = {s["label"]: s for s in STRATEGIES}
DEFAULT_STRATEGY_KEY = "base_210s"
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
