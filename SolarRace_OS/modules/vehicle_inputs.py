"""
vehicle_inputs.py — parking brake and lights, read from the Pi's own GPIO
=========================================================================
These two indicators cannot come from the motor controller. A Silixcon ESC has
no knowledge of a parking brake or of headlights — they are separate circuits —
and the one byte that looked like it might carry them (byte 3 of the 0x600
status frame) is documented as a PROTECTIONS word, not vehicle state. Reading
them from there produced a confident "brake released / lights off" that was
really just "no protection has tripped".

So the switches are wired into the Raspberry Pi directly and read here.

⚠️ ELECTRICAL — READ BEFORE WIRING
Raspberry Pi GPIO pins are 3.3 V ONLY and are not 5 V tolerant, let alone 12 V.
Connecting a light circuit straight to a GPIO pin will destroy the Pi. Two safe
patterns:

  DRY CONTACT (preferred, use this for the parking-brake microswitch)
      GPIO pin ──────────┐
                         │
                       [switch]
                         │
      GND   ─────────────┘
  Nothing but the switch between the pin and ground. Enable the Pi's internal
  pull-up, so the pin idles HIGH and reads LOW when the switch closes. No
  external voltage touches the Pi at all. This is why ACTIVE_LOW below defaults
  to True.

  LIVE CIRCUIT (for sensing whether the 12 V lights are actually energised)
  Do NOT use a bare resistor divider — a wiring fault would put 12 V on the pin.
  Use an optocoupler (e.g. PC817) with a ~1 kΩ series resistor on the LED side:
      +12V (lights) ──[1k]──▶|── opto LED ── lights GND
      GPIO pin ── opto collector,  emitter ── Pi GND, internal pull-up on
  This keeps the two grounds galvanically isolated, which also protects the Pi
  from motor-circuit noise.

PIN CHOICE
Set the two pins below to whatever you wire. Avoid anything the CAN HAT uses:
SPI0 (GPIO 7, 8, 9, 10, 11) and its interrupt lines (commonly GPIO 24 / 25),
plus I2C (GPIO 2, 3 — they have fixed 1.8 kΩ pull-ups) and GPIO 0/1 (HAT ID
EEPROM). Comfortably free and physically convenient on the header:

      GPIO 5  (pin 29)      GPIO 6  (pin 31)
      GPIO 16 (pin 36)      GPIO 20 (pin 38)      GPIO 21 (pin 40)
      GPIO 26 (pin 37)

The defaults are GPIO 5 and GPIO 6 — adjacent to each other, next to a ground
pin (pin 30 or 34), which makes for a tidy two-switch loom. Change them freely.

UNTIL IT IS WIRED
Both pins default to None, which means "no source". read() then reports None
for that input — NOT False — and the driver HUD shows it as UNKNOWN rather than
as a confident "off". A driver must never be told the parking brake is released
when nobody actually knows.
"""

import threading

# --------------------------------------------------------------------------- #
# Configuration — set these once the loom is in.
# BCM numbering (GPIO n), not physical header position.
# --------------------------------------------------------------------------- #
PARKING_BRAKE_PIN = None      # e.g. 5  -> header pin 29
LIGHTS_PIN = None             # e.g. 6  -> header pin 31

# True  = switch shorts the pin to GND, internal pull-up on (the wiring above).
# False = switch drives the pin high (needs an external pull-DOWN).
ACTIVE_LOW = True

# Contact bounce on a mechanical switch is milliseconds; this is generous and
# stops a marginal contact from flickering the indicator on the HUD.
DEBOUNCE_S = 0.05


class VehicleInputs:
    """Reads the brake/lights switches, or reports "unknown" when it cannot.

    Never raises at the call site. A missing gpiozero (running on a laptop), a
    pin that is already claimed, or an unconfigured pin all degrade to None —
    an honest "we don't know" — rather than to a plausible-looking False.
    """

    def __init__(self, parking_brake_pin=None, lights_pin=None,
                 active_low=None, debounce_s=DEBOUNCE_S):
        self.parking_brake_pin = (PARKING_BRAKE_PIN if parking_brake_pin is None
                                  else parking_brake_pin)
        self.lights_pin = LIGHTS_PIN if lights_pin is None else lights_pin
        self.active_low = ACTIVE_LOW if active_low is None else active_low
        self.debounce_s = debounce_s

        self._lock = threading.Lock()
        self._devices = {}          # name -> gpiozero.Button
        self._errors = {}           # name -> why it is unavailable
        self.backend = None         # "gpiozero" once anything opened

    # ------------------------------------------------------------------ #
    def start(self):
        """Claim the configured pins. Safe to call anywhere; never raises."""
        configured = {"parking_brake": self.parking_brake_pin,
                      "lights_on": self.lights_pin}
        if not any(p is not None for p in configured.values()):
            self._errors["all"] = "no pins configured yet"
            return self

        try:
            from gpiozero import Button
        except Exception as exc:
            # Not on a Pi, or python3-gpiozero not installed. Everything stays
            # unknown; the rest of the telemetry system is unaffected.
            self._errors["all"] = f"gpiozero unavailable ({exc})"
            return self

        for name, pin in configured.items():
            if pin is None:
                self._errors[name] = "pin not configured"
                continue
            try:
                # pull_up=True matches the switch-to-ground wiring in the module
                # docstring: the pin idles HIGH and is pulled LOW when closed,
                # so `is_pressed` is True when the switch is closed.
                self._devices[name] = Button(
                    pin, pull_up=self.active_low, bounce_time=self.debounce_s)
                self.backend = "gpiozero"
            except Exception as exc:
                self._errors[name] = f"GPIO {pin} unavailable ({exc})"
        return self

    def stop(self):
        with self._lock:
            for dev in self._devices.values():
                try:
                    dev.close()
                except Exception:
                    pass
            self._devices.clear()

    # ------------------------------------------------------------------ #
    def read(self):
        """{"parking_brake": True|False|None, "lights_on": True|False|None}.

        None means "no source" and the HUD renders it as UNKNOWN. That
        distinction is the whole point of this module.
        """
        out = {}
        for name in ("parking_brake", "lights_on"):
            dev = self._devices.get(name)
            if dev is None:
                out[name] = None
                continue
            try:
                out[name] = bool(dev.is_pressed)
            except Exception:
                out[name] = None            # pin died mid-race: say so
        return out

    @property
    def available(self):
        return bool(self._devices)

    def status(self):
        """One-line summary for the boot log."""
        if self._devices:
            pins = ", ".join(f"{n}=GPIO{d.pin.number}"
                             for n, d in self._devices.items())
            return f"vehicle inputs: {pins}"
        why = self._errors.get("all") or "; ".join(
            f"{k}: {v}" for k, v in self._errors.items()) or "none configured"
        return f"vehicle inputs: UNKNOWN ({why})"


# --------------------------------------------------------------------------- #
# Bench check:  python3 SolarRace_OS/modules/vehicle_inputs.py
# Set the pins above first, then flick the switches and watch the values.
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import time

    inputs = VehicleInputs().start()
    print(inputs.status())
    if not inputs.available:
        print("\nNo pins are configured (or this is not a Pi).")
        print("Set PARKING_BRAKE_PIN / LIGHTS_PIN at the top of this file to")
        print("the BCM numbers you wired, e.g. 5 and 6, then run this again.")
    print("\nCtrl+C to stop.\n")
    try:
        while True:
            vals = inputs.read()
            print("  " + "   ".join(
                f"{k}: {'ON ' if v else ('off' if v is False else '  ?')}"
                for k, v in vals.items()), end="\r")
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        inputs.stop()
        print()
