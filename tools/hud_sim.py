"""
hud_sim.py — drive the real driver HUD from a fake car, on any laptop.
=======================================================================
No CAN adapter, no GPS, no Firebase, no Raspberry Pi. It opens the SAME
driver_dash_v2.RacingDashboard the car runs and feeds its slots directly, so
what you see here is what the driver sees — layout, colours, thresholds and all.

    python tools/hud_sim.py                    # windowed, real time, hazard tour on
    python tools/hud_sim.py --speed 5          # five laps' worth per lap of clock
    python tools/hud_sim.py --fullscreen       # as it runs in the car
    python tools/hud_sim.py --profile lap33_290s
    python tools/hud_sim.py --no-tour          # no automatic hazards

    (or double-click "Start HUD Demo.bat" on Windows, ./"Start HUD Demo.sh" on the Pi)

Keys (simulator only — the HUD's own Ctrl+R / Ctrl+T / Alt+F4 still work):
    M   send a pit message        P   pause / resume the virtual car
    N   clear the pit message
    H   next hazard (stops tour)  X   clear the hazard
    R   hold hard regen on/off (brake light stays lit while it is on)

THE REGEN BRAKE LIGHT IS REAL HERE. On a Pi this drives the actual lamp on
GPIO 17 through modules.regen_light.RegenLight — the same object, thresholds
and minimum-on hold main.py uses, fed the fake car's motor power. So braking
into a corner in the simulator lights the bench lamp, and the HUD's status line
says BRAKE LIGHT ON at the same moment. Off the car (no gpiozero, or the pin
already claimed by a running HUD) nothing is driven and the logic still runs:
the startup line says which of the two you have. --no-regen-light skips it.

The virtual car drives the selected speed profile with a little lag and noise,
so the TARGET readout genuinely goes green and amber.

EVERY SCREEN HAS NUMBERS. DS003 (30 cell temperatures) and DS004 (26 cell
voltages) are fed from the same fake pack as the headline gauges, so the hottest
DS003 tile IS the MAX CELL gauge and the DS004 cells add up to the pack voltage.
Before, the simulator never fed them: DS003 sat on its "NOT CONFIGURED" sign
and DS004 was 26 dashes, so the two regulation screens could not be reviewed
at all without the car.

HAZARDS are the real ones, with the real labels. Alert text comes from the
same bit tables the car decodes (can_worker._ERROR_BITS / _LIMIT_BITS,
bms_parser._PROTECTION_BITS) and a "silent car" blanks exactly what
main.py._emit_zeros blanks — so the bench shows what the driver would see in
each failure, not an approximation of it.

The physics is plausible, not modelled: this is a display bench.
"""

import argparse
import math
import os
import random
import signal
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
# Same bootstrap the app does: repo root for drivetrain/speed_profile, and
# SolarRace_OS/ for the HUD and its modules.
for _p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "SolarRace_OS")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from PySide6.QtCore import Qt, QTimer                       # noqa: E402
from PySide6.QtGui import QKeySequence, QShortcut           # noqa: E402
from PySide6.QtWidgets import QApplication                  # noqa: E402

import drivetrain                                           # noqa: E402
import efficiency                                           # noqa: E402
import limits                                               # noqa: E402
from cell_extremes import RollingExtremes                   # noqa: E402
import speed_profile                                        # noqa: E402
from driver_dash_v2 import RacingDashboard, RACING_QSS      # noqa: E402
from modules import mms_parser, pt1000                      # noqa: E402
from modules.regen_light import RegenLight, REGEN_LIGHT_PIN  # noqa: E402

TICK_MS = 100                      # 10 Hz, the car's profile tick rate

# The map the controller reports in normal driving. Named THROUGH mms_parser,
# never typed here: this used to be a literal "Race", which the car never
# shows — raw 1 is "NORMAL MODE" on the real controller.
NORMAL_MAP_RAW = 1
REVERSE_MAP_RAW = 10

# The pack, as the car wires it: two 13-cell modules. DS004 shows 26 voltage
# taps; DS003 shows thermistor ids 1-13 (module A) and 21-33 (module B).
MODULE_CELLS = limits.CELL_COUNT
TOTAL_CELLS = 2 * MODULE_CELLS
CAR_MASS_KG = 250.0                # same road-load figure the 210 s baseline uses


