"""
net_monitor.py — is the car actually online?
=============================================
Answers one question for the driver's HUD: can this Pi reach the internet right
now. A daemon thread probes a couple of well-known addresses on a slow cadence
and parks the answer in a snapshot; callers copy that snapshot and never wait.

    net = NetMonitor().start()
    ...
    net.get_status()["net_status"]     # "online" | "offline" | "unknown"

WHY THIS IS A THREAD AND NOT A CALL
-----------------------------------
Every way of asking "am I online?" can block for seconds, and there is no
timeout you can set that makes that untrue — an unreachable host does not
answer, it simply says nothing until something gives up. On a cellular link in
a moving car that is the normal case, not the exception.

The HUD repaints on the Qt GUI thread and the telemetry loop pushes to Firebase
on its own; a check that stalls either one would freeze the speed gauge or drop
samples to render a status light, which is a bad trade at any price. So the
probe lives here, alone, on a thread of its own, and the only thing the HUD ever
does is read an attribute under a lock.

FOUR THINGS THIS FILE EXISTS TO GET RIGHT
-----------------------------------------
1. NO DNS ON THE CHECK PATH. Targets are IP LITERALS. Probing a hostname would
   put a resolver lookup in front of every check, and a stalled resolver blocks
   for its own timeout — typically 5 s per nameserver, ignoring the socket
   timeout entirely, because the stall happens before the socket exists. That
   is precisely the multi-second block this module exists to keep off the HUD.

2. A DROPPED PACKET IS NOT AN OUTAGE. A cellular link in a moving car loses
   single probes constantly. An indicator that goes red every time one is lost
   is an indicator the driver learns to ignore within a lap, and then it cannot
   warn them about the outage that matters. So offline needs
   FAILURES_BEFORE_OFFLINE consecutive failures; online needs one success.
   The asymmetry is deliberate — bad news must be corroborated, good news is
   safe to show at once.

3. "NOT CHECKED YET" IS NOT "OFFLINE". The status starts UNKNOWN and is shown
   unlit, exactly like the MAP and solar badges show a dash before their first
   reading. Booting the HUD into a red warning light that means nothing but
   "give me five seconds" spends the driver's attention on our startup.

4. A WEDGED PROBE MUST NOT READ AS HEALTHY. If the thread dies or a connect
   hangs past every deadline, the last answer would sit there looking live
   forever. get_status() ages it out: a snapshot older than STALE_AFTER_S
   reports UNKNOWN, so a frozen check shows as "don't know" rather than a
   confident green.

WHAT THIS DOES *NOT* TELL YOU
-----------------------------
That the pit is receiving telemetry. Reaching 8.8.8.8 proves the link is up, not
that Firebase accepted the last write — the two come apart exactly when it
matters (auth expiry, a blocked port, a Firebase outage, a full quota). Treat a
green NET badge as "the radio is working", not as "the pit can see me".
If the car ever needs the stronger claim, feed it the timestamp of the last
successful push rather than making this module guess.
"""

import socket
import sys
import threading
import time


def safe_print(msg):
    """print() that can never raise, whatever stdout's encoding is.

    Same helper and same reason as solar_current.py: this runs headless on the
    Pi with its output going to a boot log whose encoding we do not control, and
    a diagnostic must never be able to break the thing it is reporting on.
    """
    try:
        print(msg)
    except Exception:
        try:
            enc = getattr(sys.stdout, "encoding", None) or "ascii"
            print(str(msg).encode(enc, "replace").decode(enc, "replace"))
        except Exception:
            pass


# ── Tuning ─────────────────────────────────────────────────────────────────── #
# Probe targets, tried in order until one answers. IP literals only (see note 1
# in the header). Port 53 is DNS: both operators answer TCP there, it is open
# through essentially every network that has a working uplink at all, and it
# needs no credentials of ours.
#
# Two DIFFERENT OPERATORS, not two addresses from one: 8.8.4.4 would fail in the
# same breath as 8.8.8.8 whenever Google is the thing that is unreachable, which
# makes the second target decoration rather than redundancy.
DEFAULT_TARGETS = (
    ("8.8.8.8", 53),      # Google Public DNS
    ("1.1.1.1", 53),      # Cloudflare
)

