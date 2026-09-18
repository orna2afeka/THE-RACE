"""
efficiency.py — throttle calibration and the Eco / Normal / Power zones
=======================================================================
Shared by the driver HUD and the pit wall, for the same reason drivetrain.py,
track.py and limits.py are shared: the moment each end keeps its own copy of a
number, they drift, and a driver being coached to stay "in the green" from the
pit wall must be looking at the same green the car is showing them.

Two separate things live here, both of which the team will replace with
measured values:

  1. PEDAL CALIBRATION — the millivolt span the throttle sensor actually
     swings across, which turns the controller's raw GPIO reading into a
     percentage. Used ONLY on the car (mms_parser decodes mV -> %); the pit
     receives the percentage already computed.

  2. EFFICIENCY ZONES — the throttle percentages at which the car stops being
     economical. Used by BOTH screens: the HUD paints the driver's zone bar and
     the pit colours its throttle tile from the same two numbers.

⚠️ EVERY NUMBER IN THIS FILE IS A PLACEHOLDER. Nothing here has been measured
on this car. They are laid out as named constants precisely so that replacing
them with real data is a one-line edit per number rather than a hunt through
two dashboards. See each constant for what to measure and how.


WHY THE ZONES ARE NOT limits.Threshold
--------------------------------------
It is tempting to reuse limits.py — it already has a warn/crit pair, a
classify() and a colour table. Deliberately not done, for the same reason
constants.SECTION_RISK keeps its own palette:

  * A limits tier means SOMETHING IS WRONG. Amber on the motor-temp gauge is a
    fault developing. Amber here means "you are driving normally" — which is
    not a warning, and must never make the driver think a protection tripped.
  * Red here means "high consumption", a coaching cue, not "act now". Making
    the two pixel-identical would teach the crew that red means one thing when
    it means two.
  * A throttle percentage has no failure mode. There is nothing to alarm on:
    100 % throttle is a legitimate, safe thing to be doing on the pit straight.

So the zone vocabulary below is its own, and the two must not be mixed:
NEVER pass a zone to limits.classify() or a limits tier to ZONE_COLOURS.
"""

# ── Pedal calibration: raw millivolts -> throttle percent ───────────────── #
#
# The siliXcon ESC reports its GPIO readings in MILLIVOLTS (see the GPIO-over-
# CAN section of modules/mms_parser.py). A pedal is a potentiometer or a hall
# sensor driven from a reference rail, so it swings between two voltages that
# are properties of THIS pedal and THIS wiring loom — not of the protocol.
#
# ⚠️ THIS PEDAL IS NOT AN ACCELERATOR. It is a ONE-PEDAL control: the ESC
# regenerates below a neutral point and accelerates above it, so the single
# millivolt reading carries two different commands and the number that matters
# is the DISTANCE FROM NEUTRAL, not the distance from zero.
#
#     720 mV ........ 2000 mV ........ 4200 mV
#     released         neutral          floored
#     max regen        coasting         max acceleration
#     <------ regen     |     accelerate ------>
#
# A released pedal is therefore NOT "no throttle" — it is maximum regeneration,
# and reporting it as 0 % of anything would be the same class of lie as
# reporting a disconnected sensor as 0 %.
#
# MEASURED ON THE CAR 2026-09-18, replacing the placeholders that shipped here.
# Read off GPIO0 (input ID 0x08) on can0 with the pedal worked by hand:
#
#   released          0x02D0 = 720 mV, rock steady over many seconds
#   floored           0x1097 = 4247 mV peak, plateau 4202..4247 mV
#
# FULL is set to 4200 rather than the 4247 peak on purpose. The top of the
# travel wanders by ~45 mV between presses, and a driver who floors the pedal
# must see 100 %, not 99 %. Percentages clamp at 100, so aiming slightly low
# costs nothing and guarantees the bar reaches the top.
#
# ⚠️ NEUTRAL IS NOT MEASURED. 2000 mV is where the team says the controller's
# neutral sits; unlike the two ends, nobody has watched the motor stop pulling
# and start regenerating as the pedal crosses it. It is the one number here
# that is still somebody's word rather than a reading, and everything on both
# dashboards pivots around it. To measure it: roll the car, ease the pedal up
# until motor power (CAN 0x618) crosses zero, and read the millivolts there.
#
# TO RE-MEASURE (five minutes, car stationary, wheels off the ground):
#   1. Bring the throttle report up (config.THROTTLE_GPIO_* on the car).
#   2. Watch the "Throttle Raw" tile on the pit dashboard, or on the Pi:
#        cansend can0 147#0008000C00640064
#        stdbuf -oL candump can0,150:7F8 | grep --line-buffered '0D 08'
#      GPIO0's millivolts are bytes 2-3 of a '0D 08' frame, big-endian.
#   3. Pedal fully RELEASED, engine armed  -> that mV is THROTTLE_MV_IDLE.
#   4. Pedal fully FLOORED                 -> that mV is THROTTLE_MV_FULL,
#      less a small margin as above.
#   5. Put both numbers below. Nothing else changes.
THROTTLE_MV_IDLE = 720.0      # measured, pedal released = FULL REGEN
THROTTLE_MV_NEUTRAL = 2000.0  # ⚠️ team's figure, not measured — see above
THROTTLE_MV_FULL = 4200.0     # measured 4247 peak; set low so floored = 100 %

