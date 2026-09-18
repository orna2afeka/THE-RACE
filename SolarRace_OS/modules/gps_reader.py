"""
gps_reader.py — live position for the car, read from gpsd
=========================================================
The Pi already runs gpsd (configured in /etc/default/gpsd), which owns the
GPS serial port and republishes fixes as JSON on a local TCP socket. This
reader is a gpsd *client*: exactly the same data stream you see with

    gpspipe -w

WHY NOT READ THE SERIAL PORT DIRECTLY (the old implementation)?
Because gpsd already holds it. With GPSD_OPTIONS="-n" gpsd opens the device at
boot and keeps it open, so a second reader on /dev/ttyUSBx fights gpsd for the
same bytes and both sides get shredded NMEA. One owner (gpsd), many clients.

WHY A RAW SOCKET INSTEAD OF `import gps`?
The `gps` module is not a pip package — it ships with gpsd as the distro
package python3-gps, so it exists on the Pi but not on a laptop, and its API
has drifted between gpsd releases. The wire protocol below is stable, needs
nothing but the standard library, and is what gpspipe speaks. Same data, one
code path, testable off the Pi (see GPSD_HOST).

THREADING
gpsd streams: a read blocks until the next report arrives. The CAN loop must
never block, so reads happen on a background daemon thread that keeps a
snapshot of the newest fix. `get_coordinates()` just copies that snapshot and
returns immediately.

Typical use:

    gps = GPSReader()
    gps.start()
    ...
    fix = gps.get_coordinates()   # {"lat": .., "lon": .., ...} or None
"""

import json
import math
import os
import re
import socket
import threading
import time

# gpsd's control/streaming socket. Host is configurable so you can point a
# laptop at the Pi for testing: GPSReader(host="raspberrypi.local").
# NOTE: gpsd listens on localhost only unless started with -G.
GPSD_HOST = "127.0.0.1"
GPSD_PORT = 2947

# Ask gpsd to stream JSON reports — the same request gpspipe -w makes.
_WATCH_COMMAND = b'?WATCH={"enable":true,"json":true}\n'

# Ask gpsd which receivers it actually holds. This is what separates the two
# ways of having no position, which look identical from the fix alone:
#   • gpsd has NO device      → nothing will ever arrive; a human must plug the
#                               receiver in or fix DEVICES= in /etc/default/gpsd
#   • gpsd has a device       → the receiver is there and still searching
# Reporting both as "no fix yet" sent people looking for sky view when the
# receiver was not connected at all.
_DEVICES_COMMAND = b'?DEVICES;\n'

# How long a /dev scan stays fresh. hardware() is called from the telemetry
# loop's change detection, and walking /dev at loop rate to watch for a USB
# plug event would be pure waste — enumeration takes a second or two anyway.
_HARDWARE_SCAN_S = 5.0

# How often to re-ask for the device list while gpsd is silent. gpsd announces
# hot-plugged receivers with a DEVICE report, but only when udev tells it; the
# poll is what notices a receiver plugged in on a Pi where that doesn't fire.
_DEVICES_POLL_S = 10.0

# How long a fix stays "current". gpsd emits TPV about once a second, so a fix
# older than this means the receiver lost lock (tunnel, garage, antenna
# unplugged). We keep serving the last known position — a frozen dot is more
# useful to the pit than an empty map — but mark it stale so it can be shown
# as such rather than mistaken for a live position.
FIX_STALE_AFTER_S = 5.0

# Reconnect backoff when gpsd isn't reachable (not started yet at boot, or
# restarted mid-race). Grows to a ceiling so a long outage doesn't spin.
_RECONNECT_MIN_S = 1.0
_RECONNECT_MAX_S = 15.0

# recv() timeout. Longer than gpsd's report interval but short enough that
# stop() stays responsive. A timeout is NOT an error: gpsd goes quiet whenever
# no device is attached, and the connection is still perfectly good.
_SOCKET_TIMEOUT_S = 5.0

# Guard against a peer that never sends a newline — don't buffer forever.
_MAX_BUFFER_BYTES = 65536


