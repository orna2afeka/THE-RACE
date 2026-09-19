#!/usr/bin/env python3
"""
check_hud_stopwatch.py — the driver's lap clock, driven through its real states
===============================================================================
    python tools/check_hud_stopwatch.py

Builds the REAL RacingDashboard widget offscreen and calls the REAL slot the
CAN worker's lap_timer_updated signal is connected to, then reads back what the
label actually says. No CAN, no car, no screen.

WHY THIS FILE EXISTS
The stopwatch row is the one part of the HUD that changes state without the
driver touching anything: a lap ends, the finished time freezes in lime for
three seconds with what that lap cost beside it, and then the clock counts on
from the line. Four different things move it — the GPS gate, the odometer
fallback, the pit's Cut lap, and the pit's display-only stopwatch commands —
and the last of those deliberately reports Wh with no lap time at all.

That is four ways to reach one widget, so it is worth pinning down. The case
that broke in review was the empty one: a restart with nothing finished and no
energy must leave the row exactly as it was, and an early version opened the
freeze window anyway and held a stale figure from the previous lap.

SKIPS ITSELF, CLEANLY, where PySide6 is not installed — the car has it, the pit
laptop may not, and a pre-race sweep must not go red over a missing GUI toolkit.
"""

import os
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_REPO, os.path.join(_REPO, "SolarRace_OS")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Must be set before QApplication: no display on a pit laptop, and none on the
# car when this is run over ssh.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except Exception as exc:                                     # noqa: BLE001
    print("SKIPPED: PySide6 is not installed here (%s)" % exc)
    print("The HUD only runs on the car; this check needs its toolkit.")
    raise SystemExit(0)

import driver_dash_v2 as dd                                  # noqa: E402

failures = []


def want(cond, msg):
    if not cond:
        failures.append(msg)
        print("      ** FAIL ** " + msg)


app = QApplication([])
hud = dd.RacingDashboard()

print("the lap stopwatch row, on the real widget:\n")

# ── A counted lap: the time freezes, the cost sits beside it ─────────────── #
hud._on_lap_timer(time.monotonic(), 283.4, 152.37)
txt = hud._lap_lbl.text()
want("4:43.4" in txt, "a finished lap does not show its time: %r" % txt)
want("152 Wh" in txt, "a finished lap does not show its cost: %r" % txt)
want("font-size:" in txt,
     "the cost is not drawn smaller than the clock: %r" % txt)
print("    lap finished          %s" % txt.split("<")[0] + "  + its Wh")

# ── ...and both clear when the freeze runs out ───────────────────────────── #
hud._lap_hold_until = time.monotonic() - 0.01
hud._tick_lap_timer()
txt = hud._lap_lbl.text()
want("Wh" not in txt, "the cost outlives the freeze window: %r" % txt)
want("<span" not in txt, "markup outlives the freeze window: %r" % txt)

# ── The pit re-datums the clock mid-lap: no lap time, cost so far ────────── #
# There is no finished lap here, so the only thing worth showing is what the
# part-lap has already spent -- and the clock restarts from zero under it.
hud._on_lap_timer(time.monotonic(), None, 88.2)
txt = hud._lap_lbl.text()
want("88 Wh" in txt, "a pit reset does not show the part-lap cost: %r" % txt)
want("0:00.0" in txt, "a pit reset does not restart the clock: %r" % txt)

# ── The pit CLEARS the clock: dash, and still says what it cost ──────────── #
hud._on_lap_timer(None, None, 91.6)
txt = hud._lap_lbl.text()
want(dd._NO_DATA in txt, "a pit clear does not blank the clock: %r" % txt)
want("92 Wh" in txt, "a pit clear drops the cost (or misrounds it): %r" % txt)

# ── Nothing to report: the row must not move ─────────────────────────────── #
# The regression. last_lap_energy_wh still holds the PREVIOUS lap's figure
# after a restart that counted nothing, so anything that opened the freeze
# window here would show one lap's cost against another lap's clock.
hud._on_lap_timer(time.monotonic(), None, None)
txt = hud._lap_lbl.text()
want("Wh" not in txt and "<span" not in txt,
     "a restart with nothing finished still showed a figure: %r" % txt)

# ── The driver's own button still works ──────────────────────────────────── #
hud._reset_lap_timer()
txt = hud._lap_lbl.text()
want("Wh" not in txt, "the driver's reset button shows a stale cost: %r" % txt)
want("0:00.0" in txt, "the driver's reset button does not zero the clock: %r" % txt)

print()
if failures:
    print("FAILED (%d)" % len(failures))
    sys.exit(1)
print("All HUD stopwatch checks passed.")
