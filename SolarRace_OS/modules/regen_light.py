"""
regen_light.py — brake light driven by regenerative braking
============================================================
The motor controller reports NEGATIVE power when the car is recovering energy,
which means the car is slowing down — so the brake light has to come on, exactly
as it would for the foot brake. Nothing else on the car knows that regen is
happening: the ESC will not drive a light, and the brake pedal switch does not
move when the driver lifts and lets regen do the slowing.

    light = RegenLight().start()
    light.update(power_w)        # whenever mms_power_W arrives
    light.tick()                 # every loop pass, frames or no frames
    light.stop()                 # on shutdown: the light is turned OFF

⚠️ ELECTRICAL — READ THIS BEFORE CONNECTING ANYTHING
**A Raspberry Pi GPIO pin cannot drive a brake light.** The pin is 3.3 V and
good for about 16 mA; a 12 V bulb wants amps, and an LED cluster still wants far
more than the pin can give. Wiring a lamp to GPIO 17 destroys the Pi, probably
along with the telemetry for the rest of the race.

The pin drives a SWITCH, and the switch drives the lamp:

    GPIO 17 ──[220R]──┬── gate, logic-level N-channel MOSFET (e.g. IRLZ44N)
                      │   (or an opto-isolated solid state relay)
                    [10k]
                      │
    Pi GND ───────────┴── MOSFET source ── 12 V system ground (COMMON with Pi GND)

    12 V ── brake light ── MOSFET drain
    Flyback diode across the lamp if it is anything inductive (a relay coil).

The 10k pulls the gate down so the lamp is OFF while the Pi is booting and the
pin is still an undriven input. Without it the light can glow or flicker during
boot, which is worse than useless on a brake light. Grounds must be common or
the MOSFET has no reference; if you would rather keep them isolated, use an
opto-isolated SSR instead and skip the shared ground.

WHY THERE IS A MINIMUM ON TIME
Regen comes and goes in fractions of a second as the driver modulates. A light
that follows the raw signal strobes, which is both illegible to a following
driver and, in most regulations, not a brake light at all. MIN_ON_S holds it lit
for a beat after regen stops, so a short lift reads as one clean flash.

WHY IT TURNS ITSELF OFF WHEN THE BUS GOES QUIET
update() is called from the CAN worker thread. If that thread stalls — a hung
network write used to be able to do this — the last commanded state would stick
forever. A brake light frozen ON tells every following driver the car is braking
when it is not, every lap, for the rest of the race. So the lamp is released
after STALE_AFTER_S with no fresh power reading: an unlit light with a dead bus
is a fault the crew can see, a permanently lit one looks deliberate.

WITHOUT THE HARDWARE
If gpiozero is missing (a laptop) or the pin cannot be claimed, everything still
runs and the state is tracked in software — status() says plainly that nothing
is being driven. The car must not fail to start because a lamp is not wired.
"""

import threading
import time

# BCM numbering (GPIO n), not physical header position. GPIO 17 is header pin 11
# and is clear of everything the CAN HAT uses — SPI0 (7-11), its interrupt lines
# (commonly 24/25), I2C (2, 3) and the HAT ID EEPROM (0, 1).
REGEN_LIGHT_PIN = 17

# Hysteresis, in watts of MOTOR power (negative = regenerating).
#
# Chosen from the car's own recorded telemetry, not picked out of the air: real
# regen sits below -100 W (2.5% of stored samples, down to -6178 W), while the
# -20..0 W band holds only 0.4% and 59% of samples read exactly 0 W. So -50 W
# catches every meaningful regen event and the 30 W dead band stops the lamp
# chattering while power dithers around zero.
ON_BELOW_W = -50.0
OFF_ABOVE_W = -20.0

# How long the lamp stays lit after regen stops. Long enough to read as a flash.
MIN_ON_S = 0.5

# No power reading for this long and the lamp is released. See the module note.
STALE_AFTER_S = 2.0

# True: pin HIGH switches the lamp on (a MOSFET gate, as wired above).
ACTIVE_HIGH = True


