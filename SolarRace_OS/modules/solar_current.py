"""
solar_current.py — MPPT→battery charge current, from a Yoctopuce Yocto-Amp
===========================================================================
Measures the DC current the solar array (via the MPPT) is pushing into the
main pack. The sensor is a Yocto-Amp / Yocto-Amp-C: a USB ammeter whose shunt
sits IN SERIES in the charge line, with the measuring side galvanically
isolated from USB (3 kV) so nothing about pack potential reaches the Pi.

    Yocto-Amp-C is the USB-C variant of the same board. Identical electrically
    and identical to the API — only the connector differs, so everything here
    applies to both.

THREE THINGS THIS FILE EXISTS TO GET RIGHT
------------------------------------------
1. THE DEVICE REPORTS MILLIAMPS. `get_currentValue()` returns mA, not amps:
   "The returned value is a number, directly representing the value in mA."
   Publishing it unconverted would put "4200 A" of solar current on the pit
   wall — or, far worse, a plausible-looking 4.2 that is actually 4.2 mA. Every
   value leaves this module already divided by MILLIAMPS_PER_AMP, in amps, and
   nothing downstream needs to know the wire unit. This is the same class of
   bug as the 0.1 km/h speed field in mms_parser, which put 2,569 rows over
   200 km/h in telemetry.db before anyone noticed.

2. THE YOCTO-AMP HAS TWO CURRENT CHANNELS AND ONLY ONE IS THE RIGHT ONE.
   `current1` is the DC component; `current2` is the AC component (RMS). A
   solar charge line is DC, so this reads current1 and nothing else. Taking
   whatever YCurrent.FirstCurrent() happens to return is a coin flip between
   them, and on a DC line the AC channel reads a near-zero ripple figure that
   looks exactly like "the array is producing almost nothing". The channel is
   selected by function id AND confirmed against the unit string the device
   reports ("mA DC" vs "mA AC"), so a firmware that ever renumbered them would
   fail loudly here instead of quietly charting ripple.

3. IT MUST SURVIVE THE CABLE FALLING OUT. This is a race car: USB connectors
   vibrate loose. Every Yoctopuce call happens on ONE background daemon thread
   that reconnects on its own with a backoff; the telemetry loop only ever
   copies a snapshot. Nothing here raises at the call site, exactly like
   GPSReader — losing the solar sensor must not take the car's telemetry down.

THREADING — WHY ALL YOCTOPUCE CALLS ARE CONFINED TO ONE THREAD
The Yoctopuce API keeps global state (the registered hub, the device list) and
is not safe to drive from several threads at once. So the rule here is simple
and absolute: the reader thread is the only thing that ever touches YAPI. The
CAN loop calls get_reading(), which takes a lock, copies a small dict and
returns immediately. No YAPI call ever happens on the telemetry thread.

INSTALL (see also SolarRace_OS/requirements.txt)
    pip install yoctopuce

    # And the udev rule, or the HUD cannot open the device as a normal user:
    echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="24e0", MODE="0666"' \
        | sudo tee /etc/udev/rules.d/51-yoctopuce.rules
    sudo udevadm control --reload-rules && sudo udevadm trigger

    Without that rule libusb can only open the device as root, and the HUD
    deliberately does NOT run as root (see deploy/can-up.service for why). The
    symptom is status "no_hub" with an access/permission error.

Typical use:

    solar = SolarCurrentReader()
    solar.start()
    ...
    reading = solar.get_reading()      # dict, always; never raises
    amps = reading["solar_current_A"]  # float, or None if unknown
"""

import sys
import threading
import time


def safe_print(msg):
    """print() that can never raise, whatever stdout's encoding is.

    NOT a nicety. The first version of this file called bare print() with a
    "☀" in the string from inside _find_dc_channel's try block. On a
    console or log file opened in a non-UTF-8 locale that raises
    UnicodeEncodeError, the except caught it, and a perfectly healthy sensor was
    discarded as "error" — the feature broken by its own log line.

    config.py carries the same helper for the same reason, after the same bug
    left a CAN bus open at shutdown. Diagnostics must never be able to break the
    thing they are reporting on.
    """
    try:
        print(msg)
    except Exception:
        try:
            enc = getattr(sys.stdout, "encoding", None) or "ascii"
            print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))
        except Exception:
            pass          # a log line is never worth raising over