# Noise band on BOTH SIDES of neutral that still reads 0 %. A pedal held near
# coasting jitters by a few mV, and without this the driver's bar would flicker
# between a few percent of regen and a few percent of acceleration — the one
# place on the scale where a wobble reads as a change of direction.
THROTTLE_MV_DEADBAND = 50.0

# Plausibility window. OUTSIDE this, the reading is not a throttle position at
# all and we publish nothing rather than a number.
#
# This is the single most important guard in the file, and it is the same
# principle as pt1000's disconnected sentinel: a broken signal wire reads 0 mV,
# and 0 mV mapped naively through the span above is 0 % — "the driver is off
# the throttle". That is a confident lie about a safety-relevant input, and it
# is exactly the failure this project has repeatedly written comments about.
# A disconnected pedal must read "—", never "0".
#
# The window is deliberately WIDER than IDLE..FULL: a pedal that reads slightly
# outside its calibrated span is still a working pedal (and, before the
# constants above are measured, is the normal case).
THROTTLE_MV_MIN_VALID = 200.0
THROTTLE_MV_MAX_VALID = 4900.0

# Status strings, mirroring pt1000.read()'s contract: the percentage may be
# None, and when it is, this says WHY — so a dashboard can distinguish "the
# controller has not reported yet" from "the pedal is unplugged".
THROTTLE_OK = "ok"
THROTTLE_LOW = "implausible_low"     # below the window: broken wire / no supply
THROTTLE_HIGH = "implausible_high"   # above the window: short to the rail


def _checked_mv(millivolts):
    """Raw reading -> (float mV, None) or (None, status) if it is not a pedal.

    One gate for every conversion below, so "is this a plausible pedal
    reading?" is answered in exactly one place and the three functions cannot
    drift apart about it.
    """
    if millivolts is None:
        return None, THROTTLE_LOW
    try:
        mv = float(millivolts)
    except (TypeError, ValueError):
        return None, THROTTLE_LOW
    if mv < THROTTLE_MV_MIN_VALID:
        return None, THROTTLE_LOW
    if mv > THROTTLE_MV_MAX_VALID:
        return None, THROTTLE_HIGH
    return mv, None