class RegenLight:
    """Drives a brake light from regenerative braking. Never raises.

    Thread note: update() is called from the CAN worker thread and tick() from
    the same loop, so there is one writer. The lock only guards the handful of
    scalars that status() reads, which the boot log touches from elsewhere.
    """

    def __init__(self, pin=None, on_below_w=ON_BELOW_W, off_above_w=OFF_ABOVE_W,
                 min_on_s=MIN_ON_S, stale_after_s=STALE_AFTER_S,
                 active_high=ACTIVE_HIGH):
        self.pin = REGEN_LIGHT_PIN if pin is None else pin
        self.on_below_w = float(on_below_w)
        self.off_above_w = float(off_above_w)
        self.min_on_s = float(min_on_s)
        self.stale_after_s = float(stale_after_s)
        self.active_high = bool(active_high)

        self._lock = threading.Lock()
        self._device = None
        self._error = None
        self.lit = False            # what the lamp is being told to do
        self.regen = False          # what the power reading says, before hold
        self._lit_since = 0.0
        self._last_power_ts = 0.0
        self.flashes = 0            # proves it is working, from the console

    # ------------------------------------------------------------------ #
    def start(self):
        """Claim the pin. Safe anywhere; never raises."""
        if self.pin is None:
            self._error = "no pin configured"
            return self
        try:
            from gpiozero import DigitalOutputDevice
        except Exception as exc:
            self._error = f"gpiozero unavailable ({exc})"
            return self
        try:
            # active_high matches the MOSFET wiring in the module docstring, and
            # initial_value=False means the lamp is explicitly OFF the moment we
            # take the pin rather than whatever the pin happened to be doing.
            self._device = DigitalOutputDevice(
                self.pin, active_high=self.active_high, initial_value=False)
        except Exception as exc:
            self._error = f"GPIO {self.pin} unavailable ({exc})"
        return self

    def stop(self):
        """Release the pin, lamp OFF. A brake light left lit after shutdown is
        a light nobody is going to notice is lying."""
        with self._lock:
            self.lit = False
            self.regen = False
            dev, self._device = self._device, None
        if dev is not None:
            try:
                dev.off()
            except Exception:
                pass
            try:
                dev.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    def update(self, power_w, now=None):
        """Feed one motor-power reading. Returns whether the lamp is lit."""
        now = time.monotonic() if now is None else now
        try:
            power_w = float(power_w)
        except (TypeError, ValueError):
            return self.lit                     # unreadable: change nothing
        with self._lock:
            self._last_power_ts = now
            if power_w <= self.on_below_w:
                self.regen = True
            elif power_w >= self.off_above_w:
                self.regen = False
            # between the two thresholds: hold whatever it was doing
        return self._apply(now)

    def tick(self, now=None):
        """Call every loop pass, frames or not.

        Without this the minimum-on hold would never expire on a quiet bus and
        the staleness release would never fire — both of which are timers, not
        events, and cannot wait for the next CAN frame to be evaluated.
        """
        return self._apply(time.monotonic() if now is None else now)

    def _apply(self, now):
        with self._lock:
            regen, lit = self.regen, self.lit
            stale = (self._last_power_ts
                     and (now - self._last_power_ts) > self.stale_after_s)
            if stale:
                # The bus has gone quiet. Release rather than hold a lit lamp:
                # see the module note on why OFF is the safer stuck state.
                self.regen = regen = False

            if regen:
                want = True
            elif lit and (now - self._lit_since) < self.min_on_s:
                want = True                     # honour the minimum flash
            else:
                want = False

            if want and not lit:
                self._lit_since = now
                self.flashes += 1
            changed = want != lit
            self.lit = want
            dev = self._device

        if changed and dev is not None:
            try:
                dev.on() if want else dev.off()
            except Exception as exc:
                with self._lock:
                    self._error = f"GPIO write failed ({exc})"
        return want

    # ------------------------------------------------------------------ #
    @property
    def available(self):
        return self._device is not None

    def status(self):
        """One line for the boot log."""
        if self._device is not None:
            return (f"regen brake light: GPIO {self.pin}, on below "
                    f"{self.on_below_w:.0f} W, off above {self.off_above_w:.0f} W, "
                    f"min {self.min_on_s:.1f}s")
        return (f"regen brake light: NOT DRIVEN ({self._error or 'unknown'}) — "
                f"logic still runs, nothing is wired")


# --------------------------------------------------------------------------- #
# Bench check:  python3 SolarRace_OS/modules/regen_light.py
# Runs the thresholds, the minimum-on hold and the stale release against a fake
# clock, so the behaviour can be proven without a car, a bus or a lamp.
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    light = RegenLight().start()
    print(light.status())
    t = 0.0
    ok = True

    def step(power, dt=0.1, expect=None, why=""):
        global t, ok
        t += dt
        got = light.update(power, now=t) if power is not None else light.tick(now=t)
        if expect is not None and got != expect:
            ok = False
        flag = "" if expect is None else ("  OK" if got == expect else "  ** FAIL **")
        print(f"  t={t:5.2f}s  power={'--' if power is None else f'{power:7.0f} W'}"
              f"  lamp={'ON ' if got else 'off'}{flag}   {why}")

    print("\nthresholds and hysteresis:")
    step(0, expect=False, why="coasting")
    step(-30, expect=False, why="inside the dead band: no change")
    step(-80, expect=True, why="below -50 W: light on")
    step(-30, expect=True, why="dead band again: HOLDS on")
    step(-10, expect=True, why="above -20 W, but min-on has not expired")

    print("\nminimum on time:")
    step(None, dt=0.6, expect=False, why="0.6s later: hold expired, light off")

    print("\na brief blip still gives a full flash:")
    step(-900, expect=True, why="hard regen")
    step(0, dt=0.05, expect=True, why="regen gone after 50 ms")
    step(None, dt=0.2, expect=True, why="still lit: minimum flash")
    step(None, dt=0.5, expect=False, why="flash complete")

    print("\nstale bus releases the lamp:")
    step(-900, expect=True, why="regen")
    step(None, dt=3.0, expect=False, why="no power for 3s: released, not stuck on")

    print(f"\nflashes counted: {light.flashes}")
    light.stop()
    print("SELF-CHECK", "PASSED" if ok else "FAILED")
    raise SystemExit(0 if ok else 1)
