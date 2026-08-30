"""
lap_tracker.py — laps, distance, energy and lap timing, computed on the car
===========================================================================
One object owns all four, because they must agree with each other: a lap is
defined by distance, lap energy is the integral between two lap triggers, and
lap time is the interval between the same two triggers. Splitting them across
the code is how they drift apart.

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

# GPS counts as "healthy" for this long after the last usable fix.
GPS_HEALTH_TIMEOUT_S = 5.0

# If the motor controller has been silent this long, distance has stopped
# accruing and the distance gate can never open again. See _on_finish_crossing.
CAN_DEAD_AFTER_S = 30.0
GPS_ONLY_MIN_LAP_S = 60.0


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
        self.lap_source = "none"         # gps | odometer | manual | none
        self.gps_lap_count = 0
        self.rejected_crossings = 0
        self.last_rejected_distance_m = None

        self.last_lap_distance_m = None
        self.last_lap_energy_wh = None
        self.last_lap_regen_energy_wh = None
        self.last_lap_time_s = None
        self.lap_started_ts = time.time()          # wall clock, for the pit

        self._lap_start_odometer_m = 0.0
        self._lap_start_energy_wh = 0.0
        self._lap_start_regen_energy_wh = 0.0
        self._lap_start_ts = None                  # monotonic
        self._armed = False              # have we seen the line at least once?

        # --- GPS crossing state -------------------------------------------- #
        self._prev_xy = None
        self._prev_fix_ts = None
        self._in_finish_zone = False
        self._last_gps_ok_ts = None
        self.finish_line_distance_m = None

    # ------------------------------------------------------------------ #
    # Integrators — each owns its own clock                               #
    # ------------------------------------------------------------------ #
    def update_motion(self, rpm, now=None):
        """Integrate distance. Call ONLY for frames carrying `mms_rpm`.

        Skipped entirely once the controller's own odometer is available (see
        update_odometer): there is no point integrating an estimate when a real
        counter is on the bus.
        """
        now = time.monotonic() if now is None else now
        if self._last_motion_ts is not None and not self.using_controller_odo:
            dt = now - self._last_motion_ts
            if 0.0 < dt < MAX_SAMPLE_GAP_S:
                self.odometer_m += drivetrain.distance_metres(rpm, dt)
        self._last_motion_ts = now
        self._have_distance = True
        self._check_distance_fallback(now)

    def update_odometer(self, trip_m, now=None):
        """Adopt the controller's own distance counter (0x620 TRIP), in metres.

        Strongly preferred over integrating RPM, for two reasons:

        1. It is derived from the controller's configured wheel size rather than
           from drivetrain.TIRE_DIAMETER_METERS, which is still a placeholder
           nobody has measured. The 3.8-4.2 km lap gate is a +/-5% window, and a
           tire error of that order would sit on its edge.
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
        elif not self.using_controller_odo:
            print("🔢 using the controller's own TRIP counter for distance")

        self._last_trip_m = trip_m
        self.using_controller_odo = True
        self._last_motion_ts = now      # distance is live; CAN is not dead
        self._have_distance = True
        self._check_distance_fallback(now)

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

    # ------------------------------------------------------------------ #
    # GPS finish-line detection                                           #
    # ------------------------------------------------------------------ #
    def update_gps(self, fix, now=None):
        """Feed the newest gpsd fix. Returns "start", "lap" or None.

        "start" = the first crossing, which arms the tracker without counting a
        lap (see _on_finish_crossing). "lap" = a confirmed lap.
        """
        now = time.monotonic() if now is None else now

        if not fix or fix.get("stale") or (fix.get("fix_mode") or 0) < 2:
            # Break the segment chain: a line drawn from before a dropout to
            # after it is not a path the car took, and could sweep the finish
            # line from the far side of the circuit.
            self._prev_xy = None
            self._prev_fix_ts = None
            return None

        # gpsd is ~1 Hz but get_coordinates() returns the same fix on every
        # call with only fix_age_s advancing. Work out when the fix was actually
        # taken and ignore repeats, so duplicate polls don't collapse the
        # segment to zero length.
        fix_ts = now - float(fix.get("fix_age_s") or 0.0)
        if (self._prev_fix_ts is not None
                and (fix_ts - self._prev_fix_ts) < NEW_FIX_MIN_DT_S):
            return None

        self._last_gps_ok_ts = now
        xy = track.to_local_xy(fix["lat"], fix["lon"])
        point_distance = math.hypot(*xy)
        self.finish_line_distance_m = point_distance

        # Swept-segment test — see track.py for why this beats a radius test.
        test_distance = point_distance
        if self._prev_xy is not None:
            gap_s = fix_ts - self._prev_fix_ts
            chord = math.dist(self._prev_xy, xy)
            if gap_s <= MAX_GPS_SEGMENT_S and chord <= MAX_GPS_SEGMENT_M:
                test_distance, _t = track.segment_min_distance(self._prev_xy, xy)

        self._prev_xy, self._prev_fix_ts = xy, fix_ts

        if test_distance <= track.FINISH_RADIUS_M:
            if not self._in_finish_zone:
                self._in_finish_zone = True
                return self._on_finish_crossing(now)
        elif point_distance > track.FINISH_EXIT_RADIUS_M:
            self._in_finish_zone = False        # far enough away to re-arm
        return None

    def _on_finish_crossing(self, now):
        """Decide whether a detected crossing is actually a lap."""
        if not self._armed:
            # First sighting of the line. The distance since power-on is
            # meaningless (the car may have been pushed to the grid), so this
            # sets the datum instead of counting a lap. Every counted lap is
            # then line-to-line, which is what makes the 4 km gate meaningful.
            self._trigger_lap("gps_start", now, count_it=False)
            return "start"

        travelled = self.odometer_m - self._lap_start_odometer_m

        if track.LAP_DISTANCE_MIN_M <= travelled <= track.LAP_DISTANCE_MAX_M:
            self.gps_lap_count += 1
            self._trigger_lap("gps", now)
            return "lap"

        # Distance gate refused. If the motor controller has gone silent the
        # odometer is frozen and this gate can NEVER open again — without this
        # branch a dead CAN bus would silently stop lap counting for the rest of
        # the race even though GPS is working perfectly.
        if self._can_is_dead(now):
            elapsed = (now - self._lap_start_ts) if self._lap_start_ts else 0.0
            if elapsed >= GPS_ONLY_MIN_LAP_S:
                self.gps_lap_count += 1
                self._trigger_lap("gps_no_can", now)
                return "lap"

        self.rejected_crossings += 1
        self.last_rejected_distance_m = travelled
        print(f"🏁 crossing rejected: {travelled:.0f} m since last lap "
              f"(need {track.LAP_DISTANCE_MIN_M:.0f}-"
              f"{track.LAP_DISTANCE_MAX_M:.0f}). If every lap lands just outside "
              f"this window, the tire constant in drivetrain.py needs measuring.")
        return None

    def _can_is_dead(self, now):
        return (self._last_motion_ts is None
                or (now - self._last_motion_ts) > CAN_DEAD_AFTER_S)

    def _gps_is_healthy(self, now):
        return (self._last_gps_ok_ts is not None
                and (now - self._last_gps_ok_ts) < GPS_HEALTH_TIMEOUT_S)

    # ------------------------------------------------------------------ #
    # Distance-only fallback                                              #
    # ------------------------------------------------------------------ #
    def _check_distance_fallback(self, now):
        """Count laps from distance when GPS can't, so counting never stops."""
        if not self._armed:
            # Never seen the finish line: behave exactly like the old
            # odometer // TRACK_LENGTH counter, so a car with no GPS at all is
            # no worse off than before this feature existed.
            limit = track.TRACK_LENGTH_METERS
        elif self._gps_is_healthy(now):
            # GPS is working but hasn't reported a crossing well past a full
            # lap — detection missed. Force the lap so the count keeps up, and
            # tag it "odometer" so the pit can see the GPS trigger failed.
            limit = track.ODOMETER_FORCE_LAP_M
        else:
            limit = track.LAP_DISTANCE_MAX_M

        if (self.odometer_m - self._lap_start_odometer_m) >= limit:
            self._trigger_lap("odometer", now)

    # ------------------------------------------------------------------ #
    # The one place a lap is ever cut                                     #
    # ------------------------------------------------------------------ #
    def _trigger_lap(self, source, now=None, count_it=True):
        """Snapshot the completed lap and re-datum for the next one.

        GPS, the distance fallback and the pit's manual Cut Lap all funnel
        through here, so a lap cut by any route produces exactly the same
        bookkeeping and there is no second copy to keep in step.
        """
        now = time.monotonic() if now is None else now

        if count_it:
            self.lap_count += 1
            self.last_lap_distance_m = self.odometer_m - self._lap_start_odometer_m
            self.last_lap_energy_wh = self.total_energy_wh - self._lap_start_energy_wh
            self.last_lap_regen_energy_wh = (
                self.regen_energy_wh - self._lap_start_regen_energy_wh)
            self.last_lap_time_s = (
                (now - self._lap_start_ts) if self._lap_start_ts is not None else None)
            self.lap_source = source

        self._lap_start_odometer_m = self.odometer_m
        self._lap_start_energy_wh = self.total_energy_wh
        self._lap_start_regen_energy_wh = self.regen_energy_wh
        self._lap_start_ts = now
        self.lap_started_ts = time.time()      # wall clock for the pit's stopwatch
        self._armed = True

    # ------------------------------------------------------------------ #
    # Commands from the pit                                               #
    # ------------------------------------------------------------------ #
    def force_lap(self, source="manual", now=None):
        """Cut a lap now — the pit's manual override of the automatic trigger."""
        self._trigger_lap(source, now)
        # Re-arm GPS immediately: after a manual cut the car may well be sitting
        # on the line, and leaving the zone latched would swallow the next
        # genuine crossing.
        self._in_finish_zone = False

    def set_lap(self, lap_number, now=None):
        """Correct the lap counter (pit recovery, e.g. after a Pi restart)."""
        self.lap_count = max(0, int(lap_number))
        self._trigger_lap("manual_set", now, count_it=False)

    def reset_energy(self):
        """Zero the energy totals without disturbing laps or distance."""
        self.total_energy_wh = 0.0
        self.regen_energy_wh = 0.0
        self.energy_gap_s = 0.0
        self._lap_start_energy_wh = 0.0
        self._lap_start_regen_energy_wh = 0.0
        self._stint_start_energy_wh = 0.0
        self._stint_start_regen_energy_wh = 0.0
        self.last_lap_energy_wh = None
        self.last_lap_regen_energy_wh = None

    def reset_trip(self):
        """Zero our own tracked distance totals without disturbing laps or energy.

        Does NOT reset the controller's own hardware TRIP counter (0x620) --
        there's no documented CAN command for that; see lap_command.py's
        reset_trip action and driver_message.send_trip_reset(). This only
        re-datums OUR running total, the same way reset_energy() only zeroes
        our own energy totals.

        _last_trip_m = None so the next real TRIP frame re-datums cleanly
        (self._last_trip_m is None, see update_odometer) instead of computing
        a bogus multi-hundred-km delta against the pre-reset value."""
        self.odometer_m = 0.0
        self._lap_start_odometer_m = 0.0
        self._last_trip_m = None
        self.last_lap_distance_m = None

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
    # Telemetry                                                           #
    # ------------------------------------------------------------------ #
    def snapshot(self):
        """The keys merged into vehicle_state["motor"] and sent to the pit.

        `last_lap_energy` / `last_lap_time_s` are held for the WHOLE of the
        following lap rather than being emitted once at the trigger. That is
        what makes the pit's per-lap history robust: it only has to receive one
        sample anywhere in a lap to record that lap's figures exactly, instead
        of needing the two samples either side of a boundary.

        None values become JSON null, which the pit already stores as NULL.

        Distance and energy are published as null until something has actually
        fed them (`_have_distance` / `_have_energy`). Before that they were sent
        as a flat 0.0 on every frame from boot, so the pit tile read a confident
        "0.00 km, Lap 0, 0 Wh" while the CAN bus was dead — the same zero-means-
        unknown lie the HUD gauges had. Lap count follows distance because it is
        gated on it (see _check_distance_fallback).
        """
        have_d, have_e = self._have_distance, self._have_energy
        return {
            "odometer_m": self.odometer_m if have_d else None,
            "calculated_lap": self.lap_count if have_d else None,
            "lap_source": self.lap_source,
            "lap_distance_m": (self.odometer_m - self._lap_start_odometer_m
                               if have_d else None),
            "lap_started_ts": self.lap_started_ts,
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
            "finish_line_distance_m": (round(self.finish_line_distance_m, 1)
                                       if self.finish_line_distance_m is not None
                                       else None),
        }

    # ------------------------------------------------------------------ #
    # Reboot persistence                                                  #
    # ------------------------------------------------------------------ #
    def state_dict(self):
        """Everything needed to resume after a restart mid-race."""
        return {
            "odometer_m": self.odometer_m,
            "total_energy_wh": self.total_energy_wh,
            "regen_energy_wh": self.regen_energy_wh,
            "lap_count": self.lap_count,
            "gps_lap_count": self.gps_lap_count,
            "lap_start_odometer_m": self._lap_start_odometer_m,
            "lap_start_energy_wh": self._lap_start_energy_wh,
            "lap_start_regen_energy_wh": self._lap_start_regen_energy_wh,
            "stint_start_energy_wh": self._stint_start_energy_wh,
            "stint_start_regen_energy_wh": self._stint_start_regen_energy_wh,
            "last_lap_energy_wh": self.last_lap_energy_wh,
            "last_lap_regen_energy_wh": self.last_lap_regen_energy_wh,
            "last_lap_time_s": self.last_lap_time_s,
            "last_lap_distance_m": self.last_lap_distance_m,
            "armed": self._armed,
            "saved_at": time.time(),
        }

    def restore(self, data):
        """Reload a checkpoint. Ignores anything malformed rather than raising —
        a corrupt checkpoint must not stop the car's telemetry from starting."""
        if not isinstance(data, dict):
            return False
        try:
            self.odometer_m = float(data.get("odometer_m", 0.0))
            self.total_energy_wh = float(data.get("total_energy_wh", 0.0))
            self.regen_energy_wh = float(data.get("regen_energy_wh", 0.0))
            self.lap_count = int(data.get("lap_count", 0))
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
            self.last_lap_energy_wh = data.get("last_lap_energy_wh")
            self.last_lap_regen_energy_wh = data.get("last_lap_regen_energy_wh")
            self.last_lap_time_s = data.get("last_lap_time_s")
            self.last_lap_distance_m = data.get("last_lap_distance_m")
            self._armed = bool(data.get("armed", False))
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
        self._lap_start_ts = None
        return True


