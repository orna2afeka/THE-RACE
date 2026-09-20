"""
lap_tracker.py — laps, distance, energy and lap timing, computed on the car
===========================================================================
One object owns all four, because they must agree with each other: a lap is
defined by distance, lap energy is the integral between two lap triggers, and
lap time is the interval between the same two triggers. Splitting them across
the code is how they drift apart.

WHAT A LAP IS
One FORWARD passage of the finish gate (track.py): a line across the racing
surface AND the pit lane, because that is what the organisers' timing loop
spans. Everything else about a lap is a TAG on it, never a reason not to count
it, and the two are kept apart on purpose — a wrongly drawn pit lane can cost a
tag, never a lap.

    flying lap      gate on track -> gate on track
    in-lap          closes at the gate IN THE PIT LANE. Our box at Zolder is
                    ~37 m past the line (surveyed 2026-09-18), so that happens
                    on the way in and the stop belongs to the out-lap; a box
                    before the line would put the stop in the in-lap instead.
                    Nothing here depends on which: the gate decides
    out-lap         starts at that pit-lane passage, so its metres — and the
                    driver's target speed — count from the line like any other
    GPS missed it   a VIRTUAL crossing, cut back at datum + 4000 m and
                    interpolated, so the datum stays on the line
    any real gate passage that is not a lap (too little distance behind it)
                    RE-SYNCS the datum to the line and counts nothing

Deliberately free of CAN, Qt and Firebase imports, so it can be unit-tested on
a laptop against synthetic frames and fixes (see the self-check at the bottom).
main.py keeps only the wiring.

WHY EACH ACCUMULATOR OWNS ITS OWN TIMESTAMP
The odometer this replaces had a real bug worth understanding, because the
shape of it is easy to reintroduce. It looked like this:

    mms_data = parse_mms_message(msg_id, data)
    if mms_data:
        dt = now - self.last_rpm_time
        self.last_rpm_time = now                       # <- every frame
        self.odometer += distance(mms_data.get("mms_rpm", 0), dt)

parse_mms_message returns data for the status (0x600), battery (0x618) and
temperature (0x630) frames too, not just the motor frame (0x610). Those frames
carry no RPM, so `.get("mms_rpm", 0)` made their interval contribute ZERO
distance — but the timestamp was still advanced, so that time was consumed and
never attributed to anything. With four frame types interleaved, the odometer
recorded roughly a quarter of the real distance.

The fix here is structural rather than a patched condition: each accumulator's
timestamp is updated *inside the method that consumes it*, and each method is
called only when the caller has confirmed the value is present:

    if "mms_rpm"     in mms_data: tracker.update_motion(mms_data["mms_rpm"])
    if "mms_power_W" in mms_data: tracker.update_energy(mms_data["mms_power_W"])

There is no longer any code path that can advance a clock without integrating
the interval it measures.

WHY time.monotonic() AND NOT time.time()
A Raspberry Pi has no battery-backed clock. It boots believing it is whenever it
last shut down, and the first NTP sync steps the wall clock — potentially by
years. A forward step injected into the energy integral produces a nonsense Wh
figure; a backward step produces negative dt. Every interval measured here uses
time.monotonic(), which cannot step or go backwards. Wall clock appears only in
`lap_started_ts`, which exists to be compared against the pit's clock.
"""

import collections
import math
import os
import sys
import time

# track.py and drivetrain.py live at the repo root, shared with the pit.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import drivetrain           # noqa: E402
import track                # noqa: E402
import track_map            # noqa: E402


# Longest interval we will integrate across. Beyond this the bus (or the app)
# was stalled and we have no idea what the car did, so the interval is dropped
# rather than assumed constant. Applies to distance AND energy.
MAX_SAMPLE_GAP_S = 2.0

# Largest believable jump in the controller's TRIP counter between frames. It is
# broadcast at 1 Hz, so even at 150 km/h that is ~42 m; anything past this is a
# reset or a corrupt frame, not distance the car actually covered.
MAX_ODO_STEP_M = 200.0

# gpsd emits ~1 Hz but we poll far faster; ignore a fix we have already seen.
NEW_FIX_MIN_DT_S = 0.2

# Never build a crossing segment across a gap longer than this — a straight line
# between two fixes 5 s apart is not a safe model of a lap of a circuit.
MAX_GPS_SEGMENT_S = 3.0
MAX_GPS_SEGMENT_M = 200.0

# ...EXCEPT on the start straight itself, where a straight line IS the path.
# When both fixes lie in this corridor around the gate the gap may be this long,
# so a few seconds of lost lock (or a stalled loop) at the worst possible moment
# still yields the crossing. Along-track and lateral limits, gate frame.
GATE_BRIDGE_MAX_S = 10.0
GATE_CORRIDOR_ALONG_M = 300.0
GATE_CORRIDOR_LATERAL_M = 40.0

# GPS counts as "healthy" for this long after the last usable fix.
GPS_HEALTH_TIMEOUT_S = 5.0

# If the motor controller has been silent this long, distance has stopped
# accruing and cannot vouch for a lap. See _on_gate_crossing.
CAN_DEAD_AFTER_S = 30.0

# A car is STATIONARY by GPS when it has stayed within this radius for this
# long (under 1.8 km/h). A stationary car cannot cross a line: this is what
# stops GPS wander from cutting laps for a car parked in its box a few tens of
# metres before the gate.
STATIC_RADIUS_M = 5.0
STATIC_AFTER_S = 10.0
WHEEL_STOPPED_KMH = 1.5
GPS_STOPPED_KMH = 3.0

# A BACKWARD passage of the gate (pushed back in the pit lane) cancels the next
# forward one — but only briefly and only nearby, so one bad fix can never eat
# a real lap a few minutes later.
DEBT_MAX_AGE_S = 120.0
DEBT_MAX_DISTANCE_M = 200.0

# After a virtual or a manual cut the datum is only roughly on the line. A real
# gate passage within this distance of it moves the datum onto the line without
# counting — it is the same lap boundary, seen properly this time.
RESYNC_AFTER_CUT_M = 600.0

# Stationary this long within the gate corridor means the car is in its box,
# whatever the lane geometry said on the way in.
BOX_STOP_S = 20.0
# ...and it takes this long on the move to end a standstill.
STILL_RUN_BREAK_S = 3.0
# A flying lap may contain this much standstill and no more.
FLYING_MAX_STOPPED_S = 10.0

# An established zone changes only after this many consecutive decisive fixes.
ZONE_CONFIRM_FIXES = 3

# A GPS lap position older than this is not used for the target speed.
TRACK_POS_MAX_AGE_S = 3.0

# (monotonic ts, odometer, Wh, regen Wh) every TRAIL_STEP_S, so the state AT a
# crossing can be interpolated rather than taken from whichever sample followed
# it, and a virtual crossing can be placed where the odometer passed 4000 m.
# 360 s covers the 400 m between that mark and the fallback firing at 4 km/h.
TRAIL_STEP_S = 0.5
TRAIL_LEN = 720

# How a lap was cut. Every one of these is passed to _trigger_lap by exactly one
# route, and the pit's "Lap Source" tile names them in its caption — keep the two
# in step (Pit_Dashboard/live_metrics.py).
#
#   gps         the car passed the finish gate
#   gps_no_can  same, with the motor controller silent, so the lap is timed by
#               GPS alone and its distance is not to be trusted
#   odometer    a VIRTUAL crossing: GPS missed the gate, so the lap was cut back
#               at 4000 m by distance
#   manual      the pit cut it from the Cut Lap button
LAP_SOURCES = ("gps", "gps_no_can", "odometer", "manual")

# Before the first lap of a run there is no trigger to name. This is a sentinel,
# never a source: it is kept out of the telemetry snapshot (see snapshot()) so
# the pit shows an unreported field rather than a fifth kind of lap.
LAP_SOURCE_NONE = "none"

# What kind of lap the last one was. Only "flying" laps are fit to build energy
# and strategy figures from; the pit filters on this.
#
#   flying   gate to gate on the track, no standstill, nothing odd
#   in       ended in the pit lane (holds the pit stop, if there was one)
#   out      started in the pit lane
#   in_out   both
#   start    did not begin at the gate: a standing start, a pit correction,
#            the lap a fresh tracker was born into
#   suspect  counted, but do not trust its figures — see last_lap_flags
LAP_KINDS = ("flying", "in", "out", "in_out", "start", "suspect")

# A Pi has no battery-backed clock and boots believing it is whenever it last
# shut down. Wall-clock arithmetic across a reboot is only attempted when both
# ends are later than this (the same test lap_command.py applies).
_PLAUSIBLE_EPOCH = 1_700_000_000

ZONE_TRACK = "track"
ZONE_PIT = "pit_lane"
ZONE_BOX = "box"            # published only: in the pit lane AND standing still


