"""
hud_sim.py — drive the real driver HUD from a fake car, on any laptop.
=======================================================================
No CAN adapter, no GPS, no Firebase, no Raspberry Pi. It opens the SAME
driver_dash_v2.RacingDashboard the car runs and feeds its slots directly, so
what you see here is what the driver sees — layout, colours, thresholds and all.

    python tools/hud_sim.py                    # windowed, real time
    python tools/hud_sim.py --speed 5          # five laps' worth per lap of clock
    python tools/hud_sim.py --fullscreen       # as it runs in the car
    python tools/hud_sim.py --profile fast_189s

Keys (simulator only — the HUD's own Ctrl+R / Ctrl+T / Alt+F4 still work):
    M   send a pit message        T   force a turn warning for 6 s
    N   clear the pit message     P   pause / resume the virtual car

The virtual car drives the selected speed profile with a little lag and noise,
so the TARGET readout genuinely goes green and amber, and the corner warnings
fire off the same profile.look_ahead() the car uses — same window, same minimum
drop. Battery, temperatures and power are plausible rather than modelled: this
is a display bench, not a vehicle simulation.
"""

import argparse
import math
import os
import random
import sys

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
import speed_profile                                        # noqa: E402
from driver_dash_v2 import RacingDashboard, RACING_QSS      # noqa: E402
from modules import pt1000                                  # noqa: E402

# Matches main.py — the sim must warn about the same corners the car does.
TURN_LOOKAHEAD_M = 175.0
TURN_MIN_DROP_KMH = 15.0

TICK_MS = 100                      # 10 Hz, the car's profile tick rate


