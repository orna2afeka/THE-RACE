"""
limits.py — the alarm thresholds both dashboards colour by
==========================================================
Shared by the driver HUD and the pit wall, for the same reason drivetrain.py
and track.py are shared: when each end kept its own copy of a number, they
drifted. The pit was calling a motor temperature "warning" at 80 °C while the
HUD flashed red at it, and a gauge that is red in the car but amber on the pit
wall makes both displays untrustworthy.

TIERS
    below warn      the gauge's normal colour; nothing is wrong
    warn .. crit    amber. Worth watching, no action required
    above crit      red, and the driver's gauge blinks. Act now.

A metric may set crit=None, meaning "amber only, never red". That is not
laziness: see MOTOR_CURRENT below for a case where an instantaneous red would
be actively harmful.

WHY THESE NUMBERS
A warning that is always on carries no information; blinking has to be rare to
mean anything. The controller gauge once alerted above 30 °C, which a powered
controller passes within seconds, so the HUD flashed red from the moment the
car woke up and the crew learned to ignore it.

The same thing had quietly come back. Checked against 46,836 recorded samples,
the previous thresholds fired like this:

    motor temp    WARN 100 °C -> 31.06 % of samples   amber a third of the race
                  CRIT 130 °C ->  1.18 %              blinking red 1 sample in 85
    ctrl temp     WARN  65 °C -> 11.01 %
    motor current CRIT  90 A  -> past p95 (174 A)      blinked on every hard pull

Every value below was chosen against that recorded distribution so that a
healthy car shows no colour at all. The measured fire-rate is quoted next to
each one; if you change a number, re-measure rather than guessing.

⚠️ PROVISIONAL. These are grounded in observed telemetry, not in datasheets.
Set them from the real limits for the TSRF-130 motor, the SiliXcon controller
and your cells when you have them — changing them here changes both dashboards
at once. Two are weaker than the rest and say so in their own comments:
PACK_VOLTAGE and SOC.
"""
from collections import namedtuple

# ── Tier names ──────────────────────────────────────────────────────────── #
# Spelled out, and used as the single vocabulary everywhere. An earlier
# "warn"/"crit" spelling elsewhere in the tree meant a mismatched string fell
# through to "normal" silently, i.e. a real alarm rendered as healthy.
NORMAL = "normal"
WARNING = "warning"
CRITICAL = "critical"

# ── Tier -> colour ──────────────────────────────────────────────────────── #
# The tier is shared, so the colour has to be too. These were the driver HUD's
# values: it is the safety-critical screen and they are already tuned for a
# sunlit cockpit on a dark theme. NORMAL is None meaning "keep whatever accent
# the widget already uses", because a healthy speed gauge and a healthy
# temperature gauge are deliberately different colours.
TIER_COLOURS = {NORMAL: None, WARNING: "#ff6500", CRITICAL: "#ff2020"}


# `low_side=True` inverts the comparison, for metrics where LOW is the danger:
# state of charge and pack voltage. Without it those two can only be coloured
# by leaving them uncoloured, which is what the HUD used to do.
# `full_scale` is the gauge's arc saturation and lives here rather than in the
# HUD for one reason: the motor gauge used to saturate at 120 °C while its own
# critical threshold was 130 °C, so the arc pinned ten degrees before it was
# allowed to turn red. Keeping the scale next to the thresholds lets _validate()
# below make that unrepresentable.
Threshold = namedtuple("Threshold", "warn crit low_side full_scale")
Threshold.__new__.__defaults__ = (None, None, False, None)


def classify(value, threshold):
    """Map a reading to NORMAL | WARNING | CRITICAL against one Threshold.

    Returns NORMAL for None, for anything non-numeric, and for a None
    threshold, so a missing reading is never coloured as though it were a
    measurement. This is the ONE comparison in the project: the HUD gauges and
    the pit tiles both call it, so the two screens cannot drift apart.

    Comparisons are strict (>, <), so a value sitting exactly on a threshold is
    still the calmer tier. Critical is tested first, so a reading is never both.
    """
    if threshold is None or value is None:
        return NORMAL
    try:
        value = float(value)
    except (TypeError, ValueError):
        return NORMAL
    # NaN fails every comparison below and so falls through to NORMAL, which is
    # the behaviour we want: it is a broken reading, not a measurement.
    if threshold.low_side:
        if threshold.crit is not None and value < threshold.crit:
            return CRITICAL
        if threshold.warn is not None and value < threshold.warn:
            return WARNING
    else:
        if threshold.crit is not None and value > threshold.crit:
            return CRITICAL
        if threshold.warn is not None and value > threshold.warn:
            return WARNING
    return NORMAL