class LapTracker:
    """Distance, energy, lap count and lap timing for one race.

    All methods are intended to be called from a SINGLE thread (the CAN worker
    thread in main.py). Nothing here locks, because nothing here is shared.
    """

    def __init__(self):
        # --- distance ------------------------------------------------------ #
        self.odometer_m = 0.0
        # Have we ever had a real basis for these totals? A fresh tracker holds
        # 0.0, which is indistinguishable from "the car has genuinely covered
        # 0 m" — so snapshot() used to publish a confident odometer_m: 0.0 and
        # calculated_lap: 0 from boot, and the pit drew "0.00 km / Lap 0" over a
        # dead bus. These flags let it publish null instead, which the pit
        # already stores as NULL and now renders as a dash.
        self._have_distance = False
        self._have_energy = False
        self._last_motion_ts = None      # monotonic; owned by update_motion
        # Once the controller's 0x620 TRIP counter appears we follow it instead
        # of integrating RPM — a real counter beats an estimate built on an
        # unmeasured tire diameter.
        self.using_controller_odo = False
        self._last_trip_m = None
        # The odometer was re-datumed under us (reset_trip, a TRIP jump), so the
        # metres since the last cut cannot vouch for a lap until the next one.
        self._distance_untrusted = False

        # --- energy -------------------------------------------------------- #
        self.total_energy_wh = 0.0
        self.regen_energy_wh = 0.0
        self.energy_gap_s = 0.0          # time we could NOT account for
        self._last_power_ts = None       # monotonic; owned by update_energy
        self._last_power_w = None        # previous sample, for the trapezoid

        # Where the CURRENT charging stint's counters started. "Stint" means
        # since the last real charging stop (see charge_detector.py) — NOT
        # since the last lap. A fresh tracker has never had a charging stop,
        # so these start at 0.0 and "current stint" reads the same as "total
        # race" until mark_stint_start() is first called — exactly the same
        # convention _lap_start_energy_wh already uses for lap 0.
        self._stint_start_energy_wh = 0.0
        self._stint_start_regen_energy_wh = 0.0

        # --- laps ---------------------------------------------------------- #
        self.lap_count = 0
        # Laps ever counted by this tracker. Unlike lap_count the pit cannot
        # set it, so it never repeats within a checkpoint's life: the pit keys
        # its per-lap tables on it.
        self.lap_seq = 0
        # gps | gps_no_can | odometer | manual, or LAP_SOURCE_NONE until a lap
        # has actually been cut. Survives a restart — see state_dict/restore.
        self.lap_source = LAP_SOURCE_NONE
        self.gps_lap_count = 0
        self.rejected_crossings = 0      # gate passages that were thrown away
        self.resyncs = 0                 # ...and ones that only moved the datum
        self.backward_crossings = 0
        self.last_rejected_distance_m = None

        self.last_lap_number = None
        self.last_lap_distance_m = None
        self.last_lap_energy_wh = None
        self.last_lap_regen_energy_wh = None
        self.last_lap_time_s = None
        self.last_lap_kind = None
        self.last_lap_flags = None       # tuple of names, see _close_lap
        self.last_lap_stopped_s = None
        self.last_cross_lateral_m = None
        self.lap_started_ts = time.time()          # wall clock, for the pit

        self._lap_start_odometer_m = 0.0
        self._lap_start_energy_wh = 0.0
        self._lap_start_regen_energy_wh = 0.0
        self._lap_start_ts = None                  # monotonic
        # Monotonic moment the last COUNTED lap ended. Equal to _lap_start_ts
        # exactly when the current lap began by finishing one, which is how the
        # HUD stopwatch tells a finished lap (hold its time) from the first
        # sighting of the line or a pit correction (just restart the clock).
        self.last_lap_finished_ts = None
        self._armed = False              # is the datum meant to be on the line?

        # What the CURRENT lap has been through; becomes the last lap's tags.
        # "gate" | "virtual" | "manual" | "boot": how this lap began.
        self._lap_origin = "boot"
        self._lap_started_in_pit = False
        self._lap_interrupted = False
        self._lap_stopped_s = 0.0
        self._lap_speed_seen = False     # without a speed, stopped_s is unknown
        self._lap_gps_path_m = 0.0       # distance by GPS, to cross-check CAN

        # --- GPS / gate state ---------------------------------------------- #
        self._last_good_xy = None        # survives stale fixes, see update_gps
        self._last_good_ts = None
        self._last_gps_ok_ts = None
        self.finish_line_distance_m = None
        self._debt = None                # (monotonic ts, odometer) of a backward pass
        self._gps_kmh = None
        self._gps_kmh_ts = None
        self._wheel_kmh = None
        self._wheel_ts = None
        self._static_anchor_xy = None
        self._static_anchor_ts = None
        self._gps_static = False

        # --- zone and GPS lap position ------------------------------------- #
        self._zone = None                # None | ZONE_TRACK | ZONE_PIT
        # An INFERRED zone (from standing still, or adopted at boot) was never
        # confirmed by a decisive fix, so ordinary fixes may overturn it.
        self._zone_inferred = False
        self._recent_lanes = collections.deque(maxlen=10)
        self._zone_votes = 0
        self._zone_candidate = None
        self._gate_along_m = None        # last fix, in the gate's frame
        self._gate_lateral_m = None
        self._track_pos = None           # (s_m, fix ts, odometer at that fix)

        # --- clocks owned by _tick ----------------------------------------- #
        self._clock = None               # newest `now` any update has seen
        self._stop_ts = None
        self._still_run_s = 0.0
        self._moving_run_s = 0.0
        self._trail = collections.deque(maxlen=TRAIL_LEN)
        self._trail_ts = None

    # ------------------------------------------------------------------ #
    # Integrators — each owns its own clock                               #
    # ------------------------------------------------------------------ #
    def update_motion(self, rpm, now=None):
        """Integrate distance. Call ONLY for frames carrying `mms_rpm`.

        The integration is skipped once the controller's own odometer is
        available (see update_odometer) — there is no point integrating an
        estimate when a real counter is on the bus — but the wheel SPEED is
        always taken from here, because "is the car standing still?" is asked
        far more often than TRIP's 1 Hz can answer.
        """
        now = time.monotonic() if now is None else now
        if self._last_motion_ts is not None and not self.using_controller_odo:
            dt = now - self._last_motion_ts
            if 0.0 < dt < MAX_SAMPLE_GAP_S:
                self.odometer_m += drivetrain.distance_metres(rpm, dt)
        self._last_motion_ts = now
        self._wheel_kmh = abs(drivetrain.speed_kmh(rpm))
        self._wheel_ts = now
        self._have_distance = True
        self._tick(now)

    def update_odometer(self, trip_m, now=None):
        """Adopt the controller's own distance counter (0x620 TRIP), in metres.

        Strongly preferred over integrating RPM, for two reasons:

        1. It is derived from the controller's configured wheel size rather than
           from drivetrain.TIRE_DIAMETER_METERS, which is still a placeholder
           nobody has measured.
        2. It is a counter, not a running integral, so a dropped frame or a
           stalled loop costs nothing — the next frame carries the true total,
           whereas an integration silently loses whatever it failed to sample.

        The car's lap distance follows the DELTA of this counter, so a TRIP
        reset (it is resettable from the controller) or a counter rollback does
        not teleport the odometer: the step is ignored and the datum re-taken.
        """
        now = time.monotonic() if now is None else now
        if trip_m is None:
            return
        trip_m = float(trip_m)

        if self._last_trip_m is not None:
            delta = trip_m - self._last_trip_m
            # A negative delta means TRIP was reset; an absurd jump means a
            # corrupt frame. Either way, re-datum rather than believe it.
            if 0.0 <= delta <= MAX_ODO_STEP_M:
                self.odometer_m += delta
            else:
                print(f"🔢 controller TRIP jumped {delta:+.0f} m — re-datuming")
                self._distance_untrusted = True
        elif not self.using_controller_odo:
            print("🔢 using the controller's own TRIP counter for distance")

        self._last_trip_m = trip_m
        self.using_controller_odo = True
        self._last_motion_ts = now      # distance is live; CAN is not dead
        self._have_distance = True
        self._tick(now)

    def update_energy(self, power_w, now=None):
        """Integrate energy. Call ONLY for frames carrying `mms_power_W`.

        Trapezoidal rather than rectangular: the controller broadcasts fast, but
        throttle transients are exactly where holding the previous sample
        constant across the interval biases the total. Averaging the two
        endpoints costs one extra variable and removes that bias.

        `mms_power_W` is SIGNED — negative during regen — so regen subtracts and
        `total_energy_wh` is NET energy at the motor. It can legitimately go
        down; nothing downstream may assume it only increases.
        """
        now = time.monotonic() if now is None else now
        power_w = float(power_w)
        if self._last_power_ts is not None and self._last_power_w is not None:
            dt = now - self._last_power_ts
            if 0.0 < dt < MAX_SAMPLE_GAP_S:
                avg_w = (self._last_power_w + power_w) / 2.0
                self.total_energy_wh += avg_w * dt / 3600.0
                if avg_w < 0.0:
                    self.regen_energy_wh += -avg_w * dt / 3600.0
            elif dt >= MAX_SAMPLE_GAP_S:
                # Record the time we refused to integrate, so a long dropout is
                # visible as missing energy rather than silently absorbed.
                self.energy_gap_s += dt
        self._last_power_ts = now
        self._last_power_w = power_w
        self._have_energy = True
        self._tick(now)

    def _tick(self, now):
        """Housekeeping that must run whichever input happens to be alive.

        Called by every update_*, so it keeps working on GPS alone or on CAN
        alone. It owns its own clocks (`_trail_ts`, `_stop_ts`) for the reason
        the module docstring gives: no interval is consumed without being
        attributed.
        """
        self._clock = now

        if self._trail_ts is None or now - self._trail_ts >= TRAIL_STEP_S:
            self._trail_ts = now
            self._trail.append((now, self.odometer_m,
                                self.total_energy_wh, self.regen_energy_wh))

        if self._stop_ts is not None:
            dt = now - self._stop_ts
            if 0.0 < dt < MAX_SAMPLE_GAP_S:
                still = self._is_stationary(now)
                if still is not None:
                    self._lap_speed_seen = True
                if still:
                    self._lap_stopped_s += dt
                    self._still_run_s += dt
                    self._moving_run_s = 0.0
                else:
                    # One fix with a little speed in it is not the car leaving:
                    # a parked receiver reports a few km/h now and then.
                    self._moving_run_s += dt
                    if self._moving_run_s >= STILL_RUN_BREAK_S:
                        self._still_run_s = 0.0
        self._stop_ts = now

        # Standing still for a while right by the gate: that is the box, even
        # if the lane geometry never said so (GPS under the pit roof, a wrongly
        # drawn pit lane, a Pi that booted there).
        # Not when the recent fixes have been saying "track", though: that is
        # a car on the grid.
        if (self._still_run_s >= BOX_STOP_S and self._zone != ZONE_PIT
                and track_map.PIT_ZONE_ENABLED and self._in_gate_corridor()
                and self._recent_lanes.count(ZONE_TRACK) < 3):
            self._set_zone(ZONE_PIT, inferred=True)

        self._check_distance_fallback(now)

    # ------------------------------------------------------------------ #
    # Is the car moving?                                                  #
    # ------------------------------------------------------------------ #
    def _is_stationary(self, now):
        """True / False, or None when nothing on board can say.

        GPS is the authority, because it is the one signal that is right both
        on a stand (wheels spinning, car parked — the pit's store holds ten
        "laps" driven that way) and when coasting (motor stopped, car moving).
        Its Doppler ground speed first: it stays near zero for a parked car even
        while the POSITION wanders by metres under the pit roof. Position next,
        for a receiver that reports no speed. Wheel speed only while GPS is out.
        """
        if self._gps_is_healthy(now):
            if (self._gps_kmh is not None
                    and now - self._gps_kmh_ts < TRACK_POS_MAX_AGE_S):
                return self._gps_kmh < GPS_STOPPED_KMH
            return self._gps_static
        return self._wheel_is_stopped(now)

    def _wheel_is_stopped(self, now):
        if self._wheel_ts is None or now - self._wheel_ts > MAX_SAMPLE_GAP_S:
            return None
        return self._wheel_kmh < WHEEL_STOPPED_KMH

    def _can_is_dead(self, now):
        return (self._last_motion_ts is None
                or (now - self._last_motion_ts) > CAN_DEAD_AFTER_S)

    def _gps_is_healthy(self, now):
        return (self._last_gps_ok_ts is not None
                and (now - self._last_gps_ok_ts) < GPS_HEALTH_TIMEOUT_S)

    def _in_gate_corridor(self, xy=None):
        if xy is None:
            along, lateral = self._gate_along_m, self._gate_lateral_m
            if along is None:
                return False
        else:
            along, lateral = track.gate_coords(xy)
        return (abs(along) <= GATE_CORRIDOR_ALONG_M
                and abs(lateral) <= GATE_CORRIDOR_LATERAL_M)

    # ------------------------------------------------------------------ #
    # GPS: the gate, the zone, the lap position                           #
    # ------------------------------------------------------------------ #
    def update_gps(self, fix, now=None):
        """Feed the newest gpsd fix. Returns "start", "lap", "resync" or None.

        "start"  = the first passage of the gate, which puts the datum on the
                   line without counting (see _on_gate_crossing)
        "lap"    = a counted lap
        "resync" = a real passage that was not a lap; the datum moved, the
                   count did not
        """
        now = time.monotonic() if now is None else now

        if not fix or fix.get("stale") or (fix.get("fix_mode") or 0) < 2:
            # Nothing is reset here. The previous good fix is KEPT: whether a
            # segment may be drawn from it is decided when the next good fix
            # arrives, from how long and how far apart the two are — a line
            # from before a dropout to after it must never be allowed to sweep
            # the gate from the far side of the circuit.
            self._tick(now)
            return None

        # gpsd is ~1 Hz but get_coordinates() returns the same fix on every
        # call with only fix_age_s advancing. Work out when the fix was actually
        # taken and ignore repeats, so duplicate polls don't collapse the
        # segment to zero length.
        fix_ts = now - float(fix.get("fix_age_s") or 0.0)
        if (self._last_good_ts is not None
                and (fix_ts - self._last_good_ts) < NEW_FIX_MIN_DT_S):
            self._tick(now)
            return None

        self._last_gps_ok_ts = now
        xy = track.to_local_xy(fix["lat"], fix["lon"])
        self.finish_line_distance_m = math.hypot(*xy)
        self._gate_along_m, self._gate_lateral_m = track.gate_coords(xy)

        speed = fix.get("speed_kmh")
        if speed is not None:
            self._gps_kmh, self._gps_kmh_ts = float(speed), now
        self._update_static(xy, fix_ts)

        prev_xy, prev_ts = self._last_good_xy, self._last_good_ts
        self._last_good_xy, self._last_good_ts = xy, fix_ts

        # May a straight line stand in for the path between the two fixes?
        segment_ok = False
        if prev_xy is not None:
            gap_s = fix_ts - prev_ts
            chord = math.dist(prev_xy, xy)
            if gap_s <= MAX_GPS_SEGMENT_S and chord <= MAX_GPS_SEGMENT_M:
                segment_ok = True
            elif (gap_s <= GATE_BRIDGE_MAX_S and self._in_gate_corridor(prev_xy)
                  and self._in_gate_corridor(xy)):
                segment_ok = True
            if segment_ok and not self._gps_static:
                self._lap_gps_path_m += chord

        self._update_zone(fix, fix_ts)

        event = None
        if segment_ok:
            hit = track.segment_gate_intersection(prev_xy, xy)
            if hit:
                event = self._on_gate_crossing(hit, prev_ts, fix_ts, now)
        self._tick(now)
        return event

    def _update_static(self, xy, fix_ts):
        if (self._static_anchor_xy is None
                or math.dist(xy, self._static_anchor_xy) > STATIC_RADIUS_M):
            self._static_anchor_xy, self._static_anchor_ts = xy, fix_ts
            self._gps_static = False
        elif fix_ts - self._static_anchor_ts >= STATIC_AFTER_S:
            self._gps_static = True

    def _update_zone(self, fix, fix_ts):
        pos, lane, decisive = track_map.locate(fix["lat"], fix["lon"])

        # An unknown zone takes the first consistent verdict; an established one
        # changes only on DECISIVE fixes, which exist only where the pit lane
        # has left the track. So the zone is decided at pit entry and pit exit
        # and merely carried down the straight, where the two lanes are 14 m
        # apart and one bad fix would otherwise blank the driver's target.
        self._recent_lanes.append(lane)
        may_change = decisive or self._zone is None or self._zone_inferred
        if lane is None or not may_change:
            self._zone_candidate, self._zone_votes = None, 0
        elif lane == self._zone:
            self._zone_candidate, self._zone_votes = None, 0
            if decisive:
                self._zone_inferred = False
        else:
            if lane != self._zone_candidate:
                self._zone_candidate, self._zone_votes = lane, 0
            self._zone_votes += 1
            if self._zone_votes >= ZONE_CONFIRM_FIXES:
                self._set_zone(lane, inferred=not decisive)

        if pos is not None and self._zone != ZONE_PIT:
            self._track_pos = (pos, fix_ts, self.odometer_m)

    def _set_zone(self, zone, inferred=False):
        self._zone = zone
        self._zone_inferred = inferred
        self._zone_candidate, self._zone_votes = None, 0
        if zone == ZONE_PIT:
            self._track_pos = None

    def _on_gate_crossing(self, hit, prev_ts, fix_ts, now):
        """The GPS path met the gate. Decide what that was."""
        if not track.CUT_LAP_ON_GATE:
            # A PERSON CUTS EVERY LAP -- see track.CUT_LAP_ON_GATE. Returned
            # before anything is touched: no count, no "gps_start" datum, no
            # resync, no backwards-passage debt. The lap distance keeps
            # running until Cut lap is pressed.
            return None
        t, lateral, sign = hit

        if self._is_stationary(now):
            return None                  # wander, not motion

        if sign < 0:
            self.backward_crossings += 1
            self._debt = (now, self.odometer_m)
            print("🏁 gate passed BACKWARDS — the next forward passage will "
                  "not count")
            return None

        if self._debt is not None:
            debt_ts, debt_odo = self._debt
            self._debt = None
            if (now - debt_ts <= DEBT_MAX_AGE_S
                    and abs(self.odometer_m - debt_odo) <= DEBT_MAX_DISTANCE_M):
                return None              # this forward pass undoes that one

        cross_ts = prev_ts + t * (fix_ts - prev_ts)
        at = self._state_at_time(cross_ts, now)
        self.last_cross_lateral_m = lateral

        if not self._armed:
            # First passage. The distance since power-on is meaningless (the car
            # may have been pushed to the grid), so this puts the datum on the
            # line instead of counting. Every lap after it is line-to-line.
            self._trigger_lap("gps_start", now, count_it=False, at=at)
            return "start"

        travelled = at[1] - self._lap_start_odometer_m
        elapsed = (cross_ts - self._lap_start_ts
                   if self._lap_start_ts is not None else None)

        # The same boundary, seen properly: the datum was put NEAR the line by a
        # virtual or a manual cut a moment ago, and here is the line itself.
        if (self._lap_origin in ("virtual", "manual")
                and travelled <= RESYNC_AFTER_CUT_M
                and not self._distance_untrusted):
            return self._resync(now, at)

        if elapsed is not None and elapsed < track.MIN_LAP_TIME_S:
            self.rejected_crossings += 1
            self.last_rejected_distance_m = travelled
            return None

        if self.been_round(now, travelled=travelled, elapsed=elapsed):
            self.gps_lap_count += 1
            self._trigger_lap("gps_no_can" if self._can_is_dead(now) else "gps",
                              now, at=at)
            return "lap"

        self.last_rejected_distance_m = travelled
        return self._resync(now, at)

    def _resync(self, now, at):
        self.resyncs += 1
        print(f"🏁 gate passed {at[1] - self._lap_start_odometer_m:.0f} m into "
              f"the lap — datum moved onto the line, nothing counted")
        self._trigger_lap("resync", now, count_it=False, at=at)
        return "resync"

    # ------------------------------------------------------------------ #
    # The trail: what the totals were at a past moment / distance         #
    # ------------------------------------------------------------------ #
    def _state_now(self, now):
        return (now, self.odometer_m, self.total_energy_wh, self.regen_energy_wh)

    def _state_at_time(self, ts, now):
        """(ts, odometer, Wh, regen) interpolated at a past monotonic moment."""
        points = list(self._trail) + [self._state_now(now)]
        if ts >= points[-1][0]:
            return (ts,) + points[-1][1:]
        if ts <= points[0][0]:
            return (ts,) + points[0][1:]
        for a, b in zip(points, points[1:]):
            if a[0] <= ts <= b[0]:
                f = (ts - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
                return tuple(p + f * (q - p) for p, q in zip(a, b))
        return (ts,) + points[-1][1:]

    def _state_at_odometer(self, odometer_m, now):
        """The same, at the moment the odometer PASSED a value, or None when
        the trail does not reach back that far.

        The first sample at or past the value closes the bracket, so a car that
        parked exactly there is cut when it arrived, not when it left.
        """
        points = list(self._trail) + [self._state_now(now)]
        if not points or odometer_m < points[0][1] or odometer_m > points[-1][1]:
            return None
        for a, b in zip(points, points[1:]):
            if a[1] <= odometer_m <= b[1]:
                f = (odometer_m - a[1]) / (b[1] - a[1]) if b[1] > a[1] else 0.0
                return tuple(p + f * (q - p) for p, q in zip(a, b))
        return None

    # ------------------------------------------------------------------ #
    # Distance-only fallback: the virtual crossing                        #
    # ------------------------------------------------------------------ #
    def _check_distance_fallback(self, now):
        """Count laps from distance when GPS can't, so counting never stops.

        The lap is cut BACK at datum + 4000 m, not here. Cutting where the
        fallback happens to fire put the datum 400 m past the line, so every
        real passage after it arrived "3600 m into the lap" and was thrown
        away; the fallback then fired again, 400 m later still, and lap
        counting stayed on the odometer for the rest of the stint.

        SWITCHED OFF at the team's decision -- track.CUT_LAP_ON_DISTANCE, and
        the reasoning is there rather than here. The code below is left intact
        and reachable by that one constant: the two faults that made it wrong
        at Zolder (an odometer 1.5 % long, GPS dead while driving) are both
        fixable, and when they are, this is how counting continues without a
        gate.
        """
        if not track.CUT_LAP_ON_DISTANCE:
            return
        if not self._have_distance:
            return
        if not self._armed:
            # Never seen the gate: behave exactly like the old
            # odometer // TRACK_LENGTH counter, so a car with no GPS at all is
            # no worse off than before this feature existed.
            limit = track.TRACK_LENGTH_METERS
        elif self._gps_is_healthy(now):
            # GPS is working but hasn't reported a passage well past a full
            # lap — detection missed.
            limit = track.ODOMETER_FORCE_LAP_M
        else:
            limit = track.LAP_DISTANCE_MAX_M

        if (self.odometer_m - self._lap_start_odometer_m) < limit:
            return
        mark = self._lap_start_odometer_m + track.TRACK_LENGTH_METERS
        at = self._state_at_odometer(mark, now)
        if at is None:
            # The trail does not reach back to the mark. Keep the DISTANCE datum
            # on it regardless — that is what keeps the next lap in phase — and
            # accept a lap time that runs to now.
            at = (now, mark, self.total_energy_wh, self.regen_energy_wh)
        self._trigger_lap("odometer", now, at=at)

    # ------------------------------------------------------------------ #
    # The one place a lap is ever cut                                     #
    # ------------------------------------------------------------------ #
    def _trigger_lap(self, source, now=None, count_it=True, at=None):
        """Close the lap (if it counts) and re-datum for the next one.

        The gate, the virtual crossing and the pit's commands all funnel
        through here, so a lap cut by any route produces exactly the same
        bookkeeping and there is no second copy to keep in step.

        `at` is the (ts, odometer, Wh, regen) the cut happened AT, which for a
        gate passage or a virtual crossing is a little in the past. The lap
        that ends and the lap that begins share it exactly, so no metre, joule
        or second is lost or counted twice at a boundary.
        """
        now = time.monotonic() if now is None else now
        ts, odo, wh, regen = at if at is not None else self._state_now(now)

        if count_it:
            self._close_lap(source, ts, odo, wh, regen)

        self._lap_start_odometer_m = odo
        self._lap_start_energy_wh = wh
        self._lap_start_regen_energy_wh = regen
        self._lap_start_ts = ts
        # wall clock for the pit's stopwatch, moved back to the cut itself
        self.lap_started_ts = time.time() - max(0.0, now - ts)
        self._armed = True

        self._lap_origin = {"odometer": "virtual", "manual": "manual",
                            "manual_restart": "manual"}.get(source, "gate")
        self._lap_started_in_pit = self._zone == ZONE_PIT
        self._lap_interrupted = False
        self._lap_stopped_s = 0.0
        self._lap_speed_seen = False
        self._lap_gps_path_m = 0.0
        self._distance_untrusted = False

    def _close_lap(self, source, ts, odo, wh, regen):
        self.lap_count += 1
        self.lap_seq += 1
        self.last_lap_number = self.lap_count
        self.last_lap_distance_m = odo - self._lap_start_odometer_m
        self.last_lap_energy_wh = wh - self._lap_start_energy_wh
        self.last_lap_regen_energy_wh = regen - self._lap_start_regen_energy_wh
        self.last_lap_time_s = (
            (ts - self._lap_start_ts) if self._lap_start_ts is not None else None)
        self.lap_source = source
        self.last_lap_finished_ts = ts
        self.last_lap_stopped_s = (round(self._lap_stopped_s, 1)
                                   if self._lap_speed_seen else None)

        ended_in_pit = self._zone == ZONE_PIT
        distance_suspect = (
            source != "gps_no_can" and not self._distance_untrusted
            and not (track.LAP_DISTANCE_MIN_M <= self.last_lap_distance_m
                     <= track.LAP_DISTANCE_MAX_M))
        flags = [name for name, on in (
            ("started_in_pit", self._lap_started_in_pit),
            ("ended_in_pit", ended_in_pit),
            ("not_from_gate", self._lap_origin in ("manual", "boot")),
            ("virtual_start", self._lap_origin == "virtual"),
            ("virtual_end", source == "odometer"),
            ("manual_end", source == "manual"),
            ("interrupted", self._lap_interrupted),
            ("distance_untrusted", self._distance_untrusted or source == "gps_no_can"),
            ("distance_suspect", distance_suspect),
            ("stopped", self._lap_stopped_s > FLYING_MAX_STOPPED_S),
        ) if on]
        self.last_lap_flags = tuple(flags)

        if self._lap_started_in_pit and ended_in_pit:
            kind = "in_out"
        elif ended_in_pit:
            kind = "in"
        elif self._lap_started_in_pit:
            kind = "out"
        elif "not_from_gate" in flags:
            kind = "start"
        elif flags:
            kind = "suspect"
        else:
            kind = "flying"
        self.last_lap_kind = kind

    # ------------------------------------------------------------------ #
    # Commands from the pit                                               #
    # ------------------------------------------------------------------ #
    def been_round(self, now=None, travelled=None, elapsed=None):
        """Has the car covered enough of a lap for this to BE one?

        The gate asks this of every forward passage: one with too little
        behind it re-syncs the datum instead of counting (see
        _on_gate_crossing). THE DRIVER'S HUD BUTTON ASKS THE SAME QUESTION,
        which is the whole reason this is a method rather than an expression
        inline up there -- a second threshold somewhere else would be a second
        idea of what a lap is.

        Either measure of distance will do: the odometer can be miscalibrated
        or frozen, the GPS path can have holes. With NEITHER available the car
        is blind, and time alone has to answer -- which is exactly when the
        driver's button matters most, so a bare distance test would disable it
        in the one case it is there for.

        `travelled` and `elapsed` are passed by the gate, which has already
        worked out where and when the crossing really happened; left out, they
        are taken as of now.
        """
        now = time.monotonic() if now is None else now
        if travelled is None:
            travelled = self.lap_distance_m
        if elapsed is None:
            elapsed = (now - self._lap_start_ts
                       if self._lap_start_ts is not None else None)
        blind = self._can_is_dead(now) or self._distance_untrusted
        return bool((travelled is not None
                     and travelled >= track.MIN_LAP_DISTANCE_M)
                    or self._lap_gps_path_m >= track.MIN_LAP_DISTANCE_M
                    or (blind and elapsed is not None))

    def force_lap(self, source="manual", now=None):
        """Cut a lap now — the pit's manual override of the automatic trigger.

        For when the pit can see the count is one short. Re-datums distance,
        energy and the lap clock here, wherever the car is.

        It does NOT un-arm any more. It used to, so that the next passage of
        the line would re-datum instead of being judged against a 3800-4200 m
        window it could not meet. That job now belongs to the gate itself: any
        real passage that is not a lap re-syncs the datum (see
        _on_gate_crossing), and one that comes within RESYNC_AFTER_CUT_M of a
        manual cut is taken as the same boundary. Un-arming on top of that
        would turn the pit-exit passage — an official lap — into a "start"
        that counts nothing.

        ⚠️ Do not use this in the box as a routine. The passage of the gate on
        the way out already counts that lap, and would then only re-sync.
        """
        self._trigger_lap(source, now)

    def set_lap(self, lap_number, now=None):
        """Correct the lap NUMBER. Touches nothing else.

        For when the pit's count and the officials' differ, or after a Pi
        restart with no checkpoint. The lap in progress carries on: its metres,
        its energy and its clock are right, only its number was wrong.

        This used to restart the lap and un-arm as well, doubling as the "fresh
        lap when leaving the box" button. Leaving the box needs no button now
        (the in-lap closes at the gate in the pit lane), and re-datuming there
        would throw that official lap away. restart_lap() keeps the old
        behaviour for test sessions.
        """
        self.lap_count = max(0, int(lap_number))

    def restart_lap(self, now=None):
        """Start a fresh lap here WITHOUT counting one, and look for the line.

        The old "fresh lap, don't count it". For test sessions and for a lap
        that must be thrown away: the part-lap driven so far is recorded
        nowhere, and the next passage of the gate is a "start" that puts the
        datum on the line, exactly like a car's first ever sight of it.
        """
        self._trigger_lap("manual_restart", now, count_it=False)
        self._armed = False
        self._debt = None

    def new_race(self, now=None):
        """GREEN FLAG: everything this race has counted goes back to zero.

        The warm-up. The car rolls out, does an installation lap or three, and
        none of it is the race -- but the lap counter, the odometer and the
        energy totals do not know that, and until now the only way to clear
        them was to stop the car and delete lap_checkpoint.json by hand on the
        Pi, at the one moment nobody has a spare pair of hands. The pit sends
        this with the green flag instead (Pit_Web api_race -> new_race), and
        main._apply_lap_commands checkpoints straight afterwards, so a reboot
        mid-race restores the race and not the warm-up.

        THIS IS THE ONE RESET THAT CLEARS last_lap_*, and the difference from
        reset_energy() and reset_trip() is deliberate. Those two re-datum a
        running total and keep the finished lap's figures, because zeroing a
        counter does not unmeasure a lap that is already over and the pit has
        no other record of it. Here the finished lap is a WARM-UP lap being
        thrown away on purpose: carried into the race they would be published
        on every row of race lap 1, and the pit would record the warm-up as a
        completed lap of the race (db.fetch_laps groups on lap_seq together
        with those figures).

        Composed from the existing resets rather than re-listing their fields,
        so there is one definition of what zeroing energy or distance means.
        restart_lap() goes LAST: it re-datums the lap on a zeroed odometer and
        leaves the car un-armed, looking for the line exactly as it does on its
        first ever sight of the gate -- which is the true state of a car on the
        grid. It also re-datums the HUD stopwatch through the usual signal, so
        the driver's clock starts with the race.

        NOT reset: `_have_distance` / `_have_energy`, which say whether the CAN
        bus has ever fed us and are not race state; the rolling 2 h cell-extreme
        report, which is a scrutineering record (rule 3.5.6) and belongs to the
        day, not the race; and the controller's own hardware TRIP counter,
        which has no documented CAN reset -- see reset_trip().
        """
        self.reset_energy()
        self.reset_trip()

        self.lap_count = 0
        self.lap_seq = 0
        self.gps_lap_count = 0
        self.lap_source = LAP_SOURCE_NONE

        # The finished lap the other resets keep. See the docstring.
        self.last_lap_number = None
        self.last_lap_distance_m = None
        self.last_lap_energy_wh = None
        self.last_lap_regen_energy_wh = None
        self.last_lap_time_s = None
        self.last_lap_kind = None
        self.last_lap_flags = None
        self.last_lap_stopped_s = None
        self.last_lap_finished_ts = None

        # Gate diagnostics. They count what this tracker has seen and are read
        # as "how is the gate behaving today"; carrying the warm-up's rejected
        # crossings into the race makes that number answer the wrong question.
        self.rejected_crossings = 0
        self.resyncs = 0
        self.backward_crossings = 0
        self.last_rejected_distance_m = None
        self.last_cross_lateral_m = None

        # Last, and on the zeroed totals: a fresh lap, un-armed, looking for
        # the line. Clears _distance_untrusted too -- metres measured from here
        # are good, it is the metres before the reset that are not.
        self.restart_lap(now)

    def reset_energy(self):
        """Zero the energy totals without disturbing laps or distance.

        `last_lap_energy_wh` IS LEFT ALONE, and so is its regen counterpart.
        They are the DIFFERENCE between two totals taken on a lap that is
        already over -- zeroing the running totals does not make that lap cost
        any less, and the pit has no other record of what it cost. Nulling them
        here also split that lap in two on the pit: db.fetch_laps() groups a
        lap's rows on lap_seq TOGETHER WITH its figures, so a figure that
        changed mid-lap started a second group, and the lap was listed twice --
        in the History table, in the workbook, and inside the average.
        """
        self.total_energy_wh = 0.0
        self.regen_energy_wh = 0.0
        self.energy_gap_s = 0.0
        self._lap_start_energy_wh = 0.0
        self._lap_start_regen_energy_wh = 0.0
        self._stint_start_energy_wh = 0.0
        self._stint_start_regen_energy_wh = 0.0
        self._trail.clear()              # it holds the old totals

    def reset_trip(self):
        """Zero our own tracked distance totals without disturbing laps or energy.

        Does NOT reset the controller's own hardware TRIP counter (0x620) --
        there's no documented CAN command for that; see lap_command.py's
        reset_trip action and driver_message.send_trip_reset(). This only
        re-datums OUR running total, the same way reset_energy() only zeroes
        our own energy totals.

        _last_trip_m = None so the next real TRIP frame re-datums cleanly
        (self._last_trip_m is None, see update_odometer) instead of computing
        a bogus multi-hundred-km delta against the pre-reset value.

        The lap in progress loses its metres, so until the next cut the gate
        must not ask the odometer whether the car has been round.

        `last_lap_distance_m` survives, for the same reason last_lap_energy_wh
        survives reset_energy(): it is a finished lap's measured length, and
        re-datuming the odometer now cannot unmeasure it. Nulling it was also
        enough to make the pit list that lap twice -- see reset_energy()."""
        self.odometer_m = 0.0
        self._lap_start_odometer_m = 0.0
        self._last_trip_m = None
        self._distance_untrusted = True
        self._track_pos = None
        self._trail.clear()

    def mark_stint_start(self):
        """Re-datum 'current stint' to start counting from right now.

        Called by main.py the instant charge_detector.ChargeDetector reports a
        new charging stop beginning. Deliberately non-destructive — unlike
        reset_energy(), total_energy_wh and regen_energy_wh are left running
        for the whole race untouched. This only moves the baseline that
        snapshot()'s stint_energy/stint_regen_energy are measured FROM.

        Snapshotting at the START of the stop rather than its END is a
        simplification, not a compromise: total_energy_wh only integrates
        motor power (update_energy), and motor power is ~0 W while the car is
        parked being charged, so the two moments are numerically the same
        baseline. Using the start means the caller only has to watch for one
        edge (charging beginning), not two.
        """
        self._stint_start_energy_wh = self.total_energy_wh
        self._stint_start_regen_energy_wh = self.regen_energy_wh

    # ------------------------------------------------------------------ #
    # Read-outs                                                           #
    # ------------------------------------------------------------------ #
    @property
    def lap_distance_m(self):
        """Metres since the current lap's datum, by the odometer."""
        return self.odometer_m - self._lap_start_odometer_m

    @property
    def lap_energy_wh(self):
        """Wh spent since the current lap's datum, net of regen, or None.

        The running counterpart of last_lap_energy_wh, and the same basis:
        motor-side watt-hours with regen subtracting (update_energy). For the
        driver's HUD, which shows it when the pit re-datums the stopwatch
        mid-lap -- at that moment there is no finished lap to report, and what
        the part-lap has already cost is the useful number.

        None until something has actually fed the energy total: before that the
        difference would be 0.0, and a confident zero is the one thing this
        tracker never reports (see snapshot()).
        """
        if not self._have_energy:
            return None
        return self.total_energy_wh - self._lap_start_energy_wh

    @property
    def lap_start_ts(self):
        """time.monotonic() the current lap began, or None. For the HUD clock."""
        return self._lap_start_ts

    @property
    def zone(self):
        """"track", "pit_lane", "box", or None when GPS has not said."""
        if self._zone == ZONE_PIT and self._still_run_s >= BOX_STOP_S / 4.0:
            return ZONE_BOX
        return self._zone

    def track_pos_m(self, now=None):
        """Lap distance from GPS, carried forward by the odometer, or None.

        None in the pit lane, without a recent unambiguous fix, or when the
        odometer has been re-datumed since that fix.
        """
        now = self._clock if now is None else now
        if self._track_pos is None or now is None or self._zone == ZONE_PIT:
            return None
        s, fix_ts, odo = self._track_pos
        if now - fix_ts > TRACK_POS_MAX_AGE_S or self.odometer_m < odo:
            return None
        return (s + self.odometer_m - odo) % track.TRACK_LENGTH_METERS

    def profile_distance_m(self, now=None):
        """Where on the lap to look up the driver's target speed, or None.

        GPS when it is fresh, because it cannot be out of phase with the track
        the way a datum can; the odometer's lap distance otherwise. None in the
        pit lane: there is no target speed there, only the speed limit.
        """
        if self._zone == ZONE_PIT:
            return None
        pos = self.track_pos_m(now)
        if pos is not None:
            return pos
        return self.lap_distance_m if self._have_distance else None

    # ------------------------------------------------------------------ #
    # Telemetry                                                           #
    # ------------------------------------------------------------------ #
    def snapshot(self):
        """The keys merged into vehicle_state["motor"] and sent to the pit.

        `last_lap_*` are held for the WHOLE of the following lap rather than
        being emitted once at the trigger. That is what makes the pit's per-lap
        history robust: it only has to receive one sample anywhere in a lap to
        record that lap's figures exactly, instead of needing the two samples
        either side of a boundary. `last_lap_number` and `lap_seq` say which lap
        they belong to, so the pit never has to infer it from calculated_lap.

        None values become JSON null, which the pit already stores as NULL.

        Distance and energy are published as null until something has actually
        fed them (`_have_distance` / `_have_energy`). Before that they were sent
        as a flat 0.0 on every frame from boot, so the pit tile read a confident
        "0.00 km, Lap 0, 0 Wh" while the CAN bus was dead — the same zero-means-
        unknown lie the HUD gauges had. Nothing here is ever 0 for "unknown".

        Cheap on purpose: it runs on every motor-controller frame, so it only
        reads fields. All geometry happens once per GPS fix, in update_gps.
        """
        have_d, have_e = self._have_distance, self._have_energy
        counting = have_d or self.lap_seq > 0
        pos = self.track_pos_m()
        return {
            "odometer_m": self.odometer_m if have_d else None,
            "calculated_lap": self.lap_count if counting else None,
            # The lap being driven. calculated_lap is laps COMPLETED, and the
            # two were being confused one tab apart on the pit wall.
            "current_lap": self.lap_count + 1 if counting else None,
            "lap_seq": self.lap_seq if counting else None,
            # The sentinel is not a trigger, so it goes out as null and the pit
            # renders it as "—" like every other unreported field. Sending the
            # string "none" put a fifth, meaningless value in a tile whose whole
            # job is to name which of the four triggers cut the lap.
            "lap_source": (self.lap_source
                           if self.lap_source != LAP_SOURCE_NONE else None),
            "lap_distance_m": self.lap_distance_m if have_d else None,
            "track_pos_m": round(pos, 1) if pos is not None else None,
            "zone": self.zone,
            "lap_started_ts": self.lap_started_ts,
            "last_lap_number": self.last_lap_number,
            "last_lap_kind": self.last_lap_kind,
            "last_lap_flags": (",".join(self.last_lap_flags)
                               if self.last_lap_flags is not None else None),
            "last_lap_stopped_s": self.last_lap_stopped_s,
            "last_lap_distance_m": self.last_lap_distance_m,
            "last_lap_time_s": self.last_lap_time_s,
            # Watt-hours. NET of regen — see update_energy.
            "total_race_energy": round(self.total_energy_wh, 3) if have_e else None,
            "last_lap_energy": (round(self.last_lap_energy_wh, 3)
                                if self.last_lap_energy_wh is not None else None),
            "regen_energy": round(self.regen_energy_wh, 3) if have_e else None,
            "last_lap_regen_energy": (round(self.last_lap_regen_energy_wh, 3)
                                      if self.last_lap_regen_energy_wh is not None
                                      else None),
            # Since the last detected charging stop (see mark_stint_start).
            # Before any stop this race, the baseline is 0 and these read the
            # same as total_race_energy/regen_energy — there is only one
            # "stint" so far.
            "stint_energy": (round(self.total_energy_wh - self._stint_start_energy_wh, 3)
                             if have_e else None),
            "stint_regen_energy": (round(self.regen_energy_wh
                                        - self._stint_start_regen_energy_wh, 3)
                                   if have_e else None),
            "gps_lap_count": self.gps_lap_count,
            # Only while GPS is live: a distance to the line from a fix that
            # froze an hour ago is worse than none.
            "finish_line_distance_m": (
                round(self.finish_line_distance_m, 1)
                if self.finish_line_distance_m is not None and self._clock is not None
                and self._gps_is_healthy(self._clock) else None),
        }

    # ------------------------------------------------------------------ #
    # Reboot persistence                                                  #
    # ------------------------------------------------------------------ #
    def state_dict(self, now=None):
        """Everything needed to resume after a restart mid-race."""
        now = time.monotonic() if now is None else now
        return {
            "odometer_m": self.odometer_m,
            "total_energy_wh": self.total_energy_wh,
            "regen_energy_wh": self.regen_energy_wh,
            "lap_count": self.lap_count,
            "lap_seq": self.lap_seq,
            # Persisted with the lap it describes: lap_count comes back from the
            # checkpoint after a reboot, so the trigger that cut that lap has to
            # come back with it or the pit shows a restored lap with no source.
            "lap_source": self.lap_source,
            "gps_lap_count": self.gps_lap_count,
            "lap_start_odometer_m": self._lap_start_odometer_m,
            "lap_start_energy_wh": self._lap_start_energy_wh,
            "lap_start_regen_energy_wh": self._lap_start_regen_energy_wh,
            "stint_start_energy_wh": self._stint_start_energy_wh,
            "stint_start_regen_energy_wh": self._stint_start_regen_energy_wh,
            "last_lap_number": self.last_lap_number,
            "last_lap_kind": self.last_lap_kind,
            "last_lap_flags": (list(self.last_lap_flags)
                               if self.last_lap_flags is not None else None),
            "last_lap_stopped_s": self.last_lap_stopped_s,
            "last_lap_energy_wh": self.last_lap_energy_wh,
            "last_lap_regen_energy_wh": self.last_lap_regen_energy_wh,
            "last_lap_time_s": self.last_lap_time_s,
            "last_lap_distance_m": self.last_lap_distance_m,
            "armed": self._armed,
            # The lap in progress. A monotonic clock means nothing after a
            # reboot, so what is saved is how long the lap had been running.
            "lap_elapsed_s": ((now - self._lap_start_ts)
                              if self._lap_start_ts is not None else None),
            "lap_started_ts": self.lap_started_ts,
            "lap_origin": self._lap_origin,
            "lap_started_in_pit": self._lap_started_in_pit,
            "lap_stopped_s": self._lap_stopped_s,
            "lap_gps_path_m": self._lap_gps_path_m,
            "distance_untrusted": self._distance_untrusted,
            "zone": self._zone,
            "saved_at": time.time(),
        }

    def restore(self, data, now=None):
        """Reload a checkpoint. Ignores anything malformed rather than raising —
        a corrupt checkpoint must not stop the car's telemetry from starting.

        Reads with defaults throughout, so a checkpoint written by the previous
        version of this file loads, and one written by this version loads there:
        rolling the car back does not cost the race its lap count.
        """
        now = time.monotonic() if now is None else now
        if not isinstance(data, dict):
            return False
        try:
            self.odometer_m = float(data.get("odometer_m", 0.0))
            self.total_energy_wh = float(data.get("total_energy_wh", 0.0))
            self.regen_energy_wh = float(data.get("regen_energy_wh", 0.0))
            self.lap_count = int(data.get("lap_count", 0))
            self.lap_seq = int(data.get("lap_seq", self.lap_count))
            # A value from an older checkpoint, or a hand-edited file, must not
            # be able to invent a trigger: anything not in LAP_SOURCES falls
            # back to the sentinel.
            restored_source = data.get("lap_source")
            self.lap_source = (restored_source if restored_source in LAP_SOURCES
                               else LAP_SOURCE_NONE)
            self.gps_lap_count = int(data.get("gps_lap_count", 0))
            self._lap_start_odometer_m = float(
                data.get("lap_start_odometer_m", self.odometer_m))
            self._lap_start_energy_wh = float(
                data.get("lap_start_energy_wh", self.total_energy_wh))
            self._lap_start_regen_energy_wh = float(
                data.get("lap_start_regen_energy_wh", self.regen_energy_wh))
            self._stint_start_energy_wh = float(
                data.get("stint_start_energy_wh", 0.0))
            self._stint_start_regen_energy_wh = float(
                data.get("stint_start_regen_energy_wh", 0.0))
            self.last_lap_number = data.get("last_lap_number")
            kind = data.get("last_lap_kind")
            self.last_lap_kind = kind if kind in LAP_KINDS else None
            flags = data.get("last_lap_flags")
            self.last_lap_flags = (tuple(str(f) for f in flags)
                                   if isinstance(flags, (list, tuple)) else None)
            self.last_lap_stopped_s = data.get("last_lap_stopped_s")
            self.last_lap_energy_wh = data.get("last_lap_energy_wh")
            self.last_lap_regen_energy_wh = data.get("last_lap_regen_energy_wh")
            self.last_lap_time_s = data.get("last_lap_time_s")
            self.last_lap_distance_m = data.get("last_lap_distance_m")
            self._armed = bool(data.get("armed", False))
            origin = data.get("lap_origin")
            self._lap_origin = (origin if origin in ("gate", "virtual", "manual", "boot")
                                else "gate" if self._armed else "boot")
            self._lap_started_in_pit = bool(data.get("lap_started_in_pit", False))
            self._lap_stopped_s = float(data.get("lap_stopped_s", 0.0))
            self._lap_gps_path_m = float(data.get("lap_gps_path_m", 0.0))
            self._distance_untrusted = bool(data.get("distance_untrusted", False))
            zone = data.get("zone")
            self._zone = zone if zone in (ZONE_TRACK, ZONE_PIT) else None
            self._zone_inferred = True       # we were away; let GPS confirm it
            elapsed = data.get("lap_elapsed_s")
            elapsed = float(elapsed) if elapsed is not None else None
            saved_at = float(data.get("saved_at") or 0.0)
            started_wall = data.get("lap_started_ts")
        except (TypeError, ValueError):
            return False
        # A restored checkpoint IS a real basis for these totals, so they are
        # known again even before the first post-reboot frame arrives — otherwise
        # resuming a race would blank the pit's distance until the bus spoke.
        self._have_distance = True
        self._have_energy = True
        # Clocks are monotonic and meaningless across a reboot; start them fresh.
        self._last_motion_ts = None
        self._last_power_ts = None
        self._last_power_w = None
        self.last_lap_finished_ts = None
        # ...except the lap clock, which is rebuilt: the time the lap had run
        # when it was saved, plus the time the Pi was down when the wall clock
        # can be believed on both sides of it (a Pi has no RTC; see
        # lap_command._clock_is_plausible). The result can be negative — this
        # process's monotonic clock started after the lap did — and that is fine.
        if elapsed is not None:
            wall = time.time()
            if saved_at > _PLAUSIBLE_EPOCH and wall > saved_at:
                elapsed += wall - saved_at
            self._lap_start_ts = now - elapsed
            if isinstance(started_wall, (int, float)) and started_wall > _PLAUSIBLE_EPOCH:
                self.lap_started_ts = float(started_wall)
        else:
            self._lap_start_ts = None
        # Whatever happened while we were away is not in this lap's figures.
        self._lap_interrupted = True
        return True


# --------------------------------------------------------------------------- #
# Self-check:  python3 SolarRace_OS/modules/lap_tracker.py
#
# One scenario per situation a race weekend produces, each DRIVEN along the
# real centreline and the real pit lane with a noisy 1 Hz GPS, a 10 Hz motor
# frame and a steady power draw. Exits non-zero on the first failure.
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import random

    # THE GATE IS SWITCHED OFF ON THE CAR (track.CUT_LAP_ON_GATE) and ON for
    # everything below, because everything below is the gate's own logic --
    # arming, resync, the pit lane, backwards passages -- and that code has to
    # stay right for the day the switch goes back. What the car does TODAY,
    # with it off, is checked first, on its own.
    print("0. the gate switched off: a person cuts every lap")
    assert track.CUT_LAP_ON_GATE is False,         "the car is meant to ship with CUT_LAP_ON_GATE = False"
    _t = LapTracker()
    _t.update_distance_from_odometer(0.0) if hasattr(_t, "update_distance_from_odometer") else None
    _hit = (0.5, 0.0, 1.0)                  # a clean forward pass, mid-gate
    for _ in range(3):
        _out = _t._on_gate_crossing(_hit, 100.0, 101.0, 101.0)
        assert _out is None, "a gate crossing did something: %r" % (_out,)
    assert _t.lap_count == 0 and _t.lap_seq == 0 and not _t._armed,         "the gate counted, cut or armed with the switch off"
    _t.cut_lap(now=200.0) if hasattr(_t, "cut_lap") else None
    print("  ok    three clean gate passes: nothing counted, nothing armed, "
          "no datum moved")
    track.CUT_LAP_ON_GATE = True
    # The same for the distance fallback, which has been off on the car since
    # 2026-09-19 while section 4 below went on testing it -- and failing, in
    # a self-check nobody could then trust the rest of.
    assert track.CUT_LAP_ON_DISTANCE is False,         "the car is meant to ship with CUT_LAP_ON_DISTANCE = False"
    track.CUT_LAP_ON_DISTANCE = True

    L = track.TRACK_LENGTH_METERS
    # Every route piece that ends at the line runs this far past it — the last
    # 1 Hz fix before the end of a piece can be 16 m short — and the next piece
    # starts from there.
    PAST = 40.0
    _LAT0 = math.radians(track.FINISH_LINE_LAT)

    def _latlon(xy):
        return (track.FINISH_LINE_LAT + math.degrees(xy[1] / track._EARTH_RADIUS_M),
                track.FINISH_LINE_LON + math.degrees(
                    xy[0] / (track._EARTH_RADIUS_M * math.cos(_LAT0))))

    def _resample(points, step=2.0):
        """A polyline as (cumulative metres, xy) every `step` metres."""
        out, d = [(0.0, points[0])], 0.0
        for a, b in zip(points, points[1:]):
            seg = math.dist(a, b)
            n = max(1, int(seg / step))
            for k in range(1, n + 1):
                f = k / n
                out.append((d + seg * f, (a[0] + f * (b[0] - a[0]),
                                          a[1] + f * (b[1] - a[1]))))
            d += seg
        return out

    PIT = _resample(list(track_map.PITLANE_XY))
    PIT_LEN = PIT[-1][0]
    # Where along the pit lane the gate is, and our box: 35-40 m PAST the line,
    # as surveyed at Zolder on 2026-09-18. BOX_BEFORE_D is the other possible
    # layout (a box short of the line), which must work just as well.
    PIT_GATE_D = next(d for d, xy in PIT if track.gate_coords(xy)[0] >= 0.0)
    BOX_D = PIT_GATE_D + 37.0
    BOX_BEFORE_D = PIT_GATE_D - 40.0
    import zolder_pitlane
    PIT_ENTRY_S, PIT_EXIT_S = zolder_pitlane.ENTRY_S_M, zolder_pitlane.EXIT_S_M

    def on_track(s0, s1):
        """Route piece: the centreline from s0 to s1 (s1 may exceed a lap)."""
        return [(d - s0, track_map.position_at_distance(d))
                for d in [s0 + 2.0 * k for k in range(int((s1 - s0) / 2.0) + 1)]]

    def in_pit(d0, d1):
        return [(d - d0, xy) for d, xy in PIT if d0 <= d <= d1]

    class Car:
        """Drives routes and feeds a LapTracker the way main.py does."""

        def __init__(self, tracker=None, clock=1000.0, noise_m=2.0,
                     odo_scale=1.0, seed=1):
            self.t = tracker or LapTracker()
            self.clock = clock
            self.noise_m = noise_m
            self.odo_scale = odo_scale
            self.rng = random.Random(seed)
            self.xy = None
            self.gps_on = True
            self.can_on = True
            self.events = []
            self._gps_due = 0.0
            self._trip_due = 0.0
            self.true_m = 0.0
            self._rpm_per_kmh = 1000.0 / drivetrain.speed_kmh(1000)

        def _step(self, xy, kmh, wheel_kmh=None, dt=0.1, power_w=1500.0):
            self.clock += dt
            self.xy = xy
            # As on the car: RPM every frame (it is the wheel SPEED), and the
            # controller's TRIP counter at 1 Hz in 10 m steps (the DISTANCE).
            w = kmh if wheel_kmh is None else wheel_kmh
            self.true_m += w / 3.6 * dt
            self._trip_due -= dt
            if self.can_on:
                self.t.update_motion(w * self._rpm_per_kmh, now=self.clock)
                self.t.update_energy(power_w if w > 0 else 0.0, now=self.clock)
                if self._trip_due <= 0.0:
                    self._trip_due = 1.0
                    trip = math.floor(self.true_m * self.odo_scale / 10.0) * 10.0
                    self.t.update_odometer(trip, now=self.clock)
            self._gps_due -= dt
            if self._gps_due <= 0.0:
                self._gps_due = 1.0
                fix = None
                if self.gps_on:
                    lat, lon = _latlon((xy[0] + self.rng.gauss(0, self.noise_m),
                                        xy[1] + self.rng.gauss(0, self.noise_m)))
                    fix = {"lat": lat, "lon": lon, "fix_mode": 3, "stale": False,
                           "fix_age_s": 0.0, "speed_kmh": kmh}
                ev = self.t.update_gps(fix, now=self.clock)
                if ev:
                    self.events.append(ev)

        def drive(self, route, kmh):
            """Follow a route piece at a steady speed."""
            d, i, v = 0.0, 0, kmh / 3.6
            end = route[-1][0]
            while d < end:
                d += v * 0.1
                while i + 1 < len(route) and route[i + 1][0] <= d:
                    i += 1
                self._step(route[i][1], kmh)

        def park(self, seconds, wheel_kmh=0.0):
            for _ in range(int(seconds * 10)):
                self._step(self.xy, 0.0, wheel_kmh=wheel_kmh)

        def lap(self, kmh=60.0):
            self.drive(on_track(PAST, L + PAST), kmh)

    def check(ok, text):
        print(f"  {'ok  ' if ok else 'FAIL'}  {text}")
        if not ok:
            raise SystemExit(f"lap_tracker self-check failed: {text}")

    def near(a, b, tol):
        return a is not None and abs(a - b) <= tol

    # ------------------------------------------------------------------ #
    print("1. grid start, then flying laps")
    c = Car()
    c.drive(on_track(L - 60.0, L - 50.0), 20.0)
    c.park(40.0)                                    # on the grid, 50 m short
    check(c.t.zone != ZONE_BOX, "standing on the grid is not the box")
    c.drive(on_track(L - 50.0, L + PAST), 40.0)
    check(c.events == ["start"] and c.t.lap_count == 0,
          "first passage arms and counts nothing")
    c.lap(60.0)
    t = c.t
    check(c.events[-1] == "lap" and t.lap_count == 1 and t.lap_source == "gps",
          "lap 1 counted at the gate")
    c.lap(60.0)
    check(t.lap_count == 2 and t.last_lap_kind == "flying" and not t.last_lap_flags,
          f"lap 2 is a flying lap ({t.last_lap_kind}, {t.last_lap_flags})")
    check(near(t.last_lap_time_s, 240.0, 0.25),
          f"lap time interpolated AT the line: {t.last_lap_time_s:.2f} s (240.00)")
    check(near(t.last_lap_distance_m, L, 15.0),
          f"lap distance {t.last_lap_distance_m:.1f} m (TRIP counts in 10 m steps)")
    check(near(t.last_lap_energy_wh, 1500 * 240 / 3600, 0.3),
          f"lap energy {t.last_lap_energy_wh:.2f} Wh (100.00)")
    check(t.last_lap_finished_ts == t.lap_start_ts,
          "stopwatch: the finished lap and the new one share one instant")
    check(t.snapshot()["current_lap"] == 3 and t.snapshot()["last_lap_number"] == 2,
          "publishes the lap being driven AND the lap the figures belong to")

    # ------------------------------------------------------------------ #
    print("\n2. pit stop, box 37 m PAST the line (ours)")
    c.drive(on_track(PAST, PIT_ENTRY_S), 60.0)
    c.drive(in_pit(0.0, BOX_D), 30.0)
    check(t.lap_count == 3 and t.last_lap_kind == "in"
          and "ended_in_pit" in t.last_lap_flags and "stopped" not in t.last_lap_flags,
          f"the gate in the pit lane closes the in-lap on the way IN ({t.last_lap_kind})")
    check(t.zone == ZONE_PIT and t.profile_distance_m() is None,
          "pit lane recognised at the entry; no target speed in it")
    c.park(300.0)
    check(t.zone == ZONE_BOX and t.lap_count == 3 and t.resyncs == 0,
          "five minutes in the box, 37 m from the gate: no lap, no re-sync")
    c.drive(in_pit(BOX_D, PIT_LEN), 30.0)
    out_lap_m = t.lap_distance_m
    check(t.lap_count == 3 and near(out_lap_m, PIT_LEN - PIT_GATE_D, 15.0),
          f"out-lap metres count from the line: {out_lap_m:.0f} m at pit exit")
    c.drive(on_track(PIT_EXIT_S, PIT_EXIT_S + 150.0), 60.0)
    check(t.zone == ZONE_TRACK
          and near(t.profile_distance_m(), PIT_EXIT_S + 150.0, 20.0),
          f"back on track: target speed looked up at {t.profile_distance_m():.0f} m")
    c.drive(on_track(PIT_EXIT_S + 150.0, L + PAST), 60.0)
    check(t.lap_count == 4 and t.last_lap_kind == "out"
          and near(t.last_lap_stopped_s, 300.0, 15.0),
          f"out-lap counted, tagged, and holding the stop: "
          f"{t.last_lap_stopped_s} s standing")
    c.lap()
    check(t.lap_count == 5 and t.last_lap_kind == "flying",
          "and the next lap is flying again")
    check(t.rejected_crossings == 0 and t.resyncs == 0 and t.lap_source == "gps",
          "nothing rejected, nothing re-synced, never fell back to the odometer")

    print("\n2b. the same stop with a box 40 m BEFORE the line")
    k = Car(seed=9)
    k.drive(on_track(L - 100.0, L + PAST), 40.0)
    k.lap()
    k.drive(on_track(PAST, PIT_ENTRY_S), 60.0)
    k.drive(in_pit(0.0, BOX_BEFORE_D), 30.0)
    k.park(300.0)
    check(k.t.zone == ZONE_BOX and k.t.lap_count == 1,
          "five minutes in the box: no lap from GPS wander")
    k.drive(in_pit(BOX_BEFORE_D, PIT_LEN), 30.0)
    check(k.t.lap_count == 2 and k.t.last_lap_kind == "in"
          and near(k.t.last_lap_stopped_s, 300.0, 15.0),
          "the in-lap closes on the way OUT and holds the stop")
    k.drive(on_track(PIT_EXIT_S, L + PAST), 60.0)
    check(k.t.lap_count == 3 and k.t.last_lap_kind == "out", "then the out-lap")

    # ------------------------------------------------------------------ #
    print("\n3. drive-through")
    c.drive(on_track(PAST, PIT_ENTRY_S), 60.0)
    c.drive(in_pit(0.0, PIT_LEN), 40.0)
    check(t.lap_count == 6 and t.last_lap_kind == "in"
          and "stopped" not in t.last_lap_flags, "counted, tagged 'in', no stop")
    c.drive(on_track(PIT_EXIT_S, L + PAST), 60.0)
    check(t.lap_count == 7 and t.last_lap_kind == "out", "then an out-lap")

    # ------------------------------------------------------------------ #
    print("\n4. GPS lost over the line -> virtual crossing, datum stays in phase")
    c.drive(on_track(PAST, L - 300.0), 60.0)
    c.gps_on = False
    before = c.clock
    c.drive(on_track(L - 300.0, L + 350.0), 60.0)
    c.gps_on = True
    check(t.lap_count == 8 and t.lap_source == "odometer"
          and "virtual_end" in t.last_lap_flags,
          f"lap counted by distance ({t.last_lap_flags})")
    check(near(t.last_lap_distance_m, L, 0.5) and near(t.last_lap_time_s, 240.0, 1.5),
          f"cut BACK at 4000 m: {t.last_lap_distance_m:.1f} m, "
          f"{t.last_lap_time_s:.1f} s")
    c.drive(on_track(350.0, L + PAST), 60.0)
    check(t.lap_count == 9 and t.lap_source == "gps" and t.rejected_crossings == 0,
          "the very next passage counts by GPS (it used to be rejected for ~9 laps)")

    # ------------------------------------------------------------------ #
    print("\n5. tire constant 8 % out")
    w = Car(odo_scale=1.08, seed=2)
    w.drive(on_track(L - 100.0, L + PAST), 40.0)
    for _ in range(3):
        w.lap()
    check(w.t.lap_count == 3 and w.t.lap_source == "gps",
          f"every lap still counts at the gate ({w.t.last_lap_distance_m:.0f} m measured)")
    check(w.t.last_lap_kind == "suspect" and "distance_suspect" in w.t.last_lap_flags,
          "and is flagged, so nobody builds strategy on its metres")

    # ------------------------------------------------------------------ #
    print("\n6. parked in the box with a wandering GPS, then on a stand")
    p = Car(noise_m=6.0, seed=3)
    p.drive(on_track(L - 100.0, L + PAST), 40.0)
    p.lap()
    p.drive(on_track(PAST, PIT_ENTRY_S), 60.0)
    p.drive(in_pit(0.0, PIT_GATE_D - 8.0), 30.0)    # 8 m short of the gate
    laps = p.t.lap_count
    p.park(1200.0)
    check(p.t.lap_count == laps and p.t.resyncs == 0,
          "20 min, 8 m from the gate, 6 m GPS noise: no lap, datum untouched")
    p.park(600.0, wheel_kmh=80.0)                   # wheels spinning, car still
    check(p.t.zone == ZONE_BOX, "wheels spinning on a stand is still 'box'")
    check("stopped" in (p.t.last_lap_flags or ()) or p.t.lap_count == laps,
          f"any lap the odometer cut there is flagged ({p.t.last_lap_flags})")

    # ------------------------------------------------------------------ #
    print("\n7. pushed backwards over the gate, then forwards")
    b = Car(seed=4)
    b.drive(on_track(L - 100.0, L + PAST), 40.0)
    b.lap()
    b.lap()
    laps = b.t.lap_count
    back = [(d, xy) for d, xy in reversed(on_track(L - 15.0, L + 15.0))]
    back = [(i * 2.0, xy) for i, (_d, xy) in enumerate(back)]
    b.drive(back, 6.0)
    b.drive(on_track(L - 15.0, L + 15.0), 6.0)
    # At walking pace the GPS noise is as big as the step, so the gate may be
    # seen several times each way. What matters is the net result.
    check(b.t.backward_crossings >= 1 and b.t.lap_count == laps
          and b.t.resyncs == 0,
          f"backward + forward = nothing ({b.t.backward_crossings} backward "
          f"passage(s) seen, each cancelled)")

    # ------------------------------------------------------------------ #
    print("\n8. reboot in the box")
    r = Car(seed=5)
    r.drive(on_track(L - 100.0, L + PAST), 40.0)
    r.lap()
    r.drive(on_track(PAST, PIT_ENTRY_S), 60.0)
    r.drive(in_pit(0.0, BOX_D), 30.0)
    r.park(60.0)
    saved = r.t.state_dict(now=r.clock)
    elapsed_at_save = r.clock - r.t.lap_start_ts
    fresh = LapTracker()
    check(fresh.restore(saved, now=5.0), "checkpoint restores")
    r2 = Car(tracker=fresh, clock=5.0, seed=6)
    r2.xy = r.xy
    r2.park(30.0)
    r2.drive(in_pit(BOX_D, PIT_LEN), 30.0)
    r2.drive(on_track(PIT_EXIT_S, L + PAST), 60.0)
    check(fresh.lap_count == 3 and "interrupted" in fresh.last_lap_flags,
          f"the lap survives the reboot and says so ({fresh.last_lap_flags})")
    check(fresh.last_lap_time_s > elapsed_at_save + 30.0,
          f"its clock carried on: {fresh.last_lap_time_s:.0f} s "
          f"({elapsed_at_save:.0f} s before the reboot)")
    old = LapTracker()
    check(old.restore({"odometer_m": 9000.0, "lap_count": 2, "armed": True}),
          "a checkpoint from the previous version still loads")

    # ------------------------------------------------------------------ #
    print("\n9. pit commands")
    m = Car(seed=7)
    m.drive(on_track(L - 100.0, L + PAST), 40.0)
    m.lap()
    m.drive(on_track(PAST, 2700.0), 60.0)
    m.t.force_lap("manual", now=m.clock)
    check(m.t.lap_count == 2 and m.t.last_lap_kind == "suspect"
          and "manual_end" in m.t.last_lap_flags, "cut lap counts, flagged")
    m.drive(on_track(2700.0, L + PAST), 60.0)
    check(m.events[-1] == "resync" and m.t.lap_count == 2,
          "the next real passage re-syncs the datum and counts nothing")
    m.lap()
    check(m.t.lap_count == 3 and m.t.lap_source == "gps"
          and near(m.t.last_lap_distance_m, L, 15.0),
          "and the lap after is a clean 4000 m by GPS")
    m.drive(on_track(PAST, 1000.0), 60.0)
    lap_m = m.t.lap_distance_m
    m.t.set_lap(10)
    check(m.t.lap_count == 10 and m.t.lap_distance_m == lap_m and m.t._armed,
          "set_lap changes the number and nothing else")
    m.t.restart_lap(now=m.clock)
    check(m.t.lap_count == 10 and round(m.t.lap_distance_m) == 0,
          "restart_lap zeroes the lap without counting")
    m.drive(on_track(1000.0, L + PAST), 60.0)
    check(m.events[-1] == "start" and m.t.lap_count == 10,
          "... and the next passage is a 'start'")
    m.lap()
    check(m.t.lap_count == 11 and m.t.lap_seq == 4,
          "lap_seq ignores set_lap, so the pit can key on it")

    # ------------------------------------------------------------------ #
    print("\n10. motor controller silent: laps by GPS alone")
    g = Car(seed=8)
    g.can_on = False
    g.drive(on_track(L - 100.0, L + PAST), 40.0)
    g.lap()
    g.lap()
    snap = g.t.snapshot()
    check(g.t.lap_count == 2 and g.t.lap_source == "gps_no_can",
          "counted, tagged gps_no_can")
    check(snap["calculated_lap"] == 2 and snap["odometer_m"] is None
          and snap["last_lap_stopped_s"] is not None,
          "lap number published; the distance it never measured stays null")

    print("\nall lap scenarios passed")
