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
# From the vehicle specification: Gates PowerGrip GT4, 8 mm pitch, 30 mm wide,
# 22 T driving pulley on the motor, 112 T driven pulley on the rear wheel, final
# drive 1:5.09. The spec states the tooth counts AND the ratio and they agree
# (112/22 = 5.0909), so this is a counted number, not a back-calculation.
#
# ⚠️ HISTORY - DO NOT "CORRECT" THIS BACK TO 144.
# On 2026-08-20 11:51 the wheel count was changed to 144, which made every
# derived speed 2.57x too low for two days. 144 is the tooth count of the BELT,
# not of the pulley: a 144 T GT4 belt at 8 mm pitch is 1152 mm long, and with
# these two pulleys that is a 285 mm motor-to-wheel centre distance - exactly
# how a Gates belt is specified. A 144 T PULLEY would be 367 mm across, two
# thirds of the 550 mm wheel it would be bolted to.
#
# A 2.75 override also sat here briefly, fitted to a chase-car reading of
# 74 km/h. It "worked" only because it absorbed the RPM error below into the
# gear ratio; 2.75 implies a 60.5 T pulley, which nobody has ever counted.
MOTOR_SPROCKET_TEETH = 22
WHEEL_SPROCKET_TEETH = 112
GEAR_RATIO = WHEEL_SPROCKET_TEETH / MOTOR_SPROCKET_TEETH      # 5.0909...

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
# 2b. THE CONTROLLER REPORTS HALF THE REAL MOTOR SPEED
#
# MEASURED, not assumed. Against 1,955 samples where the car was moving above
# 30 km/h with 5+ satellites, comparing the controller's RPM field to gpsd's own
# ground speed through the 5.0909 final drive gives a required multiplier of
# 2.0101 - half a percent from exactly 2. At the fastest GPS fix in the store,
# 79.6 km/h:
#
#     wheel            768 rpm
#     motor should be  3911 rpm   (768 x 5.0909)
#     controller says  1961 rpm   <- almost exactly half
#
# A factor of EXACTLY 2 is not calibration drift, it is a counting error:
# the controller is configured with twice the motor's real pole-pair count, so
# it divides the electrical frequency by twice what it should. The honest fix is
# in the SiliXcon configuration, not here - halve its pole-pair setting and this
# constant becomes 1.0. Until someone does that on the car, correct it here so
# the RPM the pit and the driver read is the real motor speed.
#
# ⚠️ This is why mms_rpm changes meaning on 2026-08-22: samples recorded before
# that are the controller's halved figure, samples after are the true one.
RPM_REPORT_SCALE = 0.5          # controller reports this fraction of true RPM

# --------------------------------------------------------------------------- #
# 3b. THE CONTROLLER'S OWN SPEED FIELD (0x610 bytes 4-5) IS SCALED SEPARATELY
#
# This divisor is NOT the gear ratio, even though it was the gear ratio for a
# while and still looks like the old wrong one. It is an empirical property of
# how the controller is currently configured: it computes that field from its
# own (halved) RPM against its own wheel setting, so the number needed to turn
# it into true km/h has nothing to do with the belt.
#
# It is measured the same way, and it CHANGED when the controller was
# reconfigured at 2026-08-20 12:12 (the field's ratio to RPM went 1.0366 ->
# 2.6656). With the value below, the stored speed matches gpsd to 0.7 %.
#
# Keeping it separate from GEAR_RATIO is the whole point: they were one constant
# until now, and that coincidence is exactly what hid the RPM error for two days
# - the speed looked perfect while everything derived from RPM was 2.57x out.
# Re-measure this if the controller is ever reconfigured again.
CONTROLLER_SPEED_DIVISOR = 6.5455

# Before that 12:12 reconfiguration the same field needed a different divisor,
# and this one is not empirical at all - it falls out of the other two. The old
# controller computed speed from its own (halved) RPM with the correct wheel
# circumference and no gear reduction, so undoing it is exactly the gear ratio
# scaled by the RPM error:
#
#     5.0909 x 0.5 = 2.5455
#
# Needed because 82 % of the stored history predates the reconfiguration, so
# anything reading that history has to know which era a row came from. Rows
# identify their own era by the ratio of the speed field to RPM - see
# db._vehicle_speed - so no date cutoff is involved anywhere.
CONTROLLER_SPEED_DIVISOR_LEGACY = GEAR_RATIO * RPM_REPORT_SCALE

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
def true_motor_rpm(reported_rpm):
    """The controller's RPM field -> the real motor RPM.

    Applied in EXACTLY ONE PLACE: mms_parser, as the 0x610 frame is decoded. So
    every mms_rpm that leaves the car is already the true motor speed, and
    everything downstream - the HUD gauge, the pit chart, the stored column -
    reads a real number rather than half of one.

    It is deliberately NOT folded into mechanical_rpm() below. If it were, then
    speed_kmh(mms_rpm) would correct an already-corrected value and land 2x
    high, which is a bug nobody would see until the car was on track.
    """
    if reported_rpm is None:
        return None
    return float(reported_rpm) / RPM_REPORT_SCALE


def mechanical_rpm(raw_rpm):
    """Motor RPM -> mechanical motor RPM. Expects an ALREADY-CORRECTED rpm.

    A no-op while MOTOR_POLE_PAIRS is 1, and kept as its own step so an ERPM
    correction has one home if it is ever needed. Note this is the opposite
    direction to RPM_REPORT_SCALE: ERPM reads HIGH so it divides, this
    controller reads LOW so true_motor_rpm() multiplies. Two separate constants
    rather than one fudge factor, so neither can silently absorb the other.
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
    return (abs(wheel_rpm(raw_rpm)) * TIRE_CIRCUMFERENCE_METERS * 60.0 / 1000.0) * 0.66


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