def throttle_percent(millivolts):
    """Raw GPIO millivolts -> (ACCELERATION percent 0-100, status).

    0 % is the neutral point, not the bottom of the pedal's travel: below
    neutral the driver is regenerating, and that is reported as 0 %
    acceleration plus a regen percentage of its own (regen_percent below).

    Keeps its name because every caller on both dashboards already reads this
    one function, and because "throttle" on a driver's screen means "how hard
    am I asking for power" — which is exactly what this still is.

    Returns None for the percentage — never 0.0 — whenever the reading cannot
    be trusted as a pedal position. A missing throttle and a released throttle
    are different facts and both dashboards render them differently.
    """
    mv, status = _checked_mv(millivolts)
    if mv is None:
        return None, status

    floor = THROTTLE_MV_NEUTRAL + THROTTLE_MV_DEADBAND
    span = THROTTLE_MV_FULL - floor
    if span <= 0:
        # A miscalibration (FULL at or below neutral, or a deadband that
        # swallows the whole span) would otherwise divide by zero or invert the
        # pedal. Say nothing rather than report a backwards throttle.
        return None, THROTTLE_LOW

    pct = (mv - floor) / span * 100.0
    # Clamped, because the plausibility window above is intentionally wider
    # than the calibrated span: a pedal 100 mV past its measured full travel is
    # 100 %, not 103 %. And everything below neutral is 0 % acceleration — true,
    # not a fallback: a regenerating car is asking for no power at all.
    return round(min(100.0, max(0.0, pct)), 1), THROTTLE_OK


def regen_percent(millivolts):
    """Raw GPIO millivolts -> (REGEN percent 0-100, status).

    The mirror of throttle_percent around the neutral point: 0 % at neutral,
    100 % with the pedal fully released. Above neutral it is 0 % — the driver
    is accelerating and regenerating nothing.

    Deliberately a separate function rather than a signed percentage. A signed
    number invites a dashboard to draw one bar from -100 to +100 and a reader to
    miss the sign; two named quantities cannot be misread, and the one place
    that wants them together (the pedal bar on both screens) asks for both.
    """
    mv, status = _checked_mv(millivolts)
    if mv is None:
        return None, status

    ceiling = THROTTLE_MV_NEUTRAL - THROTTLE_MV_DEADBAND
    span = ceiling - THROTTLE_MV_IDLE
    if span <= 0:
        return None, THROTTLE_LOW

    pct = (ceiling - mv) / span * 100.0
    return round(min(100.0, max(0.0, pct)), 1), THROTTLE_OK


# ── Efficiency zones ────────────────────────────────────────────────────── #
#
# The driver-facing half. A hybrid car's ECO/POWER bar, reduced to the one
# question a driver can act on at 90 km/h: am I costing us the race right now?
#
# ⚠️ PLACEHOLDERS. These are the boundaries the feature request suggested
# (0-40 / 41-75 / 76-100), NOT the TSRF-130's efficiency map. Replace them
# with the throttle percentages at which this motor's efficiency actually falls
# away and both screens follow at once.
#
# When the real efficiency map arrives, note that it is very unlikely to be a
# function of throttle ALONE — motor efficiency is a surface over (torque, rpm).
# If it turns out the zone boundary needs to move with speed, change zone()
# below to take rpm too: every caller already passes through this one function,
# so that stays a change in one file.
THROTTLE_ECO_MAX_PCT = 40.0      # ⚠️ PLACEHOLDER — up to here is Eco
THROTTLE_NORMAL_MAX_PCT = 75.0   # ⚠️ PLACEHOLDER — up to here is Normal

# Zone identifiers. NOT limits tiers — see the module docstring. "normal"
# happens to spell the same as limits.NORMAL and means something related but
# distinct; ZONE_COLOURS and TIER_COLOURS are separate tables on purpose and a
# value from one must never be looked up in the other.
ZONE_ECO = "eco"
ZONE_NORMAL = "normal"
ZONE_POWER = "power"

ZONE_ORDER = (ZONE_ECO, ZONE_NORMAL, ZONE_POWER)

# Green / amber / red, as the request asked for. These are NOT the limits
# palette: the reds differ so that a red efficiency bar can never be mistaken
# at a glance for a red fault gauge sitting next to it.
ZONE_COLOURS = {
    ZONE_ECO: "#00E676",     # green — coasting economically
    ZONE_NORMAL: "#FFC400",  # amber — ordinary driving
    ZONE_POWER: "#FF6B35",   # orange-red — deliberately NOT limits' #ff2020
}