def cruise_power_w(kmh: float) -> float:
    """Steady-speed power from the SAME road-load model the profiles were
    generated from: 210s.xlsx reproduces exactly as v x (11 N + 0.05 v^2), plus
    a little for the drivetrain. An earlier cubic guess read about twice this
    at race pace, which left no headroom to accelerate: the fake car sat at
    full drive power 42 % of every lap chasing a target it could never reach.
    """
    v = kmh / 3.6
    return 150.0 + v * (11.0 + 0.05 * v * v)

# What the car can physically do, from the 210 s baseline with its five join
# artifacts excluded (see tools/generate_profiles.py): 4.56 m/s2 either way.
# Without this cap the lag model below asked for 40 m/s2 pulling away from a
# standstill, which made "motor power" read 78 kW and kept Power Limit lit for
# a fifth of every run -- on a car whose recorded 99th percentile is 4.1 kW.
MAX_ACCEL_MS2 = 2.5                # pulling away, well inside the motor's grip
MAX_BRAKE_MS2 = 4.5
MOTOR_MAX_W = 5600.0               # the highest power the car has recorded
# What the driver normally asks of the motor when accelerating. Acceleration is
# limited by POWER as well as by grip: at 90 km/h a fixed 2.5 m/s2 needs about
# 15 kW, so a rate cap alone pinned every straight at MOTOR_MAX_W and kept
# Power Limit lit a quarter of the time. The real car's 99th percentile is
# 4.1 kW, so the limit should be an occasional event, not the normal state.
DRIVE_W = 3900.0

# What the R key feeds the light: hard regen, well below regen_light.ON_BELOW_W
# and inside the -3.5 kW the fake car can make on its own, so the bench sees the
# same power the car reports braking hard into a corner.
FORCED_REGEN_W = -900.0


def pedal_mv_for_power(power_w: float) -> float:
    """Motor power -> a plausible pedal voltage for the DS001 pedal bar.

    The fake car is driven by power, not by a pedal, so this runs the real
    car's relationship backwards: power above zero means the driver is asking
    for acceleration, power below zero means they have lifted into regen, and
    the pedal sits that far either side of efficiency.THROTTLE_MV_NEUTRAL.

    It is a PLAUSIBLE INVERSE, not a model of the real pedal. The car's actual
    pedal-to-power curve is the ESC's business and is not linear. What this has
    to get right is only what the bench is for: that the bar moves the right
    way, reaches both ends, and shows REGEN exactly when the brake light is
    lit — which it does, because both are driven from this same power figure.
    """
    span_up = efficiency.THROTTLE_MV_FULL - efficiency.THROTTLE_MV_NEUTRAL
    span_down = efficiency.THROTTLE_MV_NEUTRAL - efficiency.THROTTLE_MV_IDLE
    if power_w >= 0.0:
        frac = min(1.0, power_w / limits.POWER.full_scale)
        return efficiency.THROTTLE_MV_NEUTRAL + frac * span_up
    frac = min(1.0, abs(power_w) / abs(FORCED_REGEN_W))
    return efficiency.THROTTLE_MV_NEUTRAL - frac * span_down


def raw_rpm_for_speed(kmh: float) -> int:
    """Inverse of drivetrain.speed_kmh() — km/h back to a TRUE motor RPM.

    "True", not the controller's halved figure, because this feeds the HUD
    directly and bypasses mms_parser - and mms_parser is where the real car's
    2x under-report is corrected. Feeding the raw halved value here would make
    the simulator disagree with the car it is meant to stand in for.

    Drives the TACHOMETER only. The speedometer is fed separately from the
    controller's own speed field, so this does not determine what the sim's
    big km/h number reads.

    Derived from the drivetrain constants rather than a hard-coded factor, so a
    change to the sprockets or the tyre shows up in the simulator too.
    """
    wheel_rpm = kmh * 1000.0 / 60.0 / drivetrain.TIRE_CIRCUMFERENCE_METERS
    return int(round(wheel_rpm * drivetrain.GEAR_RATIO * drivetrain.MOTOR_POLE_PAIRS))


# A short script of pit traffic, so a passive watch still shows the banner
# appearing and — the point of it being an event, not a readout — going away
# again. (seconds since start, payload or None to clear)
PIT_SCRIPT = [
    (12.0, {"category": "STRATEGY", "value": "HOLD PACE"}),
    (20.0, None),
    (38.0, {"category": "BOX", "value": "THIS LAP"}),
    (48.0, None),
    (70.0, {"category": "", "value": "GOOD JOB — 2 LAPS TO GO"}),
    (80.0, None),
]