# --------------------------------------------------------------------------- #
# Self-check:  python3 SolarRace_OS/modules/lap_tracker.py
# Simulates a lap: drive 4 km at 60 km/h drawing 1500 W, then cross the line.
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    t = LapTracker()
    clock = 1000.0
    lat_per_m = 1.0 / 111_320.0

    def gps_at(along_m, offset_m=3.0):
        return {"lat": track.FINISH_LINE_LAT + offset_m * lat_per_m,
                "lon": track.FINISH_LINE_LON + (along_m * lat_per_m
                       / math.cos(math.radians(track.FINISH_LINE_LAT))),
                "fix_mode": 3, "stale": False, "fix_age_s": 0.0}

    # Cross the line once to arm.
    print("arming pass over the line:")
    for along in (-60, -30, 0, 30, 60):
        clock += 1.0
        ev = t.update_gps(gps_at(along), now=clock)
        if ev:
            print(f"  {along:+5.0f} m -> {ev} (lap {t.lap_count})")

    # Drive a lap: 60 km/h needs 240 s for 4 km. RPM for 60 km/h:
    rpm = 60.0 / drivetrain.speed_kmh(1000) * 1000
    print(f"\ndriving 4 km at 60 km/h ({rpm:.0f} motor rpm, 1500 W)...")
    for _ in range(2400):                    # 0.1 s steps
        clock += 0.1
        t.update_motion(rpm, now=clock)
        t.update_energy(1500.0, now=clock)
    print(f"  odometer  {t.odometer_m:8.1f} m")
    print(f"  energy    {t.total_energy_wh:8.2f} Wh  "
          f"(expect 1500 W x 240 s = {1500 * 240 / 3600:.2f})")

    print("\nsecond pass over the line:")
    for along in (-60, -30, 0, 30, 60):
        clock += 1.0
        ev = t.update_gps(gps_at(along), now=clock)
        if ev:
            print(f"  {along:+5.0f} m -> {ev}")
    print(f"\n  lap count       {t.lap_count}")
    print(f"  lap source      {t.lap_source}")
    print(f"  last lap dist   {t.last_lap_distance_m:.1f} m")
    print(f"  last lap energy {t.last_lap_energy_wh:.2f} Wh")
    print(f"  last lap time   {t.last_lap_time_s:.1f} s")