ZONE_LABELS = {
    ZONE_ECO: "ECO",
    ZONE_NORMAL: "NORMAL",
    ZONE_POWER: "POWER",
}


def zone(throttle_pct):
    """Throttle percent -> ZONE_ECO | ZONE_NORMAL | ZONE_POWER, or None.

    None for a missing reading, so a car that is not reporting throttle shows
    a blank bar rather than a confident green one. This is the ONE place the
    comparison happens: the HUD bar and the pit tile both call it, so the two
    screens cannot drift apart the way the gear ratio and the alarm thresholds
    each did before they were hoisted to the repo root.
    """
    if throttle_pct is None:
        return None
    try:
        pct = float(throttle_pct)
    except (TypeError, ValueError):
        return None
    if pct <= THROTTLE_ECO_MAX_PCT:
        return ZONE_ECO
    if pct <= THROTTLE_NORMAL_MAX_PCT:
        return ZONE_NORMAL
    return ZONE_POWER


def zone_label(throttle_pct):
    """Human-readable zone name, or None when there is no reading."""
    z = zone(throttle_pct)
    return None if z is None else ZONE_LABELS[z]


def zone_colour(throttle_pct):
    """Hex colour for a throttle percentage, or None when there is no reading.

    None means "the caller keeps whatever neutral it already uses" — the same
    contract limits.TIER_COLOURS[NORMAL] has, so widgets need no special case.
    """
    z = zone(throttle_pct)
    return None if z is None else ZONE_COLOURS[z]


def _validate():
    """Reject a zone configuration that cannot render honestly.

    Runs at import, like limits._validate(), so a bad edit is an ImportError on
    the bench instead of a nonsensical bar on the car. The failure this rules
    out is a real one: swap the two boundaries by accident and zone() silently
    stops ever returning ZONE_NORMAL, so the bar jumps green->red and the
    middle zone quietly ceases to exist.
    """
    if not 0.0 < THROTTLE_ECO_MAX_PCT < THROTTLE_NORMAL_MAX_PCT < 100.0:
        raise ValueError(
            "efficiency.py: zone boundaries must satisfy "
            "0 < THROTTLE_ECO_MAX_PCT < THROTTLE_NORMAL_MAX_PCT < 100, got "
            f"{THROTTLE_ECO_MAX_PCT} and {THROTTLE_NORMAL_MAX_PCT}."
        )
    if THROTTLE_MV_FULL <= THROTTLE_MV_IDLE:
        raise ValueError(
            "efficiency.py: THROTTLE_MV_FULL must be above THROTTLE_MV_IDLE "
            f"(got {THROTTLE_MV_FULL} <= {THROTTLE_MV_IDLE}). A pedal wired "
            "backwards is a wiring fix, not a calibration one."
        )
    # Neutral must sit strictly inside the travel, with room for the deadband on
    # both sides. Outside that, one half of the pedal silently ceases to exist:
    # neutral at or below IDLE and the car can never show regen, neutral at or
    # above FULL and it can never show acceleration. Either would be a screen
    # that looks fine and is simply missing half the driver's input.
    if not (THROTTLE_MV_IDLE + THROTTLE_MV_DEADBAND
            < THROTTLE_MV_NEUTRAL
            < THROTTLE_MV_FULL - THROTTLE_MV_DEADBAND):
        raise ValueError(
            "efficiency.py: THROTTLE_MV_NEUTRAL must lie between IDLE and FULL "
            f"with the deadband clear of both (got neutral "
            f"{THROTTLE_MV_NEUTRAL} in {THROTTLE_MV_IDLE}..{THROTTLE_MV_FULL} "
            f"with deadband {THROTTLE_MV_DEADBAND})."
        )
    if not (THROTTLE_MV_MIN_VALID <= THROTTLE_MV_IDLE
            and THROTTLE_MV_FULL <= THROTTLE_MV_MAX_VALID):
        raise ValueError(
            "efficiency.py: the plausibility window must contain the "
            "calibrated span, otherwise a correctly-pressed pedal reads as a "
            "disconnected one."
        )


_validate()