def colour_for(value, threshold):
    """Convenience: the tier colour for a reading, or None to keep the accent."""
    return TIER_COLOURS[classify(value, threshold)]


# ── Temperatures ────────────────────────────────────────────────────────── #

# Motor windings, via the PT1000 in the stator. The recorded median is 90 °C and
# p95 is 119 °C — this motor simply runs hot, so the old 100 °C warning was
# amber for a third of the race. 120 °C puts amber just above p95 (4.53 % of
# samples) and 140 °C is above the highest reading ever recorded (135.5 °C), so
# red means genuinely new territory rather than "working hard".
MOTOR_TEMP = Threshold(warn=120.0, crit=140.0, full_scale=150.0)

# Controller/ESC internal temperature. Recorded p95 is 68 °C, max 75 °C, so the
# old 65 °C warning fired 11 % of the time. 70 °C -> 1.47 %. Most ESCs begin
# thermally derating around 80-85 °C, so crit means "you are about to lose
# power", not "it is broken".
CTRL_TEMP = Threshold(warn=70.0, crit=85.0, full_scale=100.0)

# Hottest cell in the pack. UNCHANGED: the tightest limits of the three, and the
# only ones not driven by the recorded data — the pack never exceeded 32 °C, so
# there is nothing to tune against. Lithium cells degrade permanently well
# before they look to be in trouble, and unlike the motor there is no
# cooling-off lap that undoes it.
CELL_TEMP = Threshold(warn=45.0, crit=55.0, full_scale=80.0)


# ── Battery ─────────────────────────────────────────────────────────────── #

# Pack geometry. Inferred from the recorded controller voltage: a peak of
# 53.24 V over 13 cells is 4.10 V/cell, right at a full Li-ion charge, and the
# 48.89 V median is 3.76 V/cell. Change CELL_COUNT and every pack-level voltage
# threshold below follows.
CELL_COUNT = 13
CELL_V_MAX = 4.20      # full charge, per cell — sets the gauge's full scale
CELL_V_WARN = 3.20     # getting low
CELL_V_CRIT = 3.00     # standard Li-ion cutoff; below here you are damaging cells

PACK_V_FULL = CELL_COUNT * CELL_V_MAX      # 54.6 V

# ⚠️ THE WEAKEST THRESHOLD HERE. Judge the CONTROLLER's measurement only
# (mms_measured_voltage_V).
#
# The BMS's own voltage read 2.25x high for most of this project's life. It was
# fixed on 2026-08-20 at 12:12 and the two now agree to within 0.4 %, so the
# comments elsewhere in the tree calling it an open bug are out of date. Colour
# still comes from the controller's figure alone: it is the one that was
# bench-confirmed against the cell count, and 82 % of the stored history is
# still from before the fix, so anything replaying that history needs the source
# that did not change meaning halfway through.
#
# Two caveats, both visible in the recorded data. Of 387 samples below the warn
# level, 85 are under 30 V, which is 1.72 V/cell and not physically possible for
# a live 13S pack: those are bad frames, not flat packs. Most of the rest look
# like real voltage sag under a 200 A pull rather than depletion. So low-voltage
# amber partly tracks hard acceleration. Nothing here smooths or sanity-floors
# the reading yet; treat the tier as a hint until it does.
PACK_VOLTAGE = Threshold(warn=CELL_COUNT * CELL_V_WARN,      # 41.6 V, 0.88 %
                         crit=CELL_COUNT * CELL_V_CRIT,      # 39.0 V, ~0.5 %
                         low_side=True,
                         # Was 120 V, a leftover from the era when the misdecoded
                         # BMS voltage (~113 V) was what the gauge showed. On a
                         # 13S pack the arc never passed 44 %, so the readout was
                         # a number with a decorative arc beside it. 60 V puts a
                         # full charge at 91 % of the sweep.
                         full_scale=60.0)

# ⚠️ Also unexercised by the recorded data: the pack never went below 41 %, so
# both fire-rates are 0 % by absence of evidence, not by tuning. The critical
# level matches what the pit wall already used (< 20 %); the warning tier is new
# — before this, the car showed a pack at 3 % in exactly the same green as one
# at 95 %, which is the most dangerous gap the HUD had.
SOC = Threshold(warn=30.0, crit=20.0, low_side=True, full_scale=100.0)