# ─────────────────────────────────────────────────────────────────────────────
#  Hazards
# ─────────────────────────────────────────────────────────────────────────────
# Each hazard edits one tick's FRAME (the values about to be pushed into the
# HUD) and returns it. Labels are pulled from the car's own decode tables by
# name, so a label renamed there cannot silently drift here.
def _label(table, text):
    for _bit, label in table:
        if label == text:
            return label
    raise KeyError(f"{text!r} is not a label the car can send")


def _mms(text):
    from can_worker import _ERROR_BITS, _LIMIT_BITS
    return _label(_ERROR_BITS + _LIMIT_BITS, text)


def _bms(text):
    from modules.bms_parser import _PROTECTION_BITS
    return _label(_PROTECTION_BITS, text)


def _hz_power_limit(f):
    f["power_w"] = 4650.0
    f["alerts"] = [(_mms("Power Limit"), "limit")]
    return f


def _hz_motor_thermal(f):
    f["motor_c"] = 128.0
    f["alerts"] = [(_mms("Motor Thermal Limit"), "limit")]
    return f


def _hz_motor_overtemp(f):
    f["motor_c"] = 146.0
    f["alerts"] = [(_mms("Motor Over-temp"), "error")]
    return f


def _hz_ctrl_overtemp(f):
    f["ctrl_c"] = 88.0
    f["alerts"] = [(_mms("Controller Over-temp"), "error")]
    return f


def _hz_low_battery(f):
    f["soc"] = 17.0
    f["cell_v"] = {i: 3.08 + 0.01 * math.sin(i) for i in range(1, TOTAL_CELLS + 1)}
    f["cell_v"][7] = 2.94          # one weak cell dragging the pack down
    f["voltage"] = sum(f["cell_v"][i] for i in range(1, MODULE_CELLS + 1))
    f["alerts"] = [(f"BMS A {_bms('Cell Undervoltage')}", "error")]
    return f


def _hz_hot_cell(f):
    f["cell_t"] = dict(f["cell_t"])
    f["cell_t"][25] = 58.0          # C_B5
    f["alerts"] = [(f"BMS B {_bms('Discharge Over-temp')}", "error")]
    return f


def _hz_reverse(f):
    f["map"] = (mms_parser.motor_map_name(REVERSE_MAP_RAW), REVERSE_MAP_RAW)
    f["speed"] = 3.0
    f["power_w"] = 180.0
    return f


def _hz_multi(f):
    # What a bad moment actually looks like: the bar shows the worst first and
    # caps at three, exactly as main.py._emit_alerts orders them.
    f["motor_c"] = 131.0
    f["power_w"] = 4400.0
    f["alerts"] = [(_mms("Hall Sensor Fault"), "error"),
                   (f"BMS A {_bms('Discharge Overcurrent')}", "error"),
                   (_mms("Motor Thermal Limit"), "limit")]
    return f


# (name, frame editor, special) — special is "can_error" or "silent" for the two
# failures that are not a value on a gauge but the car going away.
HAZARDS = [
    ("Power limit", _hz_power_limit, None),
    ("Motor thermal limit", _hz_motor_thermal, None),
    ("Motor over-temperature", _hz_motor_overtemp, None),
    ("Controller over-temperature", _hz_ctrl_overtemp, None),
    ("Low battery / weak cell", _hz_low_battery, None),
    ("Hot cell in module B", _hz_hot_cell, None),
    ("Reverse selected", _hz_reverse, None),
    ("Three faults at once", _hz_multi, None),
    ("CAN bus error", None, "can_error"),
    ("Car silent — no data", None, "silent"),
]

# The automatic tour, in REAL seconds so it stays readable at --speed 5:
# a quiet start, then each hazard in turn, with normal driving between them.
TOUR_START_S = 25.0
TOUR_ON_S = 9.0
TOUR_OFF_S = 11.0