def raw_rpm_for_speed(kmh: float) -> int:
    """Inverse of drivetrain.speed_kmh() — km/h back to a raw CAN speed value.

    Derived from the drivetrain constants rather than a hard-coded factor, so a
    change to the sprockets or the tyre shows up in the simulator too. Getting
    this wrong is exactly the bug the shared drivetrain module exists to
    prevent: a HUD that reads 3 % off the pit's number for the same frame.
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


class FakeCar:
    """A car that chases the profile's target speed instead of a real one."""

    def __init__(self, profile, time_scale: float):
        self.profile = profile
        self.time_scale = time_scale
        self.distance_m = 0.0
        self.speed_kmh = 0.0
        self.soc = 96.0
        self.motor_c = 31.0
        self.ctrl_c = 34.0
        self.cell_c = 27.0
        self.voltage = 96.4
        self.t = 0.0                       # simulated seconds since start
        self.paused = False
        self._script_idx = 0
        self._last_turn_key = None

    def step(self, dt_s: float):
        """Advance the car by dt_s of SIMULATED time. Returns nothing; read the
        public attributes afterwards."""
        target = self.profile.speed_kmh_at(self.distance_m)

        # First-order lag towards the target plus a slow wander, so the driver
        # delta swings through the ±5 km/h tolerance instead of sitting exactly
        # on it — that is what makes the green/amber switch visible on the bench.
        wander = 3.5 * math.sin(self.t * 0.42) + random.uniform(-0.6, 0.6)
        self.speed_kmh += ((target + wander) - self.speed_kmh) * min(1.0, dt_s * 1.6)
        self.speed_kmh = max(0.0, self.speed_kmh)

        self.distance_m += self.speed_kmh / 3.6 * dt_s
        self.t += dt_s

        # Plausible-looking auxiliaries. Not a vehicle model — just enough
        # movement that every gauge on both screens has something to show.
        load = self.speed_kmh / 90.0
        self.soc = max(4.0, self.soc - 0.02 * dt_s * load)
        self.voltage = 84.0 + 0.16 * self.soc + random.uniform(-0.2, 0.2)
        self.motor_c += (32.0 + 46.0 * load - self.motor_c) * dt_s * 0.05
        self.ctrl_c += (34.0 + 30.0 * load - self.ctrl_c) * dt_s * 0.05
        self.cell_c += (26.0 + 16.0 * load - self.cell_c) * dt_s * 0.03

    @property
    def lap_distance_m(self) -> float:
        return self.distance_m % self.profile.lap_length_m

    @property
    def lap(self) -> int:
        return int(self.distance_m // self.profile.lap_length_m) + 1

    @property
    def power_w(self) -> float:
        # Roughly cubic in speed, as aero drag is, with a floor for the drivetrain.
        return 120.0 + 1900.0 * (self.speed_kmh / 90.0) ** 3

    def due_pit_message(self):
        """Next scripted pit event whose time has passed, or False if none.

        Returns the payload (possibly None, meaning "clear"), so `False` rather
        than `None` is the "nothing to do" answer.
        """
        if self._script_idx >= len(PIT_SCRIPT):
            return False
        when, payload = PIT_SCRIPT[self._script_idx]
        if self.t < when:
            return False
        self._script_idx += 1
        return payload

    def turn_ahead(self):
        """(distance, max_kmh, drop) for a corner worth warning about, or None.

        Change-detected exactly like main.py._tick_profile: without that the
        strip would be restyled ten times a second all the way to the apex.
        Returns False when nothing changed since the last call.
        """
        ahead = self.profile.look_ahead(self.lap_distance_m, TURN_LOOKAHEAD_M,
                                        TURN_MIN_DROP_KMH)
        key = None if ahead is None else (round(ahead[0] / 10), round(ahead[1]))
        if key == self._last_turn_key:
            return False
        self._last_turn_key = key
        return ahead


def build_sim(hud: RacingDashboard, car: FakeCar, strategy: str) -> QTimer:
    """Wire a timer that pushes the fake car into the HUD's real slots."""

    hud._on_status("● SIMULATION — no CAN bus")
    hud._on_alerts([])
    hud._on_motor_map("Race", 1)
    hud._on_vehicle_flags({"ecu_on": True, "parking_brake": False,
                           "lights_on": True, "reverse": False})

    forced_turn_until = {"t": -1.0}        # boxed so the closure can write it

    def tick():
        if car.paused:
            return
        dt = TICK_MS / 1000.0 * car.time_scale
        car.step(dt)

        speed = car.speed_kmh
        hud._on_rpm(raw_rpm_for_speed(speed))
        hud._on_voltage(car.voltage)
        hud._on_soc(int(round(car.soc)))
        hud._on_power(int(car.power_w))
        hud._on_battery_current(car.power_w / max(1.0, car.voltage))
        hud._on_motor_current(car.power_w / max(1.0, car.voltage) * 1.4)
        hud._on_ctrl_temp(int(round(car.ctrl_c)))
        hud._on_cell_temp(car.cell_c)
        hud._on_motor_temp(pt1000.ohms_from_celsius(car.motor_c),
                           car.motor_c, pt1000.STATUS_OK)

        hud._on_target_speed(car.profile.speed_kmh_at(car.lap_distance_m),
                             strategy)

        # Turn warning — the forced one (T key) wins while it is running, so a
        # manual test is not immediately overwritten by the profile.
        if car.t < forced_turn_until["t"]:
            pass
        else:
            ahead = car.turn_ahead()
            if ahead is not False:
                if ahead is None:
                    hud._on_turn_alert(0.0, 0.0, 0.0)
                else:
                    hud._on_turn_alert(*ahead)

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

    def force_turn():
        forced_turn_until["t"] = car.t + 6.0
        hud._on_turn_alert(120.0, 34.0, 48.0)
        QTimer.singleShot(6000, lambda: hud._on_turn_alert(0.0, 0.0, 0.0))

    def toggle_pause():
        car.paused = not car.paused
        hud._on_status("● SIMULATION — PAUSED" if car.paused
                       else "● SIMULATION — no CAN bus")

    QShortcut(QKeySequence("M"), hud, activated=send_msg)
    QShortcut(QKeySequence("N"), hud, activated=lambda: hud.set_pit_message(None))
    QShortcut(QKeySequence("T"), hud, activated=force_turn)
    QShortcut(QKeySequence("P"), hud, activated=toggle_pause)
    return timer


def main() -> int:
    ap = argparse.ArgumentParser(description="Bench simulator for the driver HUD.")
    ap.add_argument("--profile", default=None,
                    help="profile name from profiles/ (default: the first one)")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="time scale; 5 = five simulated seconds per real second")
    ap.add_argument("--fullscreen", action="store_true",
                    help="run as it does in the car (Alt+F4 or Ctrl+Shift+Q to quit)")
    args = ap.parse_args()

    available = speed_profile.available_profiles()
    if not available:
        print("No profiles found in profiles/. Run tools/generate_profiles.py first.")
        return 1
    name = args.profile or sorted(available)[0]
    if name not in available:
        print(f"Unknown profile {name!r}. Available: {', '.join(sorted(available))}")
        return 1
    profile = speed_profile.load_csv(available[name], name=name)

    print(f"[sim] profile {name}  ·  lap {profile.lap_length_m:.0f} m  ·  "
          f"{profile.lap_time_s():.0f} s  ·  time scale ×{args.speed}")
    print("[sim] keys: M pit message · N clear · T turn warning · P pause")
    print("[sim] HUD keys: Alt+F4 / Ctrl+Shift+Q quit · Ctrl+Shift+C cursor")

    app = QApplication(sys.argv)
    app.setStyleSheet(RACING_QSS)
    hud = RacingDashboard()
    # The cursor is hidden on the car; on a laptop you want it back.
    hud.setCursor(Qt.ArrowCursor)
    hud.setWindowTitle(f"EV Racing HUD — SIMULATION ({name})")

    car = FakeCar(profile, args.speed)
    build_sim(hud, car, name)

    if args.fullscreen:
        hud.showFullScreen()
    else:
        hud.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