# Below this, a pack-voltage reading is not a low pack: it is a bad frame.
# 2.5 V/cell is already under any Li-ion cell's usable floor, and a BMS would
# have opened the contactor long before a running car got there. The recorded
# store holds 102 such samples, down to 22.3 V (1.72 V/cell), with a perfectly
# healthy pack reported a second either side - so they are decode or framing
# noise, not measurements.
#
# It matters because they were landing in the CRITICAL band: replaying the store
# showed the voltage gauge blinking red on 0.57 % of samples, MORE OFTEN than it
# went amber, which is precisely the cry-wolf failure this file exists to stop.
# Treated as "no reading" they show an em dash instead, consistent with how
# every other unreported metric behaves.
PACK_V_IMPLAUSIBLE_BELOW = CELL_COUNT * 2.5      # 32.5 V


def plausible_pack_voltage(volts):
    """The reading, or None if it is not physically possible for this pack.

    Apply this where the voltage ENTERS the system, not inside classify():
    classify is generic and must not know about any one metric's physics. The
    distinction it preserves is the project-wide one - a broken reading is not a
    measurement, and must never be coloured as though it were.
    """
    if volts is None:
        return None
    try:
        volts = float(volts)
    except (TypeError, ValueError):
        return None
    return None if volts < PACK_V_IMPLAUSIBLE_BELOW else volts


# Pack current, compared as a MAGNITUDE — the callers apply abs() before this,
# because discharge is negative and a large discharge is a large negative
# number. Recorded |current| reaches 36.1 A, so the existing 40 A critical was
# well chosen and is kept; 30 A adds the amber tier it never had (0.24 %).
BATT_CURRENT = Threshold(warn=30.0, crit=40.0, full_scale=60.0)


# ── Motor ───────────────────────────────────────────────────────────────── #

# AMBER ONLY, DELIBERATELY — crit is None, so this never turns red and never
# blinks. High motor current IS normal here: the recorded distribution sits at
# 0 A for half the samples and then jumps to near a 200 A ceiling under
# acceleration, so p95 is 174 A. Any instantaneous red would blink on every hard
# pull out of a corner (3.2 % of samples even at 195 A) and would recreate
# exactly the ignore-the-flashing-gauge problem this file exists to prevent. A
# real over-current alarm has to watch SUSTAINED current, which nothing here
# does yet.
#
# Note also that the maximum is exactly 200.00 A with p99 at 199 A, which looks
# like a decode clip rather than a true ceiling. If it is, real current goes
# higher and this needs revisiting.
MOTOR_CURRENT = Threshold(warn=150.0, crit=None, full_scale=200.0)

# Motor power. Recorded p99 is 4140 W and the peak 5638 W. Regen is negative and
# so can never trip a high-side threshold, which is what we want: recovering
# energy is not a fault.
POWER = Threshold(warn=4000.0, crit=5000.0, full_scale=6000.0)


# ── Vehicle ─────────────────────────────────────────────────────────────── #

# Over-speed guard. This lived in the car as a bare 120 in driver_dash_v2.py,
# so the pit wall could not see it and could not stay in step with it.
#
# The numbers here were 110/120, set from a recorded maximum of 100.6 km/h that
# turned out to be an artefact: the gear ratio was wrong by 2.57x at the time,
# so every speed in the store was fiction. gpsd's own ground speed puts the real
# maximum at 79.6 km/h.
#
# So crit is 90: above anything this car has actually done, but close enough that
# reaching it means something. Full scale 100 keeps the real top speed at about
# 80 % of the sweep instead of two thirds. Warn is None because there is no
# useful "nearly too fast" band — either it is a normal lap or something is
# wrong.
SPEED = Threshold(warn=None, crit=90.0, full_scale=100.0)
# ⚠️ tools/replay_limits.py reports this firing red on ~10 % of the stored
# history. Do NOT raise crit because of that number. Those rows are one session
# with the wheels off the ground: the motor free-spun to 12,704 rpm, which is a
# 259 km/h EQUIVALENT road speed, and not one of those samples has a GPS fix.
# On the road the car has never passed 79.6 km/h, so this fires 0 % of the time
# where it matters. Filter the replay to rows with a GPS fix to see that.