class FakeCar:
    """A car that chases the profile's target speed instead of a real one."""

    def __init__(self, profile, time_scale: float):
        self.profile = profile
        self.time_scale = time_scale
        self.distance_m = 0.0
        self.speed_kmh = 0.0
        self.accel_ms2 = 0.0
        self.soc = 96.0
        self.motor_c = 31.0
        self.ctrl_c = 34.0
        self.cell_c = 27.0
        self.voltage = 53.5
        self.t = 0.0                       # simulated seconds since start
        self.paused = False
        self._script_idx = 0

        # Fixed per-cell character, so the grids look like a real pack (the
        # same cells run warm or low every lap) instead of flickering noise.
        rng = random.Random(7)
        self._temp_offset = {i: rng.uniform(-1.8, 1.8)
                             for _n, lo, hi in limits.THERMISTOR_GROUP_RANGES
                             for i in range(lo, hi + 1)}
        # Module B sits nearer the motor controller and runs a little warmer.
        for i in range(21, 34):
            self._temp_offset[i] += 1.5
        self._volt_offset = {i: rng.uniform(-0.012, 0.012)
                             for i in range(1, TOTAL_CELLS + 1)}

    def step(self, dt_s: float):
        """Advance the car by dt_s of SIMULATED time."""
        target = self.profile.speed_kmh_at(self.distance_m)

        # First-order lag towards the target plus a slow wander, so the driver
        # delta swings through the ±5 km/h tolerance instead of sitting exactly
        # on it — that is what makes the green/amber switch visible.
        wander = 3.5 * math.sin(self.t * 0.42) + random.uniform(-0.6, 0.6)
        before = self.speed_kmh
        wanted = self.speed_kmh + ((target + wander) - self.speed_kmh) * min(1.0, dt_s * 1.6)
        if wanted > before:
            v = max(before / 3.6, 3.0)
            cruise = cruise_power_w(before)
            a_cap = max(0.15, min(MAX_ACCEL_MS2, (DRIVE_W - cruise) / (CAR_MASS_KG * v)))
            step = a_cap * dt_s * 3.6
        else:
            step = MAX_BRAKE_MS2 * dt_s * 3.6
        self.speed_kmh = max(0.0, before + max(-step, min(step, wanted - before)))
        self.accel_ms2 = ((self.speed_kmh - before) / 3.6 / dt_s) if dt_s > 0 else 0.0

        self.distance_m += self.speed_kmh / 3.6 * dt_s
        self.t += dt_s

        load = self.speed_kmh / 90.0
        self.soc = max(4.0, self.soc - 0.02 * dt_s * load)
        # 13S pack: 3.0 V/cell empty to 4.2 V/cell full, sagging under load.
        self.voltage = (limits.CELL_COUNT
                        * (limits.CELL_V_CRIT
                           + (limits.CELL_V_MAX - limits.CELL_V_CRIT)
                           * self.soc / 100.0)
                        - 0.6 * max(0.0, load)
                        + random.uniform(-0.1, 0.1))
        # Tuned to the real car's record: median ~90 C, peak 135.5 C. The motor
        # runs hot on the straights and cools through the corners, so a normal
        # lap spends most of its time well under the 120 C thermal limit and
        # only a long flat-out stretch reaches it.
        self.motor_c += (30.0 + 82.0 * load - self.motor_c) * dt_s * 0.02
        self.ctrl_c += (34.0 + 30.0 * load - self.ctrl_c) * dt_s * 0.05
        self.cell_c += (26.0 + 16.0 * load - self.cell_c) * dt_s * 0.03

    @property
    def lap_distance_m(self) -> float:
        return self.distance_m % self.profile.lap_length_m

    @property
    def power_w(self) -> float:
        """Drag, rolling and drivetrain, plus the power to change speed.

        Negative when slowing: the controller really does report regen as
        negative power. Only part of the braking comes back — most goes into
        the friction brakes — and it is clamped near the -4 kW the car has
        actually been seen to make.
        """
        v = self.speed_kmh / 3.6
        cruise = cruise_power_w(self.speed_kmh)
        accel = CAR_MASS_KG * self.accel_ms2 * v
        if accel < 0:
            return max(cruise + 0.35 * accel, -3500.0)
        return min(cruise + accel, MOTOR_MAX_W)

    def cell_temps(self) -> dict:
        return {i: self.cell_c + off + random.uniform(-0.15, 0.15)
                for i, off in self._temp_offset.items()}

    def cell_voltages(self) -> dict:
        per_cell = self.voltage / limits.CELL_COUNT
        return {i: per_cell + off for i, off in self._volt_offset.items()}

    def due_pit_message(self):
        """Next scripted pit event whose time has passed, or False if none."""
        if self._script_idx >= len(PIT_SCRIPT):
            return False
        when, payload = PIT_SCRIPT[self._script_idx]
        if self.t < when:
            return False
        self._script_idx += 1
        return payload


def natural_alerts(frame) -> list:
    """Alerts the car raises by itself from what it is doing, no hazard needed.

    A long flat-out run really does heat the motor into its thermal limit, so
    the simulator should show that happening rather than only on a keypress.
    """
    out = []
    if frame["motor_c"] >= limits.MOTOR_TEMP.warn:
        out.append((_mms("Motor Thermal Limit"), "limit"))
    if frame["power_w"] >= limits.POWER.warn:
        out.append((_mms("Power Limit"), "limit"))
    return out