# The library is imported defensively so this module can be imported on a
# laptop that has never heard of Yoctopuce. A missing sensor library must not
# stop the HUD from starting — it just means the solar reading is unavailable,
# reported as status "no_library" and rendered as an em dash like any other
# absent value. Same reasoning as the pit dashboard's optional Plotly import.
try:
    from yoctopuce.yocto_api import YAPI, YRefParam
    from yoctopuce.yocto_current import YCurrent
    YOCTO_AVAILABLE = True
    YOCTO_IMPORT_ERROR = None
except Exception as _exc:            # ImportError, or a broken install
    YAPI = None
    YRefParam = None
    YCurrent = None
    YOCTO_AVAILABLE = False
    YOCTO_IMPORT_ERROR = str(_exc)


# ── Unit conversion ─────────────────────────────────────────────────────── #
# The one number that matters most in this file. See point 1 in the docstring.
MILLIAMPS_PER_AMP = 1000.0

# ── Channel selection ───────────────────────────────────────────────────── #
# current1 = DC, current2 = AC (RMS). We want DC.
DC_FUNCTION_SUFFIX = "current1"
# What the device says its unit is on the DC channel. Checked, not assumed —
# see point 2. Compared case-insensitively and by substring, because the exact
# string has varied ("mA DC" / "mA") across firmware revisions and a unit check
# that is too strict would reject a perfectly good sensor.
DC_UNIT_MARKER = "DC"

# ── Device rating ───────────────────────────────────────────────────────── #
# Datasheet limits for the Yocto-Amp / Yocto-Amp-C. Not enforced here — the
# hardware measures what it measures — but published with every reading so the
# dashboards can say "this is past what the sensor is rated for" rather than
# charting a number nobody should trust. 10 A continuous, 17 A peak, and at a
# sustained 10 A the PCB runs ~15 °C above ambient (Yoctopuce recommend a fan).
SENSOR_MAX_CONTINUOUS_A = 10.0
SENSOR_MAX_PEAK_A = 17.0

# A reading beyond the peak rating is not a measurement, it is a saturated or
# faulty sensor. Published as None rather than as a number, for the same reason
# an out-of-range PT1000 resistance does not become a temperature.
IMPLAUSIBLE_ABOVE_A = SENSOR_MAX_PEAK_A * 1.2      # 20.4 A

# ── Cadence ─────────────────────────────────────────────────────────────── #
# The device refreshes at 10 Hz. Polling at 4 Hz is well inside that and is
# already far faster than the 0.5 s Firebase push, so a faster loop would cost
# USB traffic and deliver nothing.
POLL_INTERVAL_S = 0.25

# How often to re-scan the USB bus while the sensor is missing. UpdateDeviceList
# is what makes a re-plugged device reappear, so this interval IS the
# reconnection time after the cable is pushed back in.
RESCAN_INTERVAL_S = 2.0

# Reconnect backoff after a hub registration failure (no library, no
# permission, no device). Grows to a ceiling so a car running without the
# sensor fitted does not spin on it for 24 hours.
_RETRY_MIN_S = 2.0
_RETRY_MAX_S = 30.0

# A reading older than this is stale — the thread has stopped producing, which
# means the sensor went away. Generous next to POLL_INTERVAL_S so a single
# missed poll is not reported as a fault.
READING_STALE_AFTER_S = 3.0

# ── Status vocabulary ───────────────────────────────────────────────────── #
# Why there is no reading, in a word. Surfaced all the way to the pit wall,
# because "the solar sensor says nothing" has several very different causes and
# the crew should not have to guess which one they are looking at.
STATUS_NO_LIBRARY = "no_library"   # pip install yoctopuce was never run
STATUS_NO_HUB = "no_hub"           # RegisterHub failed — usually the udev rule
STATUS_SEARCHING = "searching"     # hub is up, no Yocto-Amp found yet
STATUS_OFFLINE = "offline"         # it was there and now it is not (cable!)
STATUS_ONLINE = "online"           # reading normally
STATUS_IMPLAUSIBLE = "implausible"  # past the sensor's rating; not a reading
STATUS_ERROR = "error"             # the API raised; detail in last_error


