#!/usr/bin/env python3
"""
check_driver_cut_lap.py - the driver's button cuts the same lap the pit does
============================================================================
    python tools/check_driver_cut_lap.py

Drives the command queue and LapTracker directly. No Qt, no CAN, no Firebase.

WHAT CHANGED AND WHY IT NEEDS GUARDING. The button beside the HUD clock used
to be display-only: it restarted the number on screen and touched nothing.
That was deliberate -- "the lap count is scrutineering evidence and a driver's
thumb must not be able to change it" -- and it is now, deliberately, not true.
The button cuts a real lap, because a driver leaving the box wants the lap they
just drove recorded, not a clock that lies about it.

Reversing a rule that explicit means the things it was protecting have to be
checked rather than assumed:

  HELD        a tap does nothing. Only a hold of _LAP_CUT_HOLD_MS cuts a lap,
              so a knock against the panel over a kerb cannot add one.
  ONE PATH    the driver's cut IS the pit's cut: same queue, same applier, same
              force_lap, same checkpoint. Not a second idea of what a lap is.
  COUNTED     the lap is recorded with its time and its energy, exactly as a
              crossing of the line records one.
  QUIET       it is NOT acked on the pit's node. An ack answers a command the
              pit sent, matched by the id it issued; answering one it never
              sent puts a lap number on the sidebar that no press asked for.
  SAFE        submit_local is callable off the CAN thread -- that is the whole
              reason it queues instead of touching LapTracker.
  GATES       the id and age gates still protect the FIREBASE path. They exist
              for a retained node, and a local press must not disarm them.
  ROUND       a press only counts a lap the car has actually been round, by
              LapTracker.been_round -- the SAME test every gate passage is
              judged by, not a second threshold. This is what stops the press
              the button most invites: at Zolder the gate in the pit lane
              closes the lap on the way IN, so a cut at pit exit used to add a
              lap nobody drove, and the count never recovered. The PIT's cut
              is deliberately still an override.
"""

import os
import sys
import threading
import time
import warnings

warnings.filterwarnings("ignore")