def build_sim(hud: RacingDashboard, car: FakeCar, strategy: str,
              tour: bool, light=None) -> QTimer:
    """Wire a timer that pushes the fake car into the HUD's real slots.

    `light` is a started RegenLight (or None). It is fed the fake car's motor
    power exactly where main.py feeds it the controller's — one call per frame
    of power, plus a tick() every pass so the minimum-on hold and the stale
    release run off the clock rather than off traffic.
    """

    normal_status = "● SIMULATION — no CAN bus"
    state = {
        "hazard": None,            # index into HAZARDS, or None
        "tour": tour,
        "real_s": 0.0,
        "last": {},                # last value pushed per slot, to push changes only
        "lap_idx": None,           # laps completed, to spot a line crossing
        "lap_start": None,         # time.monotonic() the current lap started
        "extremes_emit_t": -1.0,
        "force_regen": False,      # R key: hold the lamp on for a wiring check
        "lamp": False,             # last lamp state, for the console log
    }

    # Rule 3.5.6 screen. Driven by the SIMULATED clock, so a time-scaled run
    # ages readings out of the 2 h window at the simulated rate, exactly as the
    # car's monotonic clock would. Fed the same per-cell readings the DS003/
    # DS004 tiles get — hazard values included, so a hot or weak cell stays in
    # the report after the hazard has cleared.
    wall0 = time.time()
    extremes = RollingExtremes(mono=lambda: car.t, wall=lambda: wall0 + car.t)

    def push(key, fn, value):
        """Call a slot only when its value changed — the HUD restyles on every
        call, and ten restyles a second of an unchanged badge is not what the
        car does either."""
        if state["last"].get(key, object()) != value:
            state["last"][key] = value
            fn(value)

    def drive_lamp(power_w):
        """Feed one power reading to the brake light and log any change.

        Returns the lamp state. Called with None when the car is sending
        nothing at all, which feeds the light no reading: after STALE_AFTER_S
        the lamp releases itself, and watching that happen on the bench is the
        point of the "car silent" hazard.
        """
        if light is None:
            return False
        lit = light.update(power_w) if power_w is not None else light.tick()
        if lit != state["lamp"]:
            state["lamp"] = lit
            power = "—" if power_w is None else f"{power_w:7.0f} W"
            print(f"[sim] brake light {'ON ' if lit else 'off'}  "
                  f"(motor power {power}, flashes {light.flashes})")
        return lit

    def status_with_lamp(text):
        return f"{text}   🛑 BRAKE LIGHT ON" if state["lamp"] else text

    def set_hazard(idx):
        prev = state["hazard"]
        state["hazard"] = idx
        if prev is not None and HAZARDS[prev][2] is not None:
            # Leaving a bus failure: force every slot to be pushed again.
            state["last"].clear()
        if idx is None:
            print("[sim] hazard cleared")
        else:
            print(f"[sim] hazard {idx + 1}/{len(HAZARDS)}: {HAZARDS[idx][0]}")
            if HAZARDS[idx][2] == "can_error":
                hud._on_error("bus-off on can0 (simulated)")
            state["last"].clear()

    def frame_now():
        return {
            "speed": car.speed_kmh,
            "voltage": car.voltage,
            "soc": car.soc,
            "power_w": car.power_w,
            "motor_c": car.motor_c,
            "ctrl_c": car.ctrl_c,
            "cell_t": car.cell_temps(),
            "cell_v": car.cell_voltages(),
            "map": (mms_parser.motor_map_name(NORMAL_MAP_RAW), NORMAL_MAP_RAW),
            "alerts": None,        # None = derive from state
        }

    def show_silent():
        # No power frames at all, so the light gets no reading: it releases
        # after STALE_AFTER_S rather than staying stuck lit. See regen_light.py.
        drive_lamp(None)
        # Exactly what main.py._emit_zeros blanks.
        push("status", hud._on_status,
             status_with_lamp("● SILENT — no data from the car"))
        hud._on_rpm(None)
        hud._on_speed(None)
        hud._on_voltage(None)
        hud._on_soc(None)
        hud._on_power(None)
        hud._on_ctrl_temp(None)
        hud._on_motor_current(None)
        hud._on_battery_current(None)
        hud._on_cell_temp(None)
        hud._on_cell_temps(True, {})
        hud._on_cell_voltages(None, {})
        hud._on_bms_probe_temps({})
        hud._on_motor_temp(0.0, -1000.0, pt1000.STATUS_NO_READING)
        hud._on_pedal(None, None, None)
        push("alerts", hud._on_alerts, [])
        push("map", lambda m: hud._on_motor_map(*m), ("", -1))

    def tick():
        state["real_s"] += TICK_MS / 1000.0

        # The automatic tour, on the real clock.
        if state["tour"] and state["real_s"] >= TOUR_START_S:
            period = TOUR_ON_S + TOUR_OFF_S
            phase = state["real_s"] - TOUR_START_S
            n = int(phase // period)
            want = (n % len(HAZARDS)) if (phase % period) < TOUR_ON_S else None
            if want != state["hazard"]:
                set_hazard(want)

        if car.paused:
            # Still tick the lamp: its minimum-on hold and its stale release are
            # timers, and a frozen car is a car sending nothing.
            drive_lamp(None)
            return
        car.step(TICK_MS / 1000.0 * car.time_scale)

        hz = state["hazard"]
        special = HAZARDS[hz][2] if hz is not None else None
        if special == "silent":
            show_silent()
            return

        f = frame_now()
        if hz is not None and HAZARDS[hz][1] is not None:
            f = HAZARDS[hz][1](f)
        if state["force_regen"]:
            # R key. Set on the FRAME, not just on the light, so the power gauge
            # shows the same regen the lamp is being driven from.
            f["power_w"] = FORCED_REGEN_W

        # The brake light, driven from the frame's motor power — the same value
        # main.py hands it from mms_power_W, and before the HUD is touched, so
        # the lamp is never a frame behind what the driver sees.
        drive_lamp(f["power_w"])

        speed = f["speed"]
        hud._on_rpm(raw_rpm_for_speed(speed))
        hud._on_speed(speed)
        hud._on_voltage(f["voltage"])
        hud._on_soc(int(round(f["soc"])))
        hud._on_power(int(f["power_w"]))
        amps = f["power_w"] / max(1.0, f["voltage"])
        # The BMS sees about HALF the current, and negative on discharge:
        # measured on 2,424 moving samples, bms_current_A = -0.49 x (P / V).
        # Each pack has its own BMS and carries its share. Feeding the whole
        # P / V here showed 73 A in critical red on every straight, against a
        # recorded pack maximum of 36 A.
        hud._on_battery_current(-0.49 * amps)
        hud._on_motor_current(amps * 1.4)
        # The pedal bar, from the same power figure that drives the brake lamp
        # above — so REGEN on the bar and BRAKE LIGHT ON in the status line
        # always agree, which is the pair worth checking on a bench.
        pedal_mv = pedal_mv_for_power(f["power_w"])
        hud._on_pedal(pedal_mv,
                      efficiency.throttle_percent(pedal_mv)[0],
                      efficiency.regen_percent(pedal_mv)[0])
        hud._on_ctrl_temp(int(round(f["ctrl_c"])))
        hud._on_motor_temp(pt1000.ohms_from_celsius(f["motor_c"]),
                           f["motor_c"], pt1000.STATUS_OK)

        # DS003 and DS004. The MAX CELL gauge is the hottest DS003 tile, so the
        # two screens can never disagree about the same pack.
        hud._on_cell_temps(True, f["cell_t"])
        hud._on_cell_temp(max(f["cell_t"].values()))
        hud._on_cell_voltages(TOTAL_CELLS, f["cell_v"])
        # BMS NTC probes: each BMS sits on its module, so its three probes
        # read near that module's cells, a little cooler (they touch the
        # pack surface, not the cell core).
        probes = {}
        for pack, (lo, hi) in (("A", (1, 13)), ("B", (21, 33))):
            mod = [t for c, t in f["cell_t"].items() if lo <= c <= hi]
            if mod:
                base = sum(mod) / len(mod)
                probes[pack] = {n: round(base - 1.0 + off, 1)
                                for n, off in ((1, 0.6), (2, -0.9), (3, -1.2))}
        hud._on_bms_probe_temps(probes)
        for cell, deg_c in f["cell_t"].items():
            extremes.add_temp(cell, deg_c)
        for cell, volts in f["cell_v"].items():
            extremes.add_volt(cell, volts, TOTAL_CELLS)
        if car.t - state["extremes_emit_t"] >= 1.0:
            state["extremes_emit_t"] = car.t
            # The HUD formats times against the real clock; the report's are
            # on the simulated one, so shift them to "now" for display.
            report = extremes.result()
            shift = time.time() - (wall0 + car.t)
            for key in ("temp_max", "temp_min", "volt_max", "volt_min"):
                r = report.get(key)
                if r is not None:
                    report[key] = (r[0], r[1], r[2] + shift)
            hud._on_cell_extremes(report)

        hud._on_target_speed(car.profile.speed_kmh_at(car.lap_distance_m), strategy)

        # Lap stopwatch, on the REAL clock like the car's. The first tick is the
        # car leaving the line (clock starts, nothing held); every later wrap is
        # a finished lap. At --speed above 1 the laps are simply shorter.
        lap_idx = int(car.distance_m // car.profile.lap_length_m)
        if lap_idx != state["lap_idx"]:
            now = time.monotonic()
            finished = (None if state["lap_start"] is None
                        else now - state["lap_start"])
            state["lap_idx"], state["lap_start"] = lap_idx, now
            hud._on_lap_timer(now, finished)

        push("map", lambda m: hud._on_motor_map(*m), f["map"])

        if special != "can_error":
            alerts = f["alerts"] if f["alerts"] is not None else natural_alerts(f)
            push("alerts", hud._on_alerts, alerts[:3])
            status = (normal_status if hz is None
                      else f"● SIMULATION — hazard: {HAZARDS[hz][0]}")
            if state["force_regen"]:
                status = "● SIMULATION — REGEN HELD (R to release)"
            push("status", hud._on_status, status_with_lamp(status))

        payload = car.due_pit_message()
        if payload is not False:
            hud.set_pit_message(payload)

    timer = QTimer(hud)
    timer.timeout.connect(tick)
    timer.start(TICK_MS)

    # ── Simulator-only keys ──────────────────────────────────────────────── #
    # Plain letters: the HUD's own chords are all Ctrl/Alt based, so nothing
    # here can shadow a control that exists on the car.
    def send_msg():
        hud.set_pit_message({"category": "TEST", "value":
                             "PIT MESSAGE — PRESS N TO CLEAR"})

    def toggle_pause():
        car.paused = not car.paused
        push("status", hud._on_status, status_with_lamp(
            "● SIMULATION — PAUSED" if car.paused else normal_status))

    def toggle_regen():
        """Hold hard regen on, so the lamp stays lit while the wiring is checked.

        A corner gives a lamp that is on for a second or two — enough to see it
        work, not enough to walk to the back of the car and look at it.
        """
        state["force_regen"] = not state["force_regen"]
        if state["force_regen"]:
            print(f"[sim] forced regen ON ({FORCED_REGEN_W:.0f} W) — "
                  f"lamp stays lit until R again")
        else:
            print("[sim] forced regen off")

    def next_hazard():
        state["tour"] = False           # a human is driving the hazards now
        cur = state["hazard"]
        set_hazard(0 if cur is None else (cur + 1) % len(HAZARDS))

    def clear_hazard():
        state["tour"] = False
        set_hazard(None)

    QShortcut(QKeySequence("M"), hud, activated=send_msg)
    QShortcut(QKeySequence("N"), hud, activated=lambda: hud.set_pit_message(None))
    QShortcut(QKeySequence("P"), hud, activated=toggle_pause)
    QShortcut(QKeySequence("H"), hud, activated=next_hazard)
    QShortcut(QKeySequence("X"), hud, activated=clear_hazard)
    QShortcut(QKeySequence("R"), hud, activated=toggle_regen)
    return timer


def main() -> int:
    ap = argparse.ArgumentParser(description="Bench simulator for the driver HUD.")
    ap.add_argument("--profile", default=None,
                    help="profile name from profiles/ (default: lap33_290s)")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="time scale; 5 = five simulated seconds per real second")
    ap.add_argument("--fullscreen", action="store_true",
                    help="run as it does in the car (Alt+F4 or Ctrl+Shift+Q to quit)")
    ap.add_argument("--no-tour", action="store_true",
                    help="do not cycle through the hazards automatically")
    ap.add_argument("--no-regen-light", action="store_true",
                    help="do not touch GPIO: the brake light is not driven")
    ap.add_argument("--regen-pin", type=int, default=None,
                    help=f"BCM pin for the brake light (default {REGEN_LIGHT_PIN})")
    args = ap.parse_args()

    available = speed_profile.available_profiles()
    if not available:
        print("No profiles found in profiles/. Run tools/build_dor_profiles.py first.")
        return 1
    name = args.profile or ("lap33_290s" if "lap33_290s" in available
                            else sorted(available)[0])
    if name not in available:
        print(f"Unknown profile {name!r}. Available: {', '.join(sorted(available))}")
        return 1
    profile = speed_profile.load_csv(available[name], name=name)

    # Fail at startup, not mid-demo, if a hazard names a label the car can't send.
    for _n, fn, _s in HAZARDS:
        if fn is not None:
            fn({"cell_t": {}, "cell_v": {}, "motor_c": 0.0,
                "power_w": 0.0, "map": None})

    print(f"[sim] profile {name}  ·  lap {profile.lap_length_m:.0f} m  ·  "
          f"{profile.lap_time_s():.0f} s  ·  time scale ×{args.speed}")
    print("[sim] keys: M pit message · N clear · T turn warning · P pause · "
          "H next hazard · X clear hazard · R hold regen")
    if not args.no_tour:
        print(f"[sim] hazard tour: starts after {TOUR_START_S:.0f} s, "
              f"{len(HAZARDS)} hazards, {TOUR_ON_S:.0f} s each (H takes over)")
    # Plain ASCII arrows: a Windows console is usually cp1252, which cannot
    # encode the HUD's own triangle glyphs, and a print that raises here would
    # stop the simulator opening at all.
    print("[sim] pages: use the < > buttons for DS002 / DS003 / DS004 / R3.5.6")
    print("[sim] HUD keys: Alt+F4 / Ctrl+Shift+Q quit · Ctrl+Shift+C cursor")

    app = QApplication(sys.argv)
    app.setStyleSheet(RACING_QSS)
    hud = RacingDashboard()
    hud.setCursor(Qt.ArrowCursor)          # hidden on the car; wanted on a laptop
    hud.setWindowTitle(f"EV Racing HUD — SIMULATION ({name})")

    car = FakeCar(profile, args.speed)

    # The real brake light, the real pin, the real thresholds. It never raises:
    # no gpiozero, or a pin already held by a running HUD, and status() says so
    # while the logic carries on driving the on-screen indication.
    light = None
    if args.no_regen_light:
        print("🛑 regen brake light: disabled (--no-regen-light)")
    else:
        light = RegenLight(pin=args.regen_pin).start()
        print(f"🛑 {light.status()}")

        # PUTTING THE LAMP OUT ON THE WAY OUT — and why it is wrapped here
        # rather than hung on app.aboutToQuit alone.
        #
        # Every deliberate close of the HUD ends in RacingDashboard._fast_exit(),
        # which finishes with os._exit(): no aboutToQuit, no atexit, no
        # interpreter shutdown, by design (it refuses to wait on Firebase
        # threads while a person stares at a frozen window). gpiozero never gets
        # to release the pin, so GPIO 17 keeps whatever it was driving — and a
        # demo quit mid-flash left the bench lamp LIT with nothing running,
        # which is the stuck-on brake light regen_light.py exists to prevent,
        # arrived at from the other end.
        #
        # So the lamp is extinguished INSIDE that path. aboutToQuit stays
        # connected as well, for any exit that does unwind normally.
        _hud_fast_exit = hud._fast_exit

        def _fast_exit_lamp_off(code, why):
            light.stop()
            _hud_fast_exit(code, why)

        hud._fast_exit = _fast_exit_lamp_off
        app.aboutToQuit.connect(light.stop)

    build_sim(hud, car, name, tour=not args.no_tour, light=light)

    if args.fullscreen:
        hud.showFullScreen()
    else:
        hud.show()

    # Ctrl+C in the terminal, or a plain `kill`, has to put the lamp out.
    # Without this the demo dies with GPIO 17 still driven HIGH and the bench
    # lamp stays lit with nothing running — the stuck-on brake light that
    # regen_light.py's stale release exists to avoid, arrived at from the other
    # end. app.quit() unwinds through aboutToQuit, which calls light.stop().
    #
    # The 200 ms timer is what makes the signal handler run at all: Qt's event
    # loop sits in C between events, and Python only dispatches a caught signal
    # when it next executes bytecode. Without something waking the interpreter,
    # Ctrl+C is not seen until the next mouse move or keypress.
    def _bail(_sig, _frame):
        app.quit()

    for _sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(_sig, _bail)
    wake = QTimer(hud)
    wake.timeout.connect(lambda: None)
    wake.start(200)

    rc = app.exec()
    if light is not None:
        light.stop()               # belt and braces: aboutToQuit already did it
    return rc


if __name__ == "__main__":
    sys.exit(main())