# Per-target connect timeout. Long enough not to libel a slow cellular handover
# as an outage, short enough that a full offline sweep of every target still
# finishes well inside CHECK_INTERVAL_S.
CONNECT_TIMEOUT_S = 2.0

# How often to probe. Deliberately slow: the driver cannot act on sub-second
# connectivity news, and a tighter loop would spend the car's cellular data
# allowance proving it still has a cellular data allowance.
CHECK_INTERVAL_S = 5.0

# Consecutive failures before the badge goes red (see note 2 in the header).
FAILURES_BEFORE_OFFLINE = 2

# A snapshot older than this is not an answer any more (see note 4). Three
# intervals plus a full offline sweep, so an ordinary slow cycle never trips it.
STALE_AFTER_S = CHECK_INTERVAL_S * 3 + CONNECT_TIMEOUT_S * len(DEFAULT_TARGETS)

# ── Status values ──────────────────────────────────────────────────────────── #
STATUS_UNKNOWN = "unknown"   # not probed yet, or the last answer aged out
STATUS_ONLINE = "online"     # a target answered
STATUS_OFFLINE = "offline"   # FAILURES_BEFORE_OFFLINE sweeps found nothing


class NetMonitor:
    """Background internet-reachability probe with a non-blocking snapshot."""

    def __init__(self, targets=DEFAULT_TARGETS,
                 interval_s=CHECK_INTERVAL_S,
                 timeout_s=CONNECT_TIMEOUT_S,
                 failures_before_offline=FAILURES_BEFORE_OFFLINE):
        self.targets = tuple(targets)
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self.failures_before_offline = max(1, int(failures_before_offline))

        self._lock = threading.Lock()
        self._thread = None
        self._running = False
        # Event rather than a sleep loop: stop() can wake the thread out of its
        # idle wait instantly instead of it noticing up to a poll-slice later.
        self._wake = threading.Event()

        # --- everything below is guarded by self._lock -------------------- #
        self._status = STATUS_UNKNOWN
        self._checked_time = 0.0     # time.time() of the last completed sweep
        self._last_ok_time = 0.0     # time.time() of the last success, 0 if never
        self._consecutive_failures = 0
        self._responder = None       # ("8.8.8.8", 53) that answered, or None
        self._check_count = 0        # proves the thread is really running
        self._last_error = None      # the OSError text from the last failure

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #
    def start(self):
        """Begin probing in the background. Idempotent. Returns self."""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._running = True
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._run, name="net-monitor", daemon=True)
        self._thread.start()
        return self

    def request_stop(self):
        """Ask the thread to finish. Returns IMMEDIATELY — never joins.

        For shutdown paths that must not block, which on this car is all of
        them: the HUD's _fast_exit has a deadline to meet and a socket already
        inside connect() cannot be interrupted, only outlived. The thread is a
        daemon, so leaving it mid-probe costs nothing at exit.
        """
        self._running = False
        self._wake.set()

    def stop(self, timeout_s=1.0):
        """Ask the thread to finish and wait briefly for it. Safe to call twice.

        The join is BOUNDED and missing it is not an error: a probe blocked in
        connect() will outlast any deadline worth waiting for, and the thread is
        a daemon precisely so that abandoning it here is harmless.
        """
        self.request_stop()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout_s)
        self._thread = None

    # ------------------------------------------------------------------ #
    # The only public read                                                #
    # ------------------------------------------------------------------ #
    def get_status(self):
        """Snapshot of the newest answer. Returns immediately, never raises.

        Always a dict, so callers need no None-check before indexing:

            net_status        STATUS_* — the value a status light should show
            net_online        True / False / None, None meaning "don't know"
            net_last_ok_age_s seconds since the last success, None if never
            net_stale         True when the probe stopped producing answers
            net_responder     which target answered, or None
        """
        now = time.time()
        with self._lock:
            status = self._status
            checked = self._checked_time
            last_ok = self._last_ok_time
            failures = self._consecutive_failures
            responder = self._responder
            count = self._check_count
            error = self._last_error

        # An answer nobody has refreshed is not an answer (see note 4 in the
        # header). Only a status we actually established can go stale — UNKNOWN
        # is already the "don't know" value and has nothing to decay into.
        stale = bool(checked) and (now - checked) > STALE_AFTER_S
        if stale and status != STATUS_UNKNOWN:
            status = STATUS_UNKNOWN

        return {
            "net_status": status,
            "net_online": (True if status == STATUS_ONLINE else
                           False if status == STATUS_OFFLINE else None),
            "net_last_ok_age_s": (now - last_ok) if last_ok else None,
            "net_consecutive_failures": failures,
            "net_stale": stale,
            "net_responder": responder,
            "net_check_count": count,
            "net_last_error": error,
        }

    def is_online(self):
        """True / False / None. None means "not established", never "offline"."""
        return self.get_status()["net_online"]

    # ------------------------------------------------------------------ #
    # Background thread — the ONLY place that touches a socket            #
    # ------------------------------------------------------------------ #
    def _run(self):
        while self._running:
            responder = self._probe()
            self._record(responder)
            # Wait on the event, not time.sleep: request_stop() wakes us out of
            # this the moment it is called.
            self._wake.wait(self.interval_s)

    def _probe(self):
        """Try each target until one accepts a connection.

        Returns the (host, port) that answered, or None. Never raises: every
        failure mode here — refused, unreachable, timed out, no route, no
        interface — is an OSError subclass and all of them mean the same thing
        to a status light.
        """
        error = None
        for target in self.targets:
            if not self._running:       # stop() arrived mid-sweep; abandon it
                break
            try:
                # Closed via the context manager. An unclosed socket per probe
                # would be a slow file-descriptor leak that only shows up hours
                # into a race, which is the worst time to discover it.
                with socket.create_connection(target, timeout=self.timeout_s):
                    return target
            except OSError as exc:
                error = f"{target[0]}:{target[1]} {exc.__class__.__name__}: {exc}"
        with self._lock:
            self._last_error = error
        return None

    def _record(self, responder):
        """Fold one sweep result into the snapshot, with the flap guard."""
        now = time.time()
        with self._lock:
            self._checked_time = now
            self._check_count += 1
            self._responder = responder

            if responder is not None:
                self._consecutive_failures = 0
                self._last_ok_time = now
                self._status = STATUS_ONLINE
                return

            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failures_before_offline:
                self._status = STATUS_OFFLINE
            # Otherwise leave the status alone: one lost probe is not an
            # outage, and rewriting it here is exactly the flap we are
            # suppressing. UNKNOWN stays UNKNOWN, ONLINE stays ONLINE until
            # a second sweep agrees the link is gone.


# ---------------------------------------------------------------------------
# Standalone check — run this on the Pi to prove the probe works before
# blaming the HUD:
#
#     python SolarRace_OS/modules/net_monitor.py
#
# Expect "online" within a few seconds, naming whichever target answered. Pull
# the cellular dongle and it should go "offline" after two sweeps (~10 s), then
# back to "online" a sweep after you plug it in.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    monitor = NetMonitor().start()
    try:
        while True:
            s = monitor.get_status()
            age = s["net_last_ok_age_s"]
            safe_print(
                f"{s['net_status']:<8} checks={s['net_check_count']:<4} "
                f"fails={s['net_consecutive_failures']} "
                f"via={s['net_responder'][0] if s['net_responder'] else '-':<8} "
                f"last_ok={f'{age:.0f}s ago' if age is not None else 'never'}"
                + (f"  [{s['net_last_error']}]" if s["net_last_error"] and
                   s["net_status"] != STATUS_ONLINE else "")
            )
            time.sleep(1.0)
    except KeyboardInterrupt:
        monitor.stop()
        safe_print("stopped")