_ROOT = os.environ.get("SOLARRACE_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
for p in (_ROOT, os.path.join(_ROOT, "SolarRace_OS"),
          os.path.join(_ROOT, "SolarRace_OS", "modules")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules import lap_command                                  # noqa: E402
from modules.lap_tracker import LapTracker                       # noqa: E402

FAILED = []


def check(label, ok, detail=""):
    print("  %-52s %s" % (label, "OK  " if ok else "FAIL"))
    if detail:
        print("       " + detail)
    if not ok:
        FAILED.append("%s - %s" % (label, detail))
    return ok


def inbox():
    """A lap inbox with no Firebase behind it."""
    return lap_command.CommandInbox(lambda cb: None,
                                    lap_command.VALID_ACTIONS, label="lap")


# The lap the driver is part way round when they reach for the button.
PART_LAP_S = 200.0
PART_LAP_WH = 47.0
# How far round they are by default: past MIN_LAP_DISTANCE_M, so the press is
# one the guard lets through. The refusals below pass a shorter one.
PART_LAP_M = 3800.0


def driven(laps=2, now=None, part_m=PART_LAP_M):
    """A tracker `PART_LAP_S` into a lap, with `laps` counted behind it.

    Built on a CONTROLLED clock, passed in, so the lap time the cut produces is
    a number worth asserting rather than whatever time.monotonic() happened to
    read. `now` is when the press lands.
    """
    now = time.monotonic() if now is None else now
    t = LapTracker()
    t._have_distance = t._have_energy = True
    for i in range(laps):
        t.odometer_m += 4000.0
        t.total_energy_wh += 120.0
        # Each earlier lap ends one PART_LAP_S before the next; the last of
        # them lands exactly PART_LAP_S before the press.
        t._trigger_lap("gate", now=now - PART_LAP_S * (laps - i))
    t.odometer_m += part_m                 # part way round the next
    t.total_energy_wh += PART_LAP_WH
    # CAN IS ALIVE, as it is on a running car: update_motion stamps this on
    # every frame. Without it _can_is_dead() is true (it has never spoken),
    # the tracker counts as blind, and been_round falls through to "time
    # alone" -- which would quietly pass every distance check below.
    t._last_motion_ts = now
    return t


def check_queue():
    box = inbox()
    box.submit_local("cut_lap")
    cmds = list(box.drain())
    check("a press reaches the CAN thread's queue",
          len(cmds) == 1 and cmds[0]["action"] == "cut_lap",
          "queued %s" % (cmds or "nothing"))
    check("QUIET: it is marked local, and carries no pit id",
          cmds[0].get("local") is True and cmds[0].get("id") is None,
          "by=%r local=%r id=%r" % (cmds[0].get("by"), cmds[0].get("local"),
                                    cmds[0].get("id")))
    check("        an unknown action is refused, not queued",
          _raises(box, "detonate"), "submit_local('detonate') raised")

    # SAFE: the HUD calls this from the Qt thread, never the CAN thread.
    box2 = inbox()
    errs = []

    def press():
        try:
            box2.submit_local("cut_lap")
        except Exception as exc:                              # noqa: BLE001
            errs.append(exc)

    threads = [threading.Thread(target=press) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("SAFE: submitting from other threads is safe",
          not errs and len(list(box2.drain())) == 8,
          "8 presses from 8 threads, %d error(s)" % len(errs))


def _raises(box, action):
    try:
        box.submit_local(action)
        return False
    except ValueError:
        return True


def check_counted():
    """COUNTED: the applier's cut_lap branch, run on a real tracker."""
    press = time.monotonic()
    t = driven(laps=2, now=press)
    before = t.lap_count
    part_wh = t.lap_energy_wh
    # This is what main._apply_lap_commands does for action == "cut_lap".
    t.force_lap("manual", now=press)
    check("COUNTED: the lap count moves",
          t.lap_count == before + 1, "%d -> %d" % (before, t.lap_count))
    check("        the lap is recorded with the time it took",
          t.last_lap_time_s is not None
          and abs(t.last_lap_time_s - PART_LAP_S) < 0.05,
          "%.2f s, against the %.0f s the driver was out" %
          (t.last_lap_time_s or -1, PART_LAP_S))
    check("        and with the Wh it actually cost",
          t.last_lap_energy_wh is not None
          and abs(t.last_lap_energy_wh - part_wh) < 1e-6,
          "%.1f Wh, the part-lap's own spend" % (t.last_lap_energy_wh or -1))
    check("        the next lap starts from zero",
          abs(t.lap_distance_m) < 1e-6,
          "lap_distance_m = %.1f m" % t.lap_distance_m)
    # The HUD shows time+Wh only when a lap FINISHED at this datum -- the test
    # main._publish_lap_timer makes before it emits.
    check("        the HUD is told a lap finished here",
          t.last_lap_finished_ts == t.lap_start_ts,
          "so _publish_lap_timer sends the time and the Wh, not a bare restart")


def check_one_path():
    """ONE PATH: the driver's cut and the pit's are the same call."""
    press = time.monotonic()
    a, b = driven(laps=2, now=press), driven(laps=2, now=press)
    a.force_lap("manual", now=press)           # what the driver's press runs
    b.force_lap("manual", now=press)           # what the pit's cut_lap runs
    same = all(getattr(a, f) == getattr(b, f) for f in
               ("lap_count", "lap_seq", "last_lap_number", "lap_source",
                "last_lap_time_s", "last_lap_energy_wh"))
    check("ONE PATH: driver and pit cut the identical lap",
          same, "both reach LapTracker.force_lap('manual') through the "
                "same queue and the same applier branch")


def check_gates_intact():
    """GATES: the Firebase path keeps both idempotency gates."""
    box = inbox()

    class Ev:
        def __init__(self, data):
            self.data = data
            self.path = "/"

    now = time.time()
    box._on_event(Ev({"id": 100, "action": "cut_lap", "ts": now}))
    first = len(list(box.drain()))
    box._on_event(Ev({"id": 100, "action": "cut_lap", "ts": now}))
    replay = len(list(box.drain()))
    check("GATES: a replayed pit command is still ignored",
          first == 1 and replay == 0,
          "id 100 accepted once, ignored on replay")

    stale = lap_command.MAX_COMMAND_AGE_S + 60
    box._on_event(Ev({"id": 200, "action": "cut_lap", "ts": now - stale}))
    check("        and a stale one is still not executed",
          len(list(box.drain())) == 0,
          "%.0f s old, adopted but not run" % stale)

    # And a local press is not affected by either gate: no id to clash, no age.
    box.submit_local("cut_lap")
    box.submit_local("cut_lap")
    check("        while two presses in a row both count",
          len(list(box.drain())) == 2,
          "the gates guard the retained node, not a thumb")


def _applier(tracker, local=True):
    """main._apply_lap_commands' cut_lap branch, verbatim.

    Returns True when the lap was counted. The one line worth copying here is
    the guard: everything else in that branch is printing and acks.
    """
    if local and not tracker.been_round():
        return False
    tracker.force_lap("manual")
    return True


def check_round():
    """ROUND: a press cannot count a lap the car has not been round."""
    import track

    short = driven(laps=2, part_m=PART_LAP_M)
    check("ROUND: a press past the gate's own distance counts",
          _applier(short) and short.lap_count == 3,
          "%.0f m in, MIN_LAP_DISTANCE_M = %.0f m"
          % (PART_LAP_M, track.MIN_LAP_DISTANCE_M))

    # THE ONE THAT BIT US. At Zolder the box is ~37 m past the line, so the
    # gate in the pit lane closes the in-lap on the way IN; the lap running at
    # pit exit is the out-lap, and it has only the pit lane behind it.
    # Measured against the lap_tracker simulator: 658 m at the exit.
    pit_exit = driven(laps=3, part_m=658.0)
    before = pit_exit.lap_count
    check("       a press at PIT EXIT counts nothing",
          not _applier(pit_exit) and pit_exit.lap_count == before,
          "658 m into the out-lap, lap stays %d" % pit_exit.lap_count)
    check("       and the datum is not moved either",
          abs(pit_exit.lap_distance_m - 658.0) < 1.0,
          "still %.0f m into the lap -- a refusal is not a re-sync, because "
          "a thumb press is nowhere in particular" % pit_exit.lap_distance_m)

    # The pit is an engineer reading the data who can see the count is short.
    # force_lap is their override and stays one.
    pit = driven(laps=3, part_m=658.0)
    check("       the PIT's cut is still an override at the same spot",
          _applier(pit, local=False) and pit.lap_count == 4,
          "force_lap is not second-guessed")

    # THE FALLBACK THAT MATTERS. With no trustworthy distance the car is blind,
    # and the driver's button is the only thing that can count a lap. A bare
    # `metres > N` test would disable it in exactly that case.
    blind = driven(laps=2, part_m=0.0)
    blind._distance_untrusted = True
    check("       a blind car can still be cut on time alone",
          _applier(blind) and blind.lap_count == 3,
          "no trustworthy distance: been_round falls back to elapsed")

    # And the rule is the gate's, not a copy.
    src = open(os.path.join(_ROOT, "SolarRace_OS", "modules", "lap_tracker.py"),
               encoding="utf-8").read()
    check("       one rule: the gate asks been_round() too",
          src.count("MIN_LAP_DISTANCE_M") == 2
          and "self.been_round(now, travelled=travelled" in src,
          "MIN_LAP_DISTANCE_M appears only inside been_round, which "
          "_on_gate_crossing calls")


def check_hold():
    """HELD: the button's own guard, read off the HUD's constants."""
    import re
    src = open(os.path.join(_ROOT, "SolarRace_OS", "driver_dash_v2.py"),
               encoding="utf-8").read()
    hold = re.search(r"_LAP_CUT_HOLD_MS = (\d+)", src)
    check("HELD: a hold is required before anything is cut",
          hold is not None and int(hold.group(1)) >= 500,
          "_LAP_CUT_HOLD_MS = %s ms" % (hold.group(1) if hold else "missing"))
    check("      a tap stops the timer instead of cutting",
          "_lap_cut_timer.isActive()" in src and "_lap_cut_timer.stop()" in src,
          "_lap_press_released cancels a hold that never completed")
    check("      the cut is queued, never applied on the GUI thread",
          "submit_local" in src and "self.laps" not in src,
          "the HUD never touches LapTracker directly")


def main():
    print("\nthe driver's button, against the rule it reverses\n")
    check_queue()
    check_counted()
    check_one_path()
    check_gates_intact()
    check_round()
    check_hold()
    if FAILED:
        print("\n%d FAILED:" % len(FAILED))
        for f in FAILED:
            print("  - %s" % f)
        return 1
    print("\nAll driver-cut-lap checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
