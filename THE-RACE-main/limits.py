"""
limits.py — the temperature thresholds both dashboards colour by
================================================================
Shared by the driver HUD and the pit wall, for the same reason drivetrain.py
and track.py are shared: when each end kept its own copy of a number, they
drifted. The pit was calling a motor temperature "warning" at 80 °C while the
HUD flashed red at it, and a gauge that is red in the car but amber on the pit
wall makes both displays untrustworthy.

TIERS
    below warn      the gauge's normal colour; nothing is wrong
    warn .. crit    amber. Worth watching, no action required
    above crit      red, and the driver's gauge blinks. Act now.

WHY THESE NUMBERS AND NOT THE OLD ONES
The controller gauge previously alerted above 30 °C. A powered motor controller
passes that within seconds of switching on, so the HUD flashed red continuously
from the moment the car woke up, and the crew learned to ignore a flashing
gauge. A warning that is always on carries no information; blinking has to be
rare to mean anything. Everything below is set so that a healthy car shows no
colour at all.

⚠️ These are conservative starting points, not datasheet values. Set them from
the real limits for the TSRF-130 motor, the Silixcon controller and your cells
when you have them — changing them here changes both dashboards at once.
"""

# Motor windings, measured by the PT1000 in the stator. Motors tolerate more
# than this, but 90 °C is where a solar-car motor is working hard enough to be
# worth backing off; sustained heat is what kills insulation.
MOTOR_TEMP_WARN = 100.0
MOTOR_TEMP_CRIT = 130.0

# Controller/ESC internal temperature. Most ESCs begin thermally derating around
# 80-85 °C, so red here means "you are about to lose power", not "it is broken".
CTRL_TEMP_WARN = 65.0
CTRL_TEMP_CRIT = 80.0

# Hottest cell in the pack. Deliberately the tightest limits of the three:
# lithium cells degrade permanently well before they are visibly in trouble, and
# unlike the motor there is no cooling-off lap that undoes it.
CELL_TEMP_WARN = 45.0
CELL_TEMP_CRIT = 55.0


def temp_condition(value, warn, crit):
    """Map a temperature to "normal" | "warning" | "critical".

    Used by the pit tiles; the driver HUD's MiniGauge applies the same two
    thresholds itself so it can also drive the blink. Returns "normal" for None
    so a missing reading is never coloured as though it were a measurement.
    """
    if value is None:
        return "normal"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "normal"
    if value > crit:
        return "critical"
    if value > warn:
        return "warning"
    return "normal"


if __name__ == "__main__":
    print("temperature limits (warn / critical):")
    for name, warn, crit in (("motor", MOTOR_TEMP_WARN, MOTOR_TEMP_CRIT),
                             ("controller", CTRL_TEMP_WARN, CTRL_TEMP_CRIT),
                             ("max cell", CELL_TEMP_WARN, CELL_TEMP_CRIT)):
        print(f"  {name:<11} {warn:5.0f} / {crit:5.0f} °C")
    print("\n  sample conditions for the motor gauge:")
    for t in (20, 74, 76, 89, 91):
        print(f"    {t:3} °C -> {temp_condition(t, MOTOR_TEMP_WARN, MOTOR_TEMP_CRIT)}")
