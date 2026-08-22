"""
drivetrain.py — the ONE definition of how motor RPM becomes road speed
======================================================================
Imported by the car (driver HUD + odometer) and by the pit (live tiles, history
charts, Excel export). Everything downstream of the CAN bus that turns rev
counts into metres or km/h comes through here.

WHY THIS FILE EXISTS
The speed maths used to be written out by hand in five places, and they had
drifted apart:

    driver HUD        (rpm / 5.0)  * 60 * (2π × 0.279) / 1000
    pit live tile     rpm * 1.727 * 60 / (1000 * 5.09)
    pit history chart rpm * 1.727 * 60 / (1000 * 5.09)
    Excel export      rpm * 1.727 * 60 / (1000 * 5.09)
    odometer/laps     gear 5.09, circumference 1.727

The HUD used gear ratio 5.0 where everything else used 5.09, and a 0.279 m
wheel RADIUS (circumference 1.7530 m) where everything else used a 1.727 m
CIRCUMFERENCE. Net effect: the driver's speedometer read 3.3 % HIGH relative to
the pit, for the same CAN frame. Both cannot be right, and a number that
disagrees with itself across two screens can't be trusted by either.

So: one module, one formula, one set of constants. Change a number here and the
HUD, the pit, the lap counter and the exported workbook all move together.

THE CHAIN, IN ORDER
    raw CAN value  ── ÷ MOTOR_POLE_PAIRS ─→  mechanical motor RPM
                   ── ÷ GEAR_RATIO       ─→  wheel RPM
                   ── × circumference    ─→  metres per minute
                   ── × 60 ÷ 1000        ─→  km/h
"""

import math

# --------------------------------------------------------------------------- #
# 1. FINAL DRIVE — Gates PowerGrip GT4 belt, motor sprocket -> wheel sprocket
#
# Derived from the tooth counts rather than hard-coding 5.09, because the teeth
# are the physical truth and the decimal is a rounding of them:
#     144 / 22 = 6.545454...   (counted; the spec sheet's "1:5.09" was wrong)
# A synchronous belt cannot slip, so this ratio is exact — no correction factor.
# Re-sprocket the car and you only change these two integers.
# --------------------------------------------------------------------------- #
# COUNTED ON THE CAR 2026-08-20. These are the physical truth; the ratio is
# derived from them rather than hard-coded, so a re-sprocket means editing two
# integers and nothing else.
#
# The spec sheet said 112/22 = 5.09 and was simply wrong about the wheel
# sprocket - it has 144 teeth, not 112. A brief override of 2.75 sat here,
# fitted to a single chase-car reading of 74 km/h; the counted teeth supersede
# it. See the note on TIRE_DIAMETER_METERS: 74 km/h cannot be reconciled with
# these teeth and a 275 mm wheel, so that reading is treated as unreliable
# (a chase car reads its own speed, and its speedometer reads optimistically).
MOTOR_SPROCKET_TEETH = 22
WHEEL_SPROCKET_TEETH = 144
GEAR_RATIO = WHEEL_SPROCKET_TEETH / MOTOR_SPROCKET_TEETH      # 6.5454...

# --------------------------------------------------------------------------- #
# 2. ELECTRICAL vs MECHANICAL RPM  (TSRF-130, external motor)
#
# A brushless motor turns once mechanically for every POLE_PAIRS electrical
# revolutions, so a controller reporting ERPM reads POLE_PAIRS times high.
#
# The Silixcon LYNX docs label the 0x610 bytes 2-3 field simply "Motor speed
# [rpm]" and mention neither ERPM nor pole pairs anywhere, so the documented
# reading is MECHANICAL. The default below is therefore 1, which divides by
# nothing and leaves behaviour unchanged.
#
# ⚠️ CONFIRM THIS ON THE CAR — it is the single biggest possible speed error
# (a 4-pole-pair motor read as mechanical would show 4× the true speed).
# How to tell, without a datasheet:
#   • Roll the car one full wheel turn by hand and watch mms_rpm, or
#   • drive at a steady speed and compare the HUD against the GPS ground speed
#     now in the telemetry (gps.speed_kmh — a genuinely independent source).
# If the wheel-derived speed is a clean small-integer multiple of GPS speed,
# that multiple is your pole-pair count. Set it here.
# --------------------------------------------------------------------------- #
MOTOR_POLE_PAIRS = 1