class SolarCurrentReader:
    """Non-blocking view of the newest solar charge current, in AMPS.

    Safe to construct and start with no sensor attached, no udev rule, and no
    yoctopuce package installed. In every one of those cases get_reading()
    simply returns a dict whose current is None and whose status says why.
    """

    def __init__(self, target_serial=None, poll_interval_s=POLL_INTERVAL_S):
        """`target_serial` pins a specific module (e.g. "AMPMK01-123456").

        Left as None it takes the first Yocto-Amp on the bus, which is the
        right default while there is exactly one. Pin it the moment a second
        Yoctopuce sensor joins the car, or "the first one" becomes a race
        between two USB enumerations and the solar trace starts reading
        whatever the other sensor measures.
        """
        self.target_serial = target_serial
        self.poll_interval_s = poll_interval_s

        self._lock = threading.Lock()
        self._thread = None
        self._running = False

        # --- everything below is guarded by self._lock -------------------- #
        self._amps = None            # newest good reading, amps. None, not 0.0
        self._reading_time = 0.0     # time.time() when _amps was stored
        self._status = (STATUS_NO_LIBRARY if not YOCTO_AVAILABLE
                        else STATUS_SEARCHING)
        self._serial = None          # serial of the module actually in use
        self._unit = None            # the unit string the device reports
        self._last_error = YOCTO_IMPORT_ERROR
        self._sample_count = 0       # proves the sensor is really producing

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #
    def start(self):
        """Begin polling in the background. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return self
        if not YOCTO_AVAILABLE:
            # Nothing to poll. Deliberately NOT an exception: a laptop without
            # the library must still be able to run main.py.
            safe_print("☀️ Solar sensor: yoctopuce library not installed "
                  f"({YOCTO_IMPORT_ERROR}) — solar current will read '—'. "
                  "Install it with: pip install yoctopuce")
            return self
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="solar-current", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        """Stop the thread and release the USB API. Safe to call twice."""
        self._running = False
        thread = self._thread
        if thread is not None and thread.is_alive():
            # Bounded join: this runs on the shutdown path and must not be able
            # to hang the process if a USB call is wedged in the driver.
            thread.join(timeout=2.0)
        self._thread = None

    # ------------------------------------------------------------------ #
    # The only public read                                                #
    # ------------------------------------------------------------------ #
    def get_reading(self):
        """Snapshot of the newest reading. Returns immediately, never raises.

        Always a dict, so callers need no None-check before indexing:

            solar_current_A       amps, or None when there is no valid reading
            solar_sensor_status   STATUS_* — why, when the current is None
            solar_sensor_serial   which module produced it, or None
            solar_current_stale   True when the newest reading has aged out

        The current is None — NEVER 0.0 — whenever we do not have a reading.
        Nothing about this car makes 0 A and "no sensor" the same fact: at
        night the array genuinely produces 0 A, and that is a real measurement
        the strategy team will read differently from a dead sensor.
        """
        with self._lock:
            amps = self._amps
            age = time.time() - self._reading_time if self._reading_time else None
            stale = age is not None and age > READING_STALE_AFTER_S
            status = self._status
            serial = self._serial
            count = self._sample_count
            error = self._last_error

        # A stale reading is not a current reading. Report the last value as
        # stale rather than serving it as live: a frozen number that looks live
        # is how a disconnected sensor gets mistaken for a becalmed array.
        if stale and status == STATUS_ONLINE:
            status = STATUS_OFFLINE

        return {
            "solar_current_A": None if (amps is None or stale) else amps,
            "solar_sensor_status": status,
            "solar_sensor_serial": serial,
            "solar_current_stale": bool(stale),
            "solar_sample_count": count,
            "solar_last_error": error,
        }

    def is_online(self):
        """True when a real reading arrived recently. For status displays."""
        return self.get_reading()["solar_sensor_status"] == STATUS_ONLINE

    # ------------------------------------------------------------------ #
    # Background thread — the ONLY place that touches YAPI                #
    # ------------------------------------------------------------------ #
    def _set(self, **fields):
        """Update the shared snapshot under the lock."""
        with self._lock:
            for key, value in fields.items():
                setattr(self, "_" + key, value)

    def _run(self):
        """Register the USB hub, then poll the DC channel until stopped."""
        retry_s = _RETRY_MIN_S

        while self._running:
            if not self._register_hub():
                # No hub: no library, no permission, or libusb unavailable.
                # Back off and try again — the udev rule may be installed, or
                # the user added to the group, while the car is running.
                self._sleep(retry_s)
                retry_s = min(_RETRY_MAX_S, retry_s * 2)
                continue
            retry_s = _RETRY_MIN_S

            try:
                self._poll_loop()
            except Exception as exc:
                # Any escape from the poll loop is a bug or a driver-level
                # failure. Report it, drop the hub, and rebuild from scratch —
                # a fresh RegisterHub is the one thing that reliably recovers a
                # wedged USB layer.
                self._set(status=STATUS_ERROR, last_error=f"{type(exc).__name__}: {exc}")
            finally:
                self._free_api()

            if self._running:
                self._sleep(_RETRY_MIN_S)

        self._free_api()

    def _register_hub(self):
        """YAPI.RegisterHub("usb"). True on success.

        RegisterHub is what claims the local USB bus for this process. It fails
        when the yoctopuce library cannot reach libusb, when another process
        already holds the devices (a running VirtualHub, or a second copy of
        this app), or — the usual one on a fresh Pi — when the udev rule is
        missing and the device nodes are root-only.
        """
        errmsg = YRefParam()
        try:
            if YAPI.RegisterHub("usb", errmsg) != YAPI.SUCCESS:
                self._set(status=STATUS_NO_HUB, last_error=str(errmsg.value))
                return False
        except Exception as exc:
            self._set(status=STATUS_NO_HUB,
                      last_error=f"{type(exc).__name__}: {exc}")
            return False
        self._set(status=STATUS_SEARCHING, last_error=None)
        return True

    def _free_api(self):
        """Release the USB API. Never raises; called on every exit path."""
        try:
            if YOCTO_AVAILABLE and YAPI is not None:
                YAPI.FreeAPI()
        except Exception:
            pass          # shutting down regardless

    def _poll_loop(self):
        """Find the DC channel, then read it until it goes away or we stop."""
        sensor = None
        last_scan = 0.0

        while self._running:
            now = time.time()

            # (Re)scan when we have no sensor, or periodically to notice a
            # module that was unplugged and plugged back in. UpdateDeviceList
            # is what makes a re-enumerated device visible again.
            if sensor is None or not self._is_online(sensor):
                if now - last_scan >= RESCAN_INTERVAL_S:
                    last_scan = now
                    if sensor is not None:
                        # We had it and lost it. Almost always the cable.
                        self._set(status=STATUS_OFFLINE)
                        sensor = None
                    sensor = self._find_dc_channel()
                if sensor is None:
                    self._sleep(self.poll_interval_s)
                    continue

            self._read_once(sensor)
            self._sleep(self.poll_interval_s)

    def _is_online(self, sensor):
        """sensor.isOnline() that cannot raise — a yanked cable can throw."""
        try:
            return bool(sensor.isOnline())
        except Exception:
            return False

    def _find_dc_channel(self):
        """Locate the Yocto-Amp's DC channel (current1). None if not present.

        Selected by function id, then CONFIRMED against the unit the device
        reports, so this can never silently end up on the AC channel. See
        point 2 of the module docstring for why that matters on a DC line.
        """
        errmsg = YRefParam()
        try:
            YAPI.UpdateDeviceList(errmsg)
        except Exception as exc:
            self._set(status=STATUS_ERROR,
                      last_error=f"UpdateDeviceList: {exc}")
            return None

        try:
            if self.target_serial:
                # Pinned to one module: ask for its DC function by name.
                candidates = [YCurrent.FindCurrent(
                    f"{self.target_serial}.{DC_FUNCTION_SUFFIX}")]
            else:
                # Walk every Current function on the bus and keep the DC one.
                candidates = []
                sensor = YCurrent.FirstCurrent()
                while sensor is not None:
                    candidates.append(sensor)
                    sensor = sensor.nextCurrent()

            for sensor in candidates:
                if not self._is_online(sensor):
                    continue
                function_id = sensor.get_functionId()
                if function_id != DC_FUNCTION_SUFFIX:
                    continue          # this is current2, the AC channel
                unit = sensor.get_unit()
                if unit and DC_UNIT_MARKER not in unit.upper():
                    # Right function id, wrong unit. Refuse it and say so
                    # rather than charting AC ripple as solar production.
                    self._set(status=STATUS_ERROR,
                              last_error=f"{function_id} reports unit "
                                         f"{unit!r}, expected a DC channel")
                    continue
                serial = sensor.get_module().get_serialNumber()
                self._set(status=STATUS_SEARCHING, serial=serial, unit=unit,
                          last_error=None)
                safe_print(f"☀️ Solar sensor online: {serial}.{function_id} "
                      f"(unit {unit!r}, rated {SENSOR_MAX_CONTINUOUS_A:.0f} A "
                      f"continuous)")
                return sensor
        except Exception as exc:
            self._set(status=STATUS_ERROR,
                      last_error=f"{type(exc).__name__}: {exc}")
            return None

        self._set(status=STATUS_SEARCHING)
        return None

    def _read_once(self, sensor):
        """One reading, converted to amps and stored. Never raises."""
        try:
            milliamps = sensor.get_currentValue()
        except Exception as exc:
            # A cable pulled mid-call lands here. Not fatal: the loop will
            # notice isOnline() is False and go back to scanning.
            self._set(status=STATUS_OFFLINE,
                      last_error=f"{type(exc).__name__}: {exc}")
            return

        # The library's "no value" sentinel. Treated as no reading, not as 0 A.
        if milliamps is None or milliamps == YAPI.INVALID_DOUBLE:
            self._set(status=STATUS_OFFLINE)
            return

        amps = float(milliamps) / MILLIAMPS_PER_AMP     # mA -> A. See point 1.

        if abs(amps) > IMPLAUSIBLE_ABOVE_A:
            # Beyond anything this sensor can legitimately report. Publish
            # nothing rather than a number: past the peak rating the reading is
            # meaningless, and charting it would hide a hardware problem behind
            # a plausible-looking trace.
            self._set(status=STATUS_IMPLAUSIBLE,
                      last_error=f"{amps:.1f} A is past the sensor's "
                                 f"{SENSOR_MAX_PEAK_A:.0f} A peak rating")
            return

        with self._lock:
            self._amps = round(amps, 3)
            self._reading_time = time.time()
            self._status = STATUS_ONLINE
            self._sample_count += 1
            self._last_error = None

    def _sleep(self, seconds):
        """Sleep in slices so stop() stays responsive."""
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(0.05, max(0.0, end - time.time())))


# ---------------------------------------------------------------------------
# Standalone check — run this on the Pi to prove the sensor works before
# blaming the dashboard:
#
#     python SolarRace_OS/modules/solar_current.py
#
# Expect a serial number and a current in AMPS once per second. If it prints
# status "no_hub", the udev rule is missing (see the header). If it prints
# "searching" forever, the Yocto-Amp is not enumerating — try `lsusb | grep
# 24e0`, which must list the device.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    reader = SolarCurrentReader().start()
    try:
        while True:
            r = reader.get_reading()
            amps = r["solar_current_A"]
            shown = "—" if amps is None else f"{amps:+.3f} A"
            print(f"solar: {shown:>12}   status={r['solar_sensor_status']:<12} "
                  f"serial={r['solar_sensor_serial'] or '—'}  "
                  f"samples={r['solar_sample_count']}"
                  + (f"  err={r['solar_last_error']}"
                     if r["solar_last_error"] else ""))
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