def _finite(value):
    """Return value as a float, or None if it's absent/non-numeric/NaN.

    gpsd omits fields it doesn't have, and sends NaN for some unknowns, so
    every numeric field has to survive both.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


class GPSReader:
    """Non-blocking view of the newest gpsd fix.

    Safe to construct and start even when gpsd is down or there is no GPS at
    all: the thread keeps retrying in the background and get_coordinates()
    simply returns None until a real fix arrives. Nothing here ever raises at
    the call site, because losing GPS must not take the telemetry down.
    """

    def __init__(self, host=GPSD_HOST, port=GPSD_PORT):
        self.host = host
        self.port = port

        self._lock = threading.Lock()
        self._thread = None
        self._running = False
        self._sock = None

        # --- everything below is guarded by self._lock --------------------- #
        self._fix = None          # newest usable fix, or None if never seen one
        self._fix_time = 0.0      # time.monotonic() when _fix was stored
        self._connected = False
        self._sats_used = None    # from SKY reports; nice for diagnostics
        self._last_error = None
        self._tpv_count = 0       # TPV reports seen (proves gpsd is talking)
        # gpsd's own device list: None = not asked/answered yet, [] = gpsd is
        # running but holds no receiver, [path, ...] = receivers it has open.
        self._devices = None
        # Full DEVICE entries behind the paths above (driver, bps, activated),
        # for the hardware() debug line.
        self._device_info = []
        self._last_devices_poll = 0.0
        # /dev scan cache — see _scan_serial_hardware().
        self._hw_scan_time = 0.0
        self._hw_ports = []
        self._hw_configured = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #
    def start(self):
        """Begin streaming in the background. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="gpsd-reader", daemon=True
        )
        self._thread.start()
        return self

    def stop(self):
        """Stop streaming and drop the connection. Safe to call twice."""
        self._running = False
        # Shut the socket down so a blocked recv() returns at once instead of
        # waiting out the timeout.
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # already closed / never connected
            try:
                sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ------------------------------------------------------------------ #
    # Public read API                                                     #
    # ------------------------------------------------------------------ #
    def get_coordinates(self):
        """Newest known position, or None if we have never had a fix.

        Never blocks and never raises. Returns a fresh dict:

            lat, lon      degrees (always present)
            alt_m         metres above sea level, when reported
            speed_kmh     ground speed from GPS, when reported
            track_deg     course over ground, when reported
            fix_mode      2 = 2D fix, 3 = 3D fix
            sats_used     satellites in the solution, when known
            fix_age_s     seconds since this fix arrived
            stale         True once fix_age_s exceeds FIX_STALE_AFTER_S
        """
        with self._lock:
            if self._fix is None:
                return None
            out = dict(self._fix)
            age = time.monotonic() - self._fix_time
            sats = self._sats_used
        out["fix_age_s"] = round(age, 1)
        out["stale"] = age > FIX_STALE_AFTER_S
        if sats is not None:
            out["sats_used"] = sats
        return out

    def status(self):
        """One-line human summary — for console logging and the HUD."""
        with self._lock:
            connected = self._connected
            has_fix = self._fix is not None
            tpv = self._tpv_count
            err = self._last_error
            age = time.monotonic() - self._fix_time if has_fix else None
            sats = self._sats_used
            devices = self._devices

        if not connected:
            if err is None:
                # First attempt hasn't finished yet — the worker prints this
                # line immediately after start(). Saying "unreachable" before
                # we have even tried once accused a gpsd that was fine.
                return f"GPS: connecting to gpsd at {self.host}:{self.port}..."
            return f"GPS: gpsd unreachable at {self.host}:{self.port} ({err})"
        if not has_fix:
            extra = f", {sats} sats" if sats is not None else ""
            if devices == []:
                # gpsd is up and answering, but owns no receiver: nothing will
                # ever arrive until someone plugs one in (or fixes DEVICES= in
                # /etc/default/gpsd). NOT "searching for satellites".
                return ("GPS: no receiver — gpsd has no device "
                        "(plug the GPS in, or check DEVICES= in "
                        "/etc/default/gpsd)")
            if devices and tpv == 0:
                return (f"GPS: {devices[0]} attached, no data yet "
                        f"(receiver silent{extra})")
            if tpv:
                # Reports ARE arriving — the receiver is alive and hunting.
                # Deliberately WITHOUT the report count: this string drives
                # change-only logging, and a counter that ticks every second
                # would print a line every second for the whole cold start.
                # Satellite count carries the same "is it working" signal and
                # only moves when something actually changes.
                sat_txt = (f"{sats} sats visible" if sats
                           else "no satellites yet")
                return f"GPS: searching for satellites ({sat_txt})"
            return (f"GPS: connected to gpsd, no fix yet "
                    f"({tpv} reports{extra})")
        sat_txt = f", {sats} sats" if sats is not None else ""
        if age > FIX_STALE_AFTER_S:
            return f"GPS: fix STALE ({age:.0f}s old{sat_txt})"
        return f"GPS: fix OK ({age:.1f}s old{sat_txt})"

    def log_line(self):
        """Coarse state, for the console's change-only logging.

        status() carries the live numbers — fix age, report count, satellites —
        which is right for the pit's health payload and wrong for a log: every
        one of them changes on every pass, so logging status() on change printed
        a line twice a second for the whole race. This says only WHICH state we
        are in, so one line marks each transition and nothing is printed in
        between. The numbers stay one status() call away.
        """
        with self._lock:
            connected = self._connected
            err = self._last_error
            devices = list(self._device_info or [])
            known = self._devices
            has_fix = self._fix is not None
            fresh = has_fix and (time.monotonic() - self._fix_time) <= FIX_STALE_AFTER_S
            tpv = self._tpv_count

        if not connected:
            if err is None:
                return f"GPS: connecting to gpsd at {self.host}:{self.port}..."
            return f"GPS: gpsd unreachable at {self.host}:{self.port} ({err})"
        if has_fix:
            return ("GPS: fix OK — position live" if fresh else
                    "GPS: fix STALE — receiver lost lock")
        if known == []:
            return ("GPS: no receiver — gpsd has no device "
                    "(plug the GPS in, or check DEVICES= in /etc/default/gpsd)")
        if devices and tpv == 0:
            return f"GPS: {devices[0].get('path')} attached, receiver silent"
        if tpv:
            return "GPS: searching for satellites"
        return "GPS: connected to gpsd, no fix yet"

    @property
    def has_fix(self):
        """True when we hold a position that is current (not stale)."""
        with self._lock:
            if self._fix is None:
                return False
            return (time.monotonic() - self._fix_time) <= FIX_STALE_AFTER_S

    # ------------------------------------------------------------------ #
    # Background thread                                                   #
    # ------------------------------------------------------------------ #
    def _run(self):
        """Connect → stream → on any failure, back off and reconnect."""
        backoff = _RECONNECT_MIN_S
        while self._running:
            try:
                self._connect()
                backoff = _RECONNECT_MIN_S   # a good connection resets it
                self._stream()               # returns when gpsd closes/stops
            except (OSError, socket.timeout) as exc:
                # Covers refused/reset/unreachable/DNS — gpsd not up yet, or
                # restarted. Record it and try again; never propagate.
                with self._lock:
                    self._connected = False
                    self._last_error = f"{type(exc).__name__}: {exc}"
            finally:
                self._close_socket()

            if not self._running:
                break
            self._sleep_interruptibly(backoff)
            backoff = min(backoff * 2, _RECONNECT_MAX_S)

    def _connect(self):
        """Open the gpsd socket and subscribe to the JSON stream."""
        sock = socket.create_connection((self.host, self.port), timeout=5.0)
        sock.settimeout(_SOCKET_TIMEOUT_S)
        sock.sendall(_WATCH_COMMAND + _DEVICES_COMMAND)
        self._sock = sock
        with self._lock:
            self._connected = True
            self._last_error = None
            self._devices = None        # this connection hasn't been told yet
            self._device_info = []
        self._last_devices_poll = time.time()

    def _stream(self):
        """Read newline-delimited JSON until the socket dies or we're stopped."""
        buf = b""
        while self._running:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                # gpsd has nothing to say (commonly: no device attached).
                # The connection is fine — keep waiting, but use the lull to
                # re-ask which receivers it holds, so a GPS plugged in mid-race
                # is noticed even when no DEVICE announcement arrives.
                self._poll_devices()
                continue
            if not chunk:
                return  # gpsd closed the connection → caller reconnects

            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._handle_line(line)

            if len(buf) > _MAX_BUFFER_BYTES:
                buf = b""  # never seen in practice; don't grow without bound

    def _handle_line(self, line):
        """Decode one gpsd JSON report. Malformed lines are skipped."""
        line = line.strip()
        if not line:
            return
        try:
            report = json.loads(line.decode("utf-8", errors="replace"))
        except (ValueError, AttributeError):
            return  # partial or non-JSON line — ignore it
        if not isinstance(report, dict):
            return

        kind = report.get("class")
        if kind == "DEVICES":
            devs = report.get("devices")
            paths = [d.get("path") for d in devs
                     if isinstance(d, dict) and d.get("path")] \
                if isinstance(devs, list) else []
            with self._lock:
                self._devices = paths
                self._device_info = [d for d in devs
                                     if isinstance(d, dict)] \
                    if isinstance(devs, list) else []
        elif kind == "DEVICE":
            # A receiver was activated or removed. gpsd sends the change, not
            # the resulting list, so ask for the list rather than guessing.
            self._poll_devices(force=True)
        elif kind == "TPV":
            self._handle_tpv(report)
        elif kind == "SKY":
            # uSat = satellites actually used in the solution. Fall back to
            # counting the `used` flags when gpsd doesn't send the summary.
            used = report.get("uSat")
            if used is None:
                sats = report.get("satellites")
                if isinstance(sats, list):
                    used = sum(1 for s in sats
                               if isinstance(s, dict) and s.get("used"))
            if used is not None:
                with self._lock:
                    self._sats_used = int(used)

    def _handle_tpv(self, report):
        """Store a TPV (time-position-velocity) report if it carries a fix.

        mode: 0 = unknown, 1 = no fix, 2 = 2D, 3 = 3D. Below 2 there is no
        position at all — gpsd still sends TPV, just without lat/lon, so the
        mode check is what separates "receiver alive" from "receiver located".
        """
        with self._lock:
            self._tpv_count += 1

        mode = report.get("mode") or 0
        lat = _finite(report.get("lat"))
        lon = _finite(report.get("lon"))
        if mode < 2 or lat is None or lon is None:
            return  # searching for satellites — keep the previous fix

        fix = {"lat": lat, "lon": lon, "fix_mode": int(mode)}

        alt = _finite(report.get("alt"))
        if alt is None:
            alt = _finite(report.get("altMSL"))
        if alt is None:
            alt = _finite(report.get("altHAE"))
        if alt is not None:
            fix["alt_m"] = round(alt, 1)

        speed_ms = _finite(report.get("speed"))     # gpsd reports m/s
        if speed_ms is not None:
            fix["speed_kmh"] = round(speed_ms * 3.6, 2)

        track = _finite(report.get("track"))        # course over ground
        if track is not None:
            fix["track_deg"] = round(track, 1)

        with self._lock:
            self._fix = fix
            self._fix_time = time.monotonic()

    # ------------------------------------------------------------------ #
    # Hardware debug                                                      #
    # ------------------------------------------------------------------ #
    def wait_for_devices(self, timeout=1.0):
        """Block briefly until gpsd has answered with its device list.

        Only for the startup log line: gpsd is on localhost and answers ?DEVICES
        in well under a millisecond, so this returns almost immediately when it
        is running, and costs `timeout` exactly once when it is not — which is
        itself the answer, and gets reported as such. Returns True if the list
        arrived. Never raises; nothing but logging may depend on it.
        """
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if self._devices is not None:
                    return True
            time.sleep(0.02)
        with self._lock:
            return self._devices is not None

    def hardware(self):
        """One debug line about the RECEIVER, not the fix.

        status() answers "do we have a position?". This answers the question
        you actually ask when we don't: *is the GPS even plugged in?* It puts
        gpsd's view and the kernel's view on the same line, which is what tells
        the three failure modes apart at a glance:

          • no serial node at all      → the receiver is unplugged / dead cable
          • node present, gpsd has none→ gpsd is pointed at the wrong device
                                         (DEVICES= in /etc/default/gpsd), or was
                                         started before the receiver enumerated
          • gpsd holds the device      → the hardware is fine; anything missing
                                         after this is sky view or antenna

        Never raises and never blocks: an unreadable /dev or /etc is reported
        as unknown, because a diagnostic that can fail is worse than none.
        """
        with self._lock:
            devices = list(self._device_info or [])
            known = self._devices
            connected = self._connected
            err = self._last_error
        ports, configured = self._scan_serial_hardware()

        if devices:
            gpsd_part = "gpsd: " + "; ".join(self._describe_device(d)
                                             for d in devices)
        elif known == []:
            gpsd_part = "gpsd: NO device"
            if configured:
                gpsd_part += f" (configured DEVICES={configured})"
        elif not connected:
            # No device list because there is no gpsd to ask. Say THAT — the
            # kernel half of the line is still worth printing, since a receiver
            # sitting there while gpsd is down is its own distinct fault.
            gpsd_part = ("gpsd: not running / unreachable"
                         + (f" ({err})" if err else ""))
        else:
            gpsd_part = "gpsd: device list not known yet"

        held = {d.get("path") for d in devices if d.get("path")}
        if ports:
            os_part = "kernel: " + "; ".join(
                self._describe_ports(name, paths, held)
                for name, paths in ports)
        else:
            os_part = "kernel: no USB/serial port — nothing enumerated"
        return f"GPS hardware: {gpsd_part} | {os_part}"

    @staticmethod
    def _describe_ports(name, paths, held):
        """One physical USB device's ports, with gpsd's one marked.

        A combined modem/GNSS module (the SIM7600 on this car) enumerates FIVE
        ttyUSB nodes from one plug. Listing each with its full by-id string ran
        to 400 characters of the same vendor name — unreadable, and the useful
        fact (which of the five is the GPS) was buried. So: one entry per
        physical device, and an arrow on the node gpsd is actually reading.
        """
        shown = []
        for path in paths:
            leaf = path.rsplit("/", 1)[-1]
            shown.append(f"{leaf}←gpsd" if path in held else leaf)
        ports = ", ".join(shown)
        if not name:
            return ports                # no by-id name (bare ttyUSB* scan)
        # The by-id string repeats manufacturer and product, so it is long and
        # mostly redundant; the tail (serial number) is the identifying part.
        label = name if len(name) <= 40 else name[:20] + "…" + name[-16:]
        return f"{label} ({ports})"

    @staticmethod
    def _describe_device(dev):
        """Format one gpsd DEVICE entry: what it is and how it's being read."""
        bits = [dev.get("path") or "?"]
        for key in ("driver", "subtype", "subtype1"):
            val = dev.get(key)
            if val:
                bits.append(str(val))
                break                 # driver name is enough; subtype is fallback
        bps = dev.get("bps")
        if bps:
            bits.append(f"{bps} bps")
        # activated is an ISO timestamp; its presence is the interesting part —
        # a device gpsd lists but has not activated is one it cannot read.
        bits.append("active" if dev.get("activated") else "NOT activated")
        return f"{bits[0]} ({', '.join(bits[1:])})" if len(bits) > 1 else bits[0]

    def _scan_serial_hardware(self):
        """(serial ports the kernel shows, DEVICES= from /etc/default/gpsd).

        Cached for _HARDWARE_SCAN_S: hardware() is called from the telemetry
        loop's change-detection, which runs many times a second, and this walks
        /dev and reads a file. USB enumeration is not that fast.
        """
        now = time.time()
        if now - self._hw_scan_time < _HARDWARE_SCAN_S:
            return self._hw_ports, self._hw_configured

        # [(device name, [node, ...]), ...] — grouped so a module that
        # enumerates several nodes from one plug reads as one device.
        groups = {}
        try:
            by_id = "/dev/serial/by-id"
            # by-id first: the symlink NAME carries the vendor/product string,
            # which is the difference between "a port exists" and "the u-blox
            # receiver is plugged in".
            for name in sorted(os.listdir(by_id)):
                target = os.path.realpath(os.path.join(by_id, name))
                # usb-<vendor>_<product>_<serial>-ifNN-portM → one key per
                # physical device, with the per-interface suffix removed.
                key = re.sub(r"-if[0-9a-fA-F]+-port\d+$", "",
                             re.sub(r"^usb-", "", name))
                groups.setdefault(key, []).append(target)
        except OSError:
            pass                       # no by-id tree (none plugged in) — fine
        ports = sorted(groups.items())
        if not ports:
            try:
                bare = sorted(f"/dev/{n}" for n in os.listdir("/dev")
                              if n.startswith(("ttyUSB", "ttyACM")))
            except OSError:
                bare = []
            ports = [("", bare)] if bare else []

        configured = None
        try:
            with open("/etc/default/gpsd", "r") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("DEVICES="):
                        configured = line.split("=", 1)[1].strip().strip('"\'')
                        break
        except OSError:
            pass                       # not a gpsd host (laptop) — nothing to say

        self._hw_ports, self._hw_configured = ports, configured
        self._hw_scan_time = now
        return ports, configured

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #
    def _poll_devices(self, force=False):
        """Ask gpsd for its device list, at most every _DEVICES_POLL_S.

        Never raises: a send that fails means the socket is gone, which the
        read side is about to discover and reconnect over. Losing the device
        list is a worse status line, never a worse fix.
        """
        now = time.time()
        if not force and now - self._last_devices_poll < _DEVICES_POLL_S:
            return
        self._last_devices_poll = now
        sock = self._sock
        if sock is None:
            return
        try:
            sock.sendall(_DEVICES_COMMAND)
        except OSError:
            pass

    def _close_socket(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        with self._lock:
            self._connected = False

    def _sleep_interruptibly(self, seconds):
        """Sleep in slices so stop() doesn't have to wait out the backoff."""
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(0.1)


# --------------------------------------------------------------------------- #
# Manual check — on the Pi:  python3 SolarRace_OS/modules/gps_reader.py
# Prints the same fixes the car will publish, so you can confirm the position
# is real BEFORE looking for it in the pit.
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    reader = GPSReader().start()
    print(f"Reading gpsd at {reader.host}:{reader.port} — Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1.0)
            print(f"{reader.status():<55} {reader.get_coordinates()}")
            print(f"    {reader.hardware()}")
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