# --------------------------------------------------------------------------- #
# 3. TIRE
#
# ⚠️ PLACEHOLDER — MEASURE AND REPLACE. This is the largest remaining source of
# speed error, and it scales speed, distance and lap count linearly: a 2 % tire
# error is a 2 % speed error and a 2 % lap-distance error, all race.
#
# 0.5497 m is not a measurement — it is back-calculated from the 1.727 m
# circumference the pit and the odometer have always used (1.727 / π), so that
# introducing this module changes NO pit number on day one. The driver HUD moves
# 3.3 % because the HUD was the outlier, not because this value changed.
#
# To measure properly (rolling circumference, not the sidewall number — a loaded
# tire is smaller than a free one):
#   1. Sit the car on the ground with the driver aboard and normal pressure.
#   2. Mark the tire and the floor at the contact patch.
#   3. Roll it straight for exactly 5 wheel revolutions; mark the floor again.
#   4. TIRE_CIRCUMFERENCE = distance / 5, then diameter = circumference / π.
# Set TIRE_DIAMETER_METERS from that, or set TIRE_CIRCUMFERENCE_METERS directly
# below if you would rather not round-trip through the diameter.
# --------------------------------------------------------------------------- #
TIRE_DIAMETER_METERS = 0.5497

TIRE_CIRCUMFERENCE_METERS = math.pi * TIRE_DIAMETER_METERS

# Kept for older imports that expect this name (Pit_Dashboard/constants.py
# re-exports it). Same value, derived — never edit this one directly.
WHEEL_CIRCUMFERENCE_METERS = TIRE_CIRCUMFERENCE_METERS


# --------------------------------------------------------------------------- #
# Conversions
# --------------------------------------------------------------------------- #
def mechanical_rpm(raw_rpm):
    """Raw CAN speed value -> mechanical motor RPM.

    A no-op while MOTOR_POLE_PAIRS is 1. Kept as its own step so the ERPM
    correction happens in exactly one place if it turns out to be needed.
    """
    if raw_rpm is None:
        return 0.0
    return float(raw_rpm) / MOTOR_POLE_PAIRS


def wheel_rpm(raw_rpm):
    """Raw CAN speed value -> WHEEL revolutions per minute.

    Divides by the gear ratio: the wheel turns slower than the motor, so a 5.09
    reduction means the wheel does 5.09 motor revs per wheel rev. Multiplying
    here instead of dividing is the classic way to get a 26× speed error.
    """
    return mechanical_rpm(raw_rpm) / GEAR_RATIO


def speed_kmh(raw_rpm):
    """Raw CAN speed value -> road speed in km/h.

    Always non-negative: reverse still moves the car, and a speedometer showing
    -12 km/h helps nobody. Direction is available from the sign of mms_rpm and
    from the reverse power map.

        wheel rev/min × metres/rev = metres/min
        metres/min × 60            = metres/hour
        metres/hour ÷ 1000         = km/h
    """
    return abs(wheel_rpm(raw_rpm)) * TIRE_CIRCUMFERENCE_METERS * 60.0 / 1000.0


def distance_metres(raw_rpm, elapsed_seconds):
    """Ground distance covered at this RPM over `elapsed_seconds`.

    Used by the odometer and lap counter, so they stay locked to the same tire
    and gear numbers the speedometer uses.
    """
    if elapsed_seconds <= 0:
        return 0.0
    revs = abs(wheel_rpm(raw_rpm)) * (elapsed_seconds / 60.0)
    return revs * TIRE_CIRCUMFERENCE_METERS


def describe():
    """One-line summary of the active drivetrain configuration, for startup logs.

    Printing this on boot means a mis-set tire or pole-pair count shows up in
    the log next to the data it produced, instead of being invisible.
    """
    return (f"drivetrain: {WHEEL_SPROCKET_TEETH}/{MOTOR_SPROCKET_TEETH} teeth "
            f"= {GEAR_RATIO:.4f}:1, tire Ø{TIRE_DIAMETER_METERS:.4f} m "
            f"(circumference {TIRE_CIRCUMFERENCE_METERS:.4f} m), "
            f"pole pairs {MOTOR_POLE_PAIRS}")


# --------------------------------------------------------------------------- #
# Self-check:  python3 drivetrain.py
# Prints the active configuration and a reference table, so you can sanity-check
# the numbers after changing the tire size or pole pairs.
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    print(describe())
    print(f"\n  one motor revolution moves the car "
          f"{TIRE_CIRCUMFERENCE_METERS / GEAR_RATIO:.6f} m\n")
    print(f"  {'motor rpm':>10} {'wheel rpm':>10} {'km/h':>8}")
    for rpm in (0, 100, 500, 1000, 2000, 3000, 4000, 5000):
        print(f"  {rpm:>10} {wheel_rpm(rpm):>10.1f} {speed_kmh(rpm):>8.2f}")

    print("\n  what the old formulas gave, for comparison:")
    for rpm in (1000, 3000):
        hud_old = (rpm / 5.0) * 60.0 * (2 * 3.14159 * 0.279) / 1000.0
        pit_old = rpm * 1.727 * 60 / (1000 * 5.09)
        print(f"    {rpm} rpm -> old HUD {hud_old:6.2f}  "
              f"old pit {pit_old:6.2f}  now {speed_kmh(rpm):6.2f} km/h")
