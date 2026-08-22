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
        self._fix_time = 0.0      # time.time() when _fix was stored
        self._connected = False
        self._sats_used = None    # from SKY reports; nice for diagnostics
        self._last_error = None
        self._tpv_count = 0       # TPV reports seen (proves gpsd is talking)

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
            age = time.time() - self._fix_time
            sats = self._sats_used
        out["fix_age_s"] = round(max(0.0, age), 1)
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
            age = time.time() - self._fix_time if has_fix else None
            sats = self._sats_used

        if not connected:
            return f"GPS: gpsd unreachable at {self.host}:{self.port}" + (
                f" ({err})" if err else "")
        if not has_fix:
            extra = f", {sats} sats" if sats is not None else ""
            return (f"GPS: connected to gpsd, no fix yet "
                    f"({tpv} reports{extra})")
        sat_txt = f", {sats} sats" if sats is not None else ""
        if age > FIX_STALE_AFTER_S:
            return f"GPS: fix STALE ({age:.0f}s old{sat_txt})"
        return f"GPS: fix OK ({age:.1f}s old{sat_txt})"

    @property
    def has_fix(self):
        """True when we hold a position that is current (not stale)."""
        with self._lock:
            if self._fix is None:
                return False
            return (time.time() - self._fix_time) <= FIX_STALE_AFTER_S

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
        sock.sendall(_WATCH_COMMAND)
        self._sock = sock
        with self._lock:
            self._connected = True
            self._last_error = None

    def _stream(self):
        """Read newline-delimited JSON until the socket dies or we're stopped."""
        buf = b""
        while self._running:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                # gpsd has nothing to say (commonly: no device attached).
                # The connection is fine — keep waiting.
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
        if kind == "TPV":
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
            self._fix_time = time.time()

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #
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
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
