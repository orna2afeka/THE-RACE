"""
charge_detector.py — is the car actually being charged right now?
===================================================================
Answers one question for the pit's "current stint" energy tiles: has the car
stopped for an external charge. Nothing on this car reports that directly —
there is no charge-MOSFET status bit, no "charging" flag anywhere on the BMS
or the CAN bus — so it has to be inferred from two readings that already
exist: the BMS pack current and the motor's own RPM.

    det = ChargeDetector()
    ...
    if det.update(bms_current_A, mms_rpm):
        laps.mark_stint_start()   # a new charging stop just began

WHY BOTH SIGNALS, NOT JUST CURRENT
bms_parser.py documents bms_current_A as "+ = charge" — current flowing INTO
the pack. That is also exactly what regen braking looks like. A detector that
only watched current would start and stop a "stint" on every hard brake zone
of every lap, which is worse than not having the feature at all: it would
silently corrupt the one number ("energy since the last real charging stop")
it exists to produce.

Regen only happens while the car is MOVING. A real charging stop happens with
the car PARKED. Requiring both — stationary AND a sustained positive pack
current — is what tells the two apart with the sensors this car actually has.

THE TWO TIME WINDOWS, AND WHY THEY ARE DIFFERENT LENGTHS
Starting a stint is committed only after CONFIRM_S of both conditions holding
together, so a BMS balancing blip or a momentary noise spike while parked
between stints doesn't fire a false start.

Ending one is asymmetric on purpose:
  * the car driving away (no longer stationary) ends it INSTANTLY — that fact
    is unambiguous the moment it is true, and waiting to confirm it would let
    the stint clock keep running for a few seconds of real driving.
  * the pack current merely dropping while still parked gets a short
    RELEASE_S grace instead — a charger's CC-to-CV transition or a balancing
    pause looks exactly like "stopped charging" for a second or two, and
    ending the stint on that alone would fragment one real charging stop into
    several fictitious short ones.

WHAT A DETECTED START ACTUALLY DOES
Nothing here resets total_energy_wh or regen_energy_wh — those keep
accumulating for the whole race untouched, exactly like today. A detected
start is only a cue for the caller to snapshot LapTracker.mark_stint_start(),
which records where "since the last charging stop" begins counting FROM. See
mark_stint_start()'s own docstring for why the snapshot is taken at the START
of the stop rather than its end — the two are numerically the same moment for
this purpose, because motor energy does not move while the car is parked.
"""

import time

# Positive = current flowing INTO the pack, per bms_parser.py's documented
# convention. High enough to clear BMS quiescent draw and cell-balancing
# current so those alone can't look like a charger being connected; low
# enough that even a light trickle charge crosses it promptly.
CURRENT_THRESHOLD_A = 1.0

# Below this the wheel is not meaningfully turning. Not exactly 0 — CAN
# rounding and sensor noise on a truly stationary wheel can read a few rpm.
RPM_STATIONARY_THRESHOLD = 5.0

# Both conditions must hold together for this long before a stint boundary is
# committed. See the module docstring for why this is longer than RELEASE_S.
CONFIRM_S = 5.0

# How long the conditions may lapse, while still parked, before a charging
# stop already in progress is called over. Not applied when the car starts
# moving — see the module docstring.
RELEASE_S = 2.0


class ChargeDetector:
    """Debounced is-charging state machine. `update()` is the only public call.

    Feed it whatever the car most recently measured, at whatever cadence the
    caller already ticks at (main.py calls this from the same ~2 Hz timer that
    publishes GPS) — this needs no CAN-frame resolution, and evaluating it on
    every frame would only make CONFIRM_S/RELEASE_S harder to reason about for
    no benefit.
    """

    def __init__(self):
        self.is_charging = False
        # monotonic timestamp: when the CURRENTLY-PENDING candidate state (a
        # start being confirmed, or a release being confirmed) was first seen.
        # None means no candidate is pending.
        self._candidate_since = None

    def update(self, bms_current_A, rpm, now=None):
        """Feed one reading. Returns True on the exact tick a stint starts —
        i.e. exactly when is_charging flips False -> True — and False on every
        other tick, including every tick a charging stop continues.

        Never raises. A missing reading (None, from a stale or absent sensor)
        is treated as "not charging" for that input, so a sensor dropout
        cannot get stuck reporting a charge in progress forever.
        """
        now = time.monotonic() if now is None else now

        stationary = rpm is not None and abs(rpm) < RPM_STATIONARY_THRESHOLD
        charging_now = (stationary and bms_current_A is not None
                        and bms_current_A > CURRENT_THRESHOLD_A)

        if not self.is_charging:
            if not charging_now:
                self._candidate_since = None
                return False
            if self._candidate_since is None:
                self._candidate_since = now
                return False
            if now - self._candidate_since >= CONFIRM_S:
                self.is_charging = True
                self._candidate_since = None
                return True
            return False

        # Already charging: moving away ends it with no grace at all.
        if not stationary:
            self.is_charging = False
            self._candidate_since = None
            return False
        if charging_now:
            self._candidate_since = None    # current resumed; cancel any release
            return False
        if self._candidate_since is None:
            self._candidate_since = now
            return False
        if now - self._candidate_since >= RELEASE_S:
            self.is_charging = False
            self._candidate_since = None
        return False