def _validate(name, t):
    """Reject a Threshold that cannot render honestly.

    This exists because of a real bug: the motor gauge's full scale was 120 °C
    while its critical threshold was 130 °C, so the arc pinned at 100 % ten
    degrees before it was permitted to turn red — the single most severe state
    the gauge could show had no distinct appearance. A comment would not have
    caught that; an import-time check does.

    It raises rather than warning. A typo here takes both dashboards down on the
    bench in one second, which is loud and cheap; a silently inverted threshold
    takes them down on track, invisibly, which is neither.
    """
    if t.low_side:
        if t.warn is not None and t.crit is not None and t.crit > t.warn:
            raise ValueError(
                f"{name}: low-side means low is bad, so crit ({t.crit}) must be "
                f"at or below warn ({t.warn}). Looks transposed.")
        if t.full_scale is not None and t.warn is not None and t.full_scale < t.warn:
            raise ValueError(
                f"{name}: full_scale {t.full_scale} is below warn {t.warn}, so "
                f"the whole warning band sits off the end of the arc.")
    else:
        if t.warn is not None and t.crit is not None and t.warn > t.crit:
            raise ValueError(
                f"{name}: high-side means high is bad, so warn ({t.warn}) must "
                f"be at or below crit ({t.crit}). Looks transposed.")
        worst = t.crit if t.crit is not None else t.warn
        if t.full_scale is not None and worst is not None and t.full_scale < worst:
            raise ValueError(
                f"{name}: full_scale {t.full_scale} is below {worst}, so the arc "
                f"saturates before the gauge is allowed to change colour.")


# ── Solar charge current ────────────────────────────────────── #
#
# What the MPPT is pushing into the pack, from the Yocto-Amp in series on the
# charge line (SolarRace_OS/modules/solar_current.py).
#
# NO WARNING AND NO CRITICAL, deliberately. More solar current is unambiguously
# better, so there is nothing to alarm on at the top; and the bottom is not a
# fault either — 0 A at night, 0 A in a tunnel and 0 A under a cloud are all
# correct readings. A low-side threshold here would hold the gauge amber for the
# entire night stint, which is exactly the always-on warning this file exists to
# prevent. The gauge therefore shows a number and an arc, and no colour.
#
# full_scale is the SENSOR's continuous rating, not the array's expected output.
# Ending the arc where the Yocto-Amp stops being able to measure is the honest
# choice: a pinned gauge then means "at the limit of what we can measure", which
# is a real thing to know. Keep in step with
# solar_current.SENSOR_MAX_CONTINUOUS_A.
SOLAR_CURRENT = Threshold(warn=None, crit=None, full_scale=10.0)


# Every metric, for the self-test and for anything that wants to iterate them.
ALL_THRESHOLDS = (
    ("motor temp", "°C", MOTOR_TEMP),
    ("ctrl temp", "°C", CTRL_TEMP),
    ("max cell temp", "°C", CELL_TEMP),
    ("pack voltage", "V", PACK_VOLTAGE),
    ("state of charge", "%", SOC),
    ("batt current", "A", BATT_CURRENT),
    ("motor current", "A", MOTOR_CURRENT),
    ("motor power", "W", POWER),
    ("speed", "km/h", SPEED),
    ("solar current", "A", SOLAR_CURRENT),
)


# Checked at import, on every run of both dashboards. See _validate().
for _name, _unit, _t in ALL_THRESHOLDS:
    _validate(_name, _t)
del _name, _unit, _t


if __name__ == "__main__":
    def _fmt(v):
        return "   —   " if v is None else f"{v:7.1f}"

    print("shared alarm thresholds (warn / critical; * = low values are the danger)")
    print()
    for name, unit, t in ALL_THRESHOLDS:
        star = "*" if t.low_side else " "
        # Three cases, not two: a threshold with neither bound set is not
        # "amber only", it is deliberately uncoloured (see SOLAR_CURRENT).
        if t.warn is None and t.crit is None:
            note = "   no colour"
        elif t.crit is None:
            note = "   amber only"
        else:
            note = ""
        print(f"  {star} {name:<16} {_fmt(t.warn)} / {_fmt(t.crit)} {unit:<4}{note}")

    print()
    print(f"  pack: {CELL_COUNT}S, full charge {PACK_V_FULL:.1f} V "
          f"({CELL_V_MAX} V/cell)")

    # Samples that BRACKET each threshold, so the boundary behaviour is visible.
    # The previous version of this self-test still probed 74/76/89/91 °C, which
    # bracketed thresholds that had already been replaced, so every line printed
    # "normal" and the test silently proved nothing.
    print()
    print("  boundary behaviour (a value exactly on a threshold stays calmer):")
    for name, unit, t in ALL_THRESHOLDS:
        probes = []
        for edge in (t.warn, t.crit):
            if edge is not None:
                probes += [edge - 0.1, edge, edge + 0.1]
        if not probes:
            continue
        print(f"    {name} ({unit}):")
        for p in sorted(set(probes), reverse=bool(t.low_side)):
            print(f"      {p:7.1f} -> {classify(p, t)}")

    print()
    print("  a missing reading is never an alarm:")
    for bad in (None, "", "n/a", float("nan")):
        print(f"    {bad!r:>8} -> {classify(bad, MOTOR_TEMP)}")
