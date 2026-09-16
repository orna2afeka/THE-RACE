#!/usr/bin/env python3
"""
check_health.py - the car-health badge, nine cases, no car
==========================================================
    python tools/check_health.py

Runs Pit_Web.api.car_health() and the badge text it drives through the nine
states the feature was specified against, and exits non-zero if any disagree.

The two that people get wrong are the last two: a car that predates the
heartbeat sends NO health block at all, and that must read as healthy. Silence
from an old build is not evidence of a fault, and an amber badge on every
laptop still running last week's image trains the crew to ignore it by
Saturday, which is the one thing this badge must never do.

The badge text here is what the pit shows when the feed is FRESH. A feed that
is not fresh at all is a different, older message and takes priority: the
car's self-report is irrelevant if we are not receiving it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Pit_Web import api                                          # noqa: E402

AGE = 3


def badge(state, age=AGE):
    """The same words the sidebar renders, built from car_health()."""
    ok, problems = api.car_health(state)
    if ok:
        return "LIVE · %ds ago" % age
    return "Pi alive %ds ago · " % age + " · ".join(problems)


CASES = [
    ("CAN live, GPS fix",
     {"can_state": "live", "can_silent_s": 0.4, "can_detail": "", "gps_fix": 1},
     "LIVE · 3s ago"),
    ("CAN live, can_silent_s 1.4 (under threshold)",
     {"can_state": "live", "can_silent_s": 1.4, "can_detail": "", "gps_fix": 1},
     "LIVE · 3s ago"),
    ("CAN silent 47.2s, no GPS",
     {"can_state": "silent", "can_silent_s": 47.2, "can_detail": "can0 silent 47s",
      "gps_fix": 0},
     "Pi alive 3s ago · can0 silent 47s · no GPS fix"),
    ("can_state disconnected",
     {"can_state": "disconnected", "can_silent_s": None, "can_detail": "no CAN bus open",
      "gps_fix": 0},
     "Pi alive 3s ago · CAN bus not open · no GPS fix"),
    ("CAN live but can_detail names a quiet channel",
     {"can_state": "live", "can_silent_s": 0.5, "can_detail": "can1 silent 3600s",
      "gps_fix": 1},
     "Pi alive 3s ago · can1 silent 3600s"),
    ("CAN live, gps_fix 0",
     {"can_state": "live", "can_silent_s": 0.5, "can_detail": "", "gps_fix": 0},
     "Pi alive 3s ago · no GPS fix"),
    ("can_state starting, no frames yet",
     {"can_state": "starting", "can_silent_s": None, "can_detail": "can0 no frames yet",
      "gps_fix": 0},
     "Pi alive 3s ago · can0 no frames yet · no GPS fix"),
    ("all health fields null (old car build)",
     {"can_state": None, "can_silent_s": None, "can_detail": None, "gps_fix": None},
     "LIVE · 3s ago"),
    ("empty state object",
     {},
     "LIVE · 3s ago"),
]

failed = 0
print("car_health(): nine cases\n")
for name, state, want in CASES:
    got = badge(state)
    ok = got == want
    failed += 0 if ok else 1
    print("  %-48s %s" % (name, "OK" if ok else "FAIL"))
    if not ok:
        print("      want: %s" % want)
        print("      got : %s" % got)

# The verdict served on the live socket carries the same answer, so the
# browser cannot disagree with the Python.
h = api._health_json({"can_state": "silent", "can_silent_s": 47.2,
                      "can_detail": "can0 silent 47s", "gps_fix": 0,
                      "pi_uptime_s": 812.0, "can_frames": 0, "gps_detail": "no fix"})
served_ok = (h["ok"] is False and h["problems"] == ["can0 silent 47s", "no GPS fix"]
             and h["piUptimeS"] == 812.0)
failed += 0 if served_ok else 1
print("  %-48s %s" % ("served payload carries ok + problems", "OK" if served_ok else "FAIL"))

print()
if failed:
    print("FAILED (%d)" % failed)
    sys.exit(1)
print("All health checks passed.")
