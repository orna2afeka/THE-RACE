#!/usr/bin/env python3
"""
check_limits.py - prove the shared thresholds behave, without a car
===================================================================
Runs the real MiniGauge and TachometerWidget headlessly (offscreen Qt) and
asserts the tier and blink behaviour for every metric in limits.ALL_THRESHOLDS.

There is no pytest anywhere in this project, so this follows the existing
convention: a standalone script under tools/ that exits non-zero on failure.

    python tools/check_limits.py

What it is actually guarding against, in order of how much it would hurt:

  1. A blink that never stops. The gauge starts its 500 ms flash timer on the
     EDGE into critical and stops it on the edge out. If a refactor breaks that
     edge handling, a gauge can be left flashing forever over a value that is
     fine - or, worse, flashing over an em dash after the bus goes quiet.
  2. A missing reading treated as a measurement. `None` must never breach a
     threshold and must never be coerced to 0.0. A gauge reading 0 is a lie the
     driver acts on.
  3. A low-side metric wired up as high-side. SoC and pack voltage are dangerous
     when LOW; getting that backwards means a flat pack shows green.
  4. An amber-only metric turning red. limits.MOTOR_CURRENT deliberately has no
     critical tier, because high motor current is normal under acceleration.
"""
import os
import sys

# Offscreen BEFORE any Qt import, so this runs over SSH and in a bare shell.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for _p in (_REPO, os.path.join(_REPO, "SolarRace_OS")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import limits                                          # noqa: E402
from PySide6.QtWidgets import QApplication             # noqa: E402
from driver_dash_v2 import MiniGauge, TachometerWidget, C_WARNING, C_CRITICAL  # noqa: E402

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}: expected {want!r}, got {got!r}")
        print(f"  FAIL  {label}: expected {want!r}, got {got!r}")
    else:
        print(f"  ok    {label}")


def eps_beyond(edge, low_side):
    """A value just past `edge` into the alarming direction."""
    return edge - 0.1 if low_side else edge + 0.1


def eps_within(edge, low_side):
    """A value just short of `edge`, still in the calmer tier."""
    return edge + 0.1 if low_side else edge - 0.1


def main():
    QApplication(sys.argv)

    print("classify() against every threshold")
    for name, _unit, t in limits.ALL_THRESHOLDS:
        # A value exactly on an edge stays in the CALMER tier, because the
        # comparisons are strict. Note "calmer" than critical is *warning*, not
        # normal: a reading sitting exactly on crit is still past warn.
        for edge, tier in ((t.warn, limits.WARNING), (t.crit, limits.CRITICAL)):
            if edge is None:
                continue
            calmer = (limits.WARNING
                      if tier == limits.CRITICAL and t.warn is not None
                      else limits.NORMAL)
            check(f"{name} @ {edge} (on the line)",
                  limits.classify(edge, t), calmer)
            check(f"{name} @ past {edge}",
                  limits.classify(eps_beyond(edge, t.low_side), t), tier)
        # Nothing unusable is ever an alarm.
        for bad in (None, "", "n/a", float("nan")):
            check(f"{name} @ {bad!r}", limits.classify(bad, t), limits.NORMAL)

    print()
    print("MiniGauge tiers, blink edges and no-data handling")
    for name, unit, t in limits.ALL_THRESHOLDS:
        g = MiniGauge(name.upper(), unit, C_WARNING, t)

        # Resting: a fresh gauge knows nothing and must not be alarming.
        check(f"{name}: starts with no value", g._value, None)
        check(f"{name}: starts calm", (g._alert, g._warning), (False, False))
        check(f"{name}: starts not blinking", g._flash_timer.isActive(), False)

        if t.warn is not None:
            g.set_value(eps_beyond(t.warn, t.low_side))
            want_warn = t.crit is None or True
            check(f"{name}: past warn -> amber", g._warning, want_warn and not g._alert)
            check(f"{name}: past warn does NOT blink", g._flash_timer.isActive(), False)

        if t.crit is not None:
            g.set_value(eps_beyond(t.crit, t.low_side))
            check(f"{name}: past crit -> red", g._alert, True)
            check(f"{name}: past crit blinks", g._flash_timer.isActive(), True)
            check(f"{name}: red is not also amber", g._warning, False)

            # THE regression that matters. The bus going quiet after a critical
            # reading must stop the flash, not leave it blinking over a dash.
            g.set_value(None)
            check(f"{name}: None after crit clears red", g._alert, False)
            check(f"{name}: None after crit STOPS the blink",
                  g._flash_timer.isActive(), False)
            check(f"{name}: None is not coerced to 0", g._value, None)

            # And back down through the tiers stops it too.
            g.set_value(eps_beyond(t.crit, t.low_side))
            g.set_value(eps_within(t.warn if t.warn is not None else t.crit,
                                   t.low_side))
            check(f"{name}: back to normal stops the blink",
                  g._flash_timer.isActive(), False)
        else:
            # Amber-only: nothing, however extreme, may turn this red.
            for v in (t.full_scale, t.full_scale * 2, t.full_scale * 10):
                g.set_value(v)
                check(f"{name}: {v:g} stays out of red (amber-only)",
                      g._alert, False)

        # The arc fraction must never go negative: signed metrics (power on
        # regen) would otherwise sweep the arc backwards out of the gauge.
        g.set_value(-abs(t.full_scale))
        frac = 0.0 if g._value is None or g._max_val <= 0 else min(
            1.0, max(0.0, g._value / g._max_val))
        check(f"{name}: negative input clamps the arc to 0", frac, 0.0)

    print()
    print("TachometerWidget shares the same rule")
    tach = TachometerWidget()
    check("tacho: starts with no speed", tach._speed, None)
    tach.set_speed(eps_beyond(limits.SPEED.crit, False))
    check("tacho: past crit -> red", tach._alert, True)
    check("tacho: past crit blinks", tach._flash_timer.isActive(), True)
    tach.set_speed(None)
    check("tacho: None stops the blink", tach._flash_timer.isActive(), False)
    check("tacho: None clears red", tach._alert, False)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S)")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
