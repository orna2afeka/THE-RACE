"""
config.py — Hardware / connection configuration for SolarRace OS
=================================================================
Topology: TWO CAN buses — as of 2026-08-25 both at the SAME bitrate.

    Channel  Rate       Device  Protocol             IDs
    -------  ---------  ------  -------------------  -----------------
    can0     500 kbit/s mms     SiliXcon LYNX        0x600-0x628 (11b)
    can0     500 kbit/s mms     siliXcon ESC API     0x147 out, 0x150 in
    can1     500 kbit/s temp    J1939 thermistor     0x1839F3xx (29b)
    ?        500 kbit/s bms     JBD query/response   0x100-0x110 (11b)

    ⚠️ THIS TABLE WAS WRONG UNTIL 2026-09-18, and it cost the car its throttle
    for weeks. It claimed the MMS was on can1 and the temp module on can0; the
    truth is the other way round, measured with candump on both wires. Reading
    never noticed, because frames are dispatched by ID and not by channel —
    only the throttle request noticed, because it is the one frame the car
    TRANSMITS, and it was being sent down a wire the motor controller cannot
    hear. Which wire the BMS answers on has NOT been re-measured; the poll goes
    to both (BMS_POLL_CHANNELS), so it works either way, but BMS_CELL_OFFSETS
    below assigns pack A/B BY CHANNEL and may therefore have the two packs
    swapped. Verify that before trusting a per-pack cell alarm.

The engineering team reconfigured BOTH the BMS and the MMS to 500 kbit/s. The
MMS previously ran at 1 Mbit/s, and that disagreement is what USED to force the
split: a single CAN wire carries exactly one bitrate, and two nodes clocked
differently do not "mostly work" — each corrupts the other's frames and the bus
collapses.

That constraint is now gone, so the two channels are a WIRING fact rather than
a bitrate requirement. can0 and can1 are separate wires into separate
controllers on the HAT; the MMS is on can0 because that is where its wire
lands, and the J1939 temp module has can1. Reading is channel-agnostic (see
below), so collapsing the car onto one wire would be a wiring job plus a
re-measure — not an edit to this file.

    Both ends of a wire must be re-flashed together. Do NOT change the numbers
    below on their own: at a mismatched bitrate the interface comes up
    perfectly clean and simply never decodes a frame, and a node whose frames
    are never ACKed walks itself to bus-off. That is not hypothetical on this
    car — an earlier revision had the MMS on can0 and the BMS on can1, and the
    mismatch is exactly what drove can0 to BUS-OFF.

    After any rate change, re-verify on the car rather than assuming: a 0x5A
    query on 0x100 should draw a valid ~48V pack-voltage reply from the BMS,
    and 0x600-0x628 should stream continuously from the MMS on can0.

Reading is channel-agnostic on purpose: the three message-ID ranges don't
overlap (11-bit BMS vs 11-bit MMS vs 29-bit extended J1939), so frames from
every open bus are merged and dispatched by ID alone. Nothing downstream needs
to know which wire a frame arrived on — which is what makes moving a device
between channels a wiring change, not a code change.

The same code also works through a USB-to-CAN adapter instead of the HAT:
the candidates below are tried in order and the first that opens is used.

SocketCAN reminder — bring each channel up at ITS OWN bitrate first. Both are
500 kbit/s today, but each is set independently and they may diverge again:
    sudo ip link set can0 up type can bitrate 500000
    sudo ip link set can1 up type can bitrate 500000
"""
import sys
import os

# ── HUD screen targeting (Wayland: wayfire or labwc) ─────────────────── #
# Which output (by name, as reported by `wlr-randr` and by
# QGuiApplication.screens()) the driver HUD should request fullscreen on.
#
# None (the default here) means "let the compositor choose" — today's
# behaviour, and the only safe default for code that runs on more than one
# car: a screen name that doesn't exist on THIS Pi must never stop the HUD
# from starting. Per-car targeting belongs in the environment, not here —
# e.g. deploy/start_hud.sh exports SOLARRACE_HUD_SCREEN for the car whose
# HUD panel is DSI-2.
HUD_SCREEN_NAME = os.environ.get("SOLARRACE_HUD_SCREEN") or None

# ── Per-channel bitrates — every device on a channel must match ITS rate ─ #
#
# can0 and can1 are independent CAN controllers, so they are free to run at
# different speeds. Devices on the SAME channel must still agree with each
# other; devices on different channels need not. Both are at 500 kbit/s today;
# the map stays per-channel because that is a fact about the current hardware
# rather than a guarantee, and one device being re-flashed again must not mean
# restructuring this.
#
# ⚠️ For SocketCAN this dict does NOT set the bitrate. python-can's socketcan
# backend accepts `bitrate=` and ignores it — the real rate is whatever
# `ip link set canX up type can bitrate N` established (deploy/can-up.service,
# or bring_up_can_buses() in main.py, both of which read these numbers). So
# this is the ONE place to edit, but it only takes effect on a channel that
# this code brings up: change it here AND re-run the unit, or a channel left
# up from a previous boot keeps its old rate while the logs claim the new one.
CAN_BITRATES = {
    "can0": 500_000,
    "can1": 500_000,
}

# Fallback rate for anything not in CAN_BITRATES — in practice the USB-to-CAN
# adapters (pcan/slcan), where python-can DOES apply this value for real. A USB
# adapter is a single interface at a single rate, so the silent-bus escalation
# in open_usb_candidates() can only ever stand in for ONE of the two channels.
# While both channels run at 500 kbit/s that choice is moot and one adapter can
# read either wire; if the rates ever diverge again, this is what picks. Kept
# under the old name because it is still imported elsewhere and is still
# exactly what it always was: the default bitrate.
CAN_BITRATE = 500_000


def bitrate_for(channel):
    """The configured bitrate for one channel, falling back to CAN_BITRATE.

    Single source of truth so the bus we open, the label we print and the
    `ip link` command we shell out to can never disagree about a channel's
    rate — a mislabelled bus is worst precisely when you are debugging a
    mixed-rate car, because the log then confirms the wrong theory.
    """
    return CAN_BITRATES.get(channel, CAN_BITRATE)

# ── Connection candidates (tried in order; first that opens wins) ───── #
# Lets the same code run via the Pi's CAN HAT or a USB-to-CAN adapter.
CAN_CANDIDATES = [
    {"interface": "socketcan", "channel": "can0"},   # CAN HAT
    {"interface": "pcan", "channel": "PCAN_USBBUS1"},  # PEAK PCAN-USB
    # {"interface": "slcan", "channel": "/dev/ttyACM0"},  # CANable / slcan
]

# ── BMS polling (JBD is master/slave — host must request each frame) ─── #
# Host sends a frame with the wanted ID carrying one byte (0x5A); the BMS
# replies on the SAME ID with DLC 8. These are the IDs we know how to parse.
BMS_POLL_BYTE = 0x5A
BMS_POLL_INTERVAL_S = 1.0          # how often to request a full refresh

# ── TWO BMS units, one per channel ──────────────────────────────────────── #
# This car carries two JBD BMS units, each monitoring its own 13-cell pack.
#
# They answer on the SAME ids (0x100-0x110): JBD's ids are fixed and carry no
# device address, so two units cannot share a wire without colliding. That is
# why they are on separate channels — and it is also why THE BUS A REPLY
# ARRIVED ON IS THE ONLY THING THAT SAYS WHICH PACK IT DESCRIBES. Decode a
# reply without knowing its channel and the two packs silently overwrite each
# other; see _remap_bms_frame() in main.py, which is what keeps them apart.
#
# Both channels are polled. JBD is master/slave — a BMS says nothing at all
# until it is asked — so a channel missing from this tuple is a pack that
# reports NOTHING, with no error to notice: exactly the failure that left the
# pit showing 12 cells instead of 26.
#
# None means "poll every open bus". The bus-off worry that once restricted
# this to a single channel does not apply to either wire here: CAN acking is
# done by ANY node on the bus, not just the addressee, and both channels carry
# other live devices, so the polls are acked whether or not a BMS answers them.
BMS_POLL_CHANNELS = ("can0", "can1")

# Where each channel's cells land in the combined 1..26 numbering the pit and
# the HUD display (DS004 "Module 1..26"; limits.cell_temp_label's C_A*/C_B*).
# can0's pack is A (cells 1-13), can1's is B (cells 14-26).
#
# The offset for the SECOND pack must match how many cells the FIRST one has
# (limits.CELL_COUNT = 13). Getting it wrong does not fail loudly — it files
# pack B's readings under pack A's cell numbers — so if the pack geometry ever
# changes, change it here, and check limits.CELL_COUNT with it.
BMS_CELL_OFFSETS = {"can0": 0, "can1": 13}

# Channel whose pack-level readings (voltage, current, SoC, temps) are the
# headline ones. The other pack's are kept too, under a bms2_ prefix, rather
# than being allowed to overwrite these — two packs' SoC alternating in the
# same field at 2 Hz would read as a wildly flapping gauge.
BMS_PRIMARY_CHANNEL = "can0"

BMS_POLL_IDS = [
    0x100,  # Basic status: voltage / current / remaining capacity
    0x101,  # Capacity & cycles & SOC
    0x102,  # Balancing & protection flags
    0x104,  # Hardware specs (string / NTC count)
    0x105,  # Temperatures (NTC 1-3)
    0x107, 0x108, 0x109, 0x10A, 0x10B,  # Cell voltages (3 cells per ID)
    0x10C, 0x10D, 0x10E, 0x10F, 0x110,
]

# ── Throttle pedal via the ESC's GPIO-over-CAN API ──────────────────── #
#
# The motor controller does NOT broadcast its GPIO readings. It reports them
# only after being asked, so the car transmits one 8-byte request frame to
# 0x147 and the ESC then answers periodically on the selected bank's ID (bank 0
# -> 0x150). Protocol details and the byte layout live in
# modules/mms_parser.py; these are the knobs for it.
#
# ⚠️ THIS MAKES THE PI TRANSMIT TO THE MOTOR CONTROLLER. Set
# THROTTLE_GPIO_REQUEST_ENABLED = False if you would rather arm the report once
# with siliXcon's own configuration tool and have the Pi only listen — the
# decoder works either way and needs no other change. Two things to know before
# leaving it on:
#
#   * Unacked transmits are what drove can0 to BUS-OFF on this car (see the
#     module docstring at the top of this file). The request therefore goes out
#     on the MMS's channel ONLY (unlike the BMS poll, which now goes to every
#     channel in BMS_POLL_CHANNELS because there is a pack on each), and
#     main.py counts consecutive TX failures rather than retrying blindly.
#   * The frame configures a REPORT. It does not change a power map, a current
#     limit, or anything the motor acts on.
THROTTLE_GPIO_REQUEST_ENABLED = True

# Which wire the ESC is on. can0: a request sent down the other wire is never
# answered, and unanswered transmits are the thing that kills a channel.
# None = send on every open bus, which is only appropriate on a
# single-channel car.
#
# ⚠️ This said "can1" until 2026-09-18 and that is why the throttle was dead:
# can1 carries the J1939 temp module, which has nothing to say about a pedal.
# Measured, not assumed — `candump can0` streams 0x600-0x628, and a request
# sent to 0x147 on can0 draws replies on 0x150 within a frame or two.
THROTTLE_GPIO_CHANNEL = "can0"

# The ESC's node address, which travels in byte 0 of the request. 0 is the
# default address and matches the documented example. If the controller has
# been readdressed, this must follow it or the request is ignored — note the
# broadcast frames give a hint: they arrive at 0x600 + ADDR.
THROTTLE_GPIO_ADDRESS = 0

# Reporting bank 0..3, each with its own reply ID and sampling rate. Bank 0
# replies on 0x150. Pick a different bank only if something else on this car is
# already using bank 0 — two configurations of the same bank overwrite each
# other, silently.
THROTTLE_GPIO_BANK = 0

# Which inputs to sample. The pedal is GPIO0 (input ID 0x08) and that is the
# only value the decoder keeps — but the RANGE ASKED FOR IS DELIBERATELY WIDER.
#
# ⚠️ Do not narrow this back to GPIO0 alone. Asking for 0x08..0x08 is what the
# vendor's docs imply, and on this controller it does NOT work: the ESC answers
# once with "0D 00 00 00 00 00" — inputs 0 and 1, both zero — and then stops.
# Asking for 0x00..0x0C makes it stream properly at the requested period, and
# GPIO0's real reading arrives in the frame tagged 0x08. Measured on the car
# 2026-09-18; the reason for the narrow-request behaviour is not understood, so
# this is a workaround rather than a fix, and it costs two extra frames per
# period on a bus that is nowhere near full.
#
# The wide sweep is also how you FIND the pedal if it is ever moved to another
# pin: watch which input ID's value tracks somebody's foot.
THROTTLE_GPIO_START_ID = 0x00    # the ESC's first input ID
THROTTLE_GPIO_END_ID = 0x0C      # GPIO4; the vendor example's top of range

# How often the ESC samples and reports, in milliseconds. 100 ms (10 Hz) is the
# documented example and the right cadence for the pit's throttle trace: fast
# enough to show the driver dabbing the pedal, cheap enough to be invisible on
# a 500 kbit/s wire.
THROTTLE_GPIO_PERIOD_MS = 100
THROTTLE_GPIO_DELAY_MS = 100

# How often the car RE-SENDS the request. The ESC's report configuration does
# not survive a controller power cycle, and the controller can be power-cycled
# without the Pi noticing, so a one-shot request at startup would leave the
# throttle dead for the rest of the race. Re-arming every 5 s costs one frame
# per 5 s and makes recovery automatic.
THROTTLE_REQUEST_INTERVAL_S = 5.0

# ── Simulation ──────────────────────────────────────────────────────── #
# Recorded CAN log replayed for simulation (contains MMS + BMS + temp frames).
SIM_LOG_PATH = "SolarRace_OS/data/can_dump.txt"

# Pin the system to log-replay and NEVER go live, even if real CAN traffic
# appears. Normally leave this False: in AUTO mode the app already replays the
# log whenever the bus is quiet/absent, and switches to live by itself when
# real frames arrive. Set True only to force pure simulation (e.g. demos).
FORCE_SIMULATION = False

# AUTO mode: once live, if the bus goes completely quiet for this many seconds,
# fall back to replaying the log so the dashboards never freeze (it switches
# back to live as soon as traffic resumes). Set to 0 to never leave live.
CAN_SILENCE_TIMEOUT_S = 5.0

# ── USB fallback (silent-bus escalation) ─────────────────────────────── #
# can0/can1 can open successfully (the HAT is present) yet carry no traffic —
# e.g. the car is actually wired up through a USB-to-CAN adapter instead of
# the HAT this run. If the open buses stay completely silent this long,
# SmartCANWorker also starts searching for a USB adapter (see
# open_usb_candidates() below) ALONGSIDE the HAT channels, rather than only
# trying USB when can0/can1 fail to open at all. 0 disables the escalation.
USB_SILENCE_FALLBACK_S = 15.0

# How often to retry the USB search once escalation has kicked in (probing
# every loop iteration would mean repeatedly trying to open a device node
# that isn't there).
USB_FALLBACK_RETRY_S = 5.0


# ── CAN link-state probe (bash) ─────────────────────────────────────── #
# The SocketCAN channels we care about. main.py brings both up at boot.
CAN_LINK_CHANNELS = ("can0", "can1")


def can_link_state(channels=CAN_LINK_CHANNELS):
    """
    Ask the OS (via `ip link show <chan>`) whether each CAN interface is UP.

    Returns a dict {channel: "UP" | "DOWN" | "ABSENT"}.

    Returns an EMPTY dict when the `ip` tool isn't available at all (e.g. when
    developing off the Pi on Windows/macOS) so callers can fall back to the
    bus-open result instead of wrongly reporting "not connected".
    """
    import re
    import subprocess

    states = {}
    for ch in channels:
        try:
            out = subprocess.run(
                ["ip", "link", "show", ch],
                capture_output=True, text=True, timeout=1.0,
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            return {}  # no `ip` command here — state is unknowable
        if out.returncode != 0:
            states[ch] = "ABSENT"          # "Device does not exist"
            continue
        # Flags live between the angle brackets, e.g. "<NOARP,UP,LOWER_UP>".
        m = re.search(r"<([^>]*)>", out.stdout)
        flags = m.group(1).split(",") if m else []
        states[ch] = "UP" if "UP" in flags else "DOWN"
    return states


# ── Bus registry: guarantees every interface is closed exactly once ──── #
#
# python-can's BusABC.__del__ prints "<Bus> was not properly shut down"
# whenever a successfully-opened bus is garbage-collected without shutdown()
# having been called. On the Pi that message kept appearing because a QThread
# is a C++ thread: the interpreter does NOT wait for it at exit, so any exit
# path that does not stop the CAN worker first (a crash in the GUI thread, a
# quit route that skips _stop_can(), or plain interpreter teardown) leaves the
# worker's buses open and lets the garbage collector find them.
#
# Chasing every one of those paths individually is whack-a-mole. Instead every
# bus this module opens is recorded here and closed by an atexit hook, so the
# guarantee holds no matter HOW the process ends or which thread owned the bus.
# Calling shutdown() normally (as _shutdown_bus does) is still the primary
# path — this is only the safety net, and shutdown() is documented as safe to
# call more than once.
# Each entry: {"bus": BusABC, "label": str, "where": str}. "where" is the call
# site that opened it, so if the safety net ever HAS to rescue a bus we can name
# the code that leaked it instead of guessing.
_OPEN_BUSES = []

# Bumped whenever this registry changes. Printed at startup so a log from the
# car proves which build is actually running — the fastest way to tell a real
# bug from a file that was never copied to the Pi.
CAN_REGISTRY_BUILD = "bus-registry/2"


# ── USB adapter pre-flight: don't "open" hardware that cannot be there ── #
#
# Trying a USB candidate that is absent is not free and not silent. python-can
# builds the Bus object BEFORE it talks to the adapter (PcanBus calls
# super().__init__ last), so a constructor that raises still leaves a
# half-built bus for the garbage collector — and BusABC.__del__ then logs
# "PcanBus was not properly shut down" even though nothing was ever open.
# With the silent-bus escalation retrying every USB_FALLBACK_RETRY_S, that
# phantom warning repeated every 5 s for the whole session on a car that has
# no PCAN adapter at all, burying the log lines that matter.
#
# So: ask cheaply whether the backend could possibly work before constructing
# anything. Absent → skip, no object, no warning, no subprocess.
_BACKEND_LIB_CACHE = {}

# What each non-socketcan backend needs present on this machine. The driver
# library is what python-can dlopen()s; a missing one means the backend can
# never open, adapter plugged in or not.
_BACKEND_LIBS = {
    "pcan": ("pcanbasic", "PCBUSB"),   # PEAK PCANBasic (Linux/Windows, macOS)
    "ixxat": ("vcinpl2", "vcinpl"),
    "vector": ("vxlapi64", "vxlapi"),
    "kvaser": ("canlib32", "canlib"),
}


def _backend_library_present(interface):
    """True when the driver library this backend needs can be found.

    Cached: ctypes.util.find_library() can shell out to ldconfig/gcc, which is
    far too expensive to repeat on a 5-second retry timer. A driver library
    does not appear mid-race — installing one means apt and a restart — so one
    lookup per process is the right granularity.
    """
    names = _BACKEND_LIBS.get(interface)
    if not names:
        return True                     # unknown backend — let it try
    if interface in _BACKEND_LIB_CACHE:
        return _BACKEND_LIB_CACHE[interface]
    import ctypes.util
    found = any(ctypes.util.find_library(n) for n in names)
    _BACKEND_LIB_CACHE[interface] = found
    return found


def candidate_available(cand):
    """Cheap "could this candidate possibly open?" check.

    Deliberately conservative — it only reports False when we are CERTAIN the
    open would fail (no driver library, or a serial device node that does not
    exist). Anything it cannot prove absent is still tried, so this can never
    hide a working adapter; it only removes doomed attempts.
    """
    interface = cand.get("interface")
    channel = cand.get("channel")
    if interface == "socketcan":
        return True                     # the HAT; `ip link` is the real probe
    # slcan / serial adapters are named by device node — a missing node is
    # conclusive, and os.path.exists() is cheap enough for the retry timer.
    if isinstance(channel, str) and channel.startswith("/dev/"):
        return os.path.exists(channel)
    return _backend_library_present(interface)


class _quiet_unopened_bus_warning:
    """Silence BusABC.__del__'s warning for a bus that never actually opened.

    A failed constructor leaves an object whose _is_shutdown is still False,
    so __del__ reports it as leaked — a warning about a bus that was never
    open, with no way to call shutdown() on it because the constructor never
    returned a reference. The filter is installed only around the construction
    call and removed immediately after; release() below is what makes the
    phantom object's __del__ run while the filter is still in place.

    Narrow on purpose: it drops ONLY the "not properly shut down" message, and
    only during an open attempt. A genuinely leaked bus is still reported by
    shutdown_all_buses()' safety net at exit, which names its call site.
    """

    _PHANTOM = "was not properly shut down"

    def __init__(self):
        import logging
        self._log = logging.getLogger("can.bus")

    def _filter(self, record):
        return self._PHANTOM not in record.getMessage()

    def __enter__(self):
        self._log.addFilter(self._filter)
        return self

    def __exit__(self, exc_type, exc, tb):
        self._log.removeFilter(self._filter)
        return False                    # never swallow the open error itself

    @staticmethod
    def release(exc):
        """Let the half-built bus die NOW, while the filter is still on.

        Call this from the `except` that caught a failed open. The exception's
        traceback holds the backend's __init__ frame, and that frame holds the
        half-built `self` — so without this the object outlives the filter and
        logs its phantom warning from wherever the caller's except block ends.
        Only the traceback is dropped, never the exception or its message (all
        any caller here ever shows), and the re-raise attaches a fresh one.
        """
        import gc
        exc.__traceback__ = None
        gc.collect()


def _new_bus(interface, channel):
    """Open one bus and register it for guaranteed cleanup at exit.

    Strong references on purpose: a weak one would let the collector reach the
    bus first, which is the very thing that produces the warning. The list is
    pruned of already-closed buses on every call, so it only ever holds the
    interfaces that are actually open.
    """
    import can       # local import so this module stays light for tooling
    import traceback

    # Drop buses that have already been shut down properly, so a car that
    # reconnects for hours doesn't accumulate dead entries.
    _OPEN_BUSES[:] = [r for r in _OPEN_BUSES
                      if not getattr(r["bus"], "_is_shutdown", True)]

    # The filter only covers the construction itself: a bus that fails to
    # open must not log itself as a leak (see _quiet_unopened_bus_warning).
    with _quiet_unopened_bus_warning() as quiet:
        try:
            bus = can.interface.Bus(
                interface=interface,
                channel=channel,
                bitrate=bitrate_for(channel),
            )
        except BaseException as exc:
            quiet.release(exc)
            raise
    label = _bus_label(interface, channel)
    where = "".join(traceback.format_stack(limit=4)[:-1]).strip().splitlines()
    _OPEN_BUSES.append({
        "bus": bus,
        "label": label,
        "where": where[-1].strip() if where else "unknown",
    })
    # _safe_print, not print: a bare print can raise UnicodeEncodeError when
    # stdout is a log file opened in a non-UTF-8 locale, and the callers of
    # this function treat ANY exception as "the adapter did not open" — so a
    # failed log line would discard a bus that is in fact open and registered.
    _safe_print(f"[can] opened {label} "
                f"[{CAN_REGISTRY_BUILD}; {len(_OPEN_BUSES)} open]")
    return bus


def _safe_print(msg):
    """print() that can never raise, whatever stdout's encoding is.

    Diagnostics must never be able to break the thing they are reporting on.
    This is not hypothetical: an emoji in a shutdown message raised
    UnicodeEncodeError under an ASCII locale and stopped the CAN bus from
    being closed at all, which is precisely how a bus ends up leaked.
    """
    try:
        print(msg)
    except Exception:
        try:                      # last resort: strip anything unencodable
            enc = (getattr(sys.stdout, "encoding", None) or "ascii")
            print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))
        except Exception:
            pass                  # a log line is never worth raising over


def _bus_label(interface, channel):
    """The one place a connection's human-readable name is built."""
    return f"{interface}:{channel} @ {bitrate_for(channel) // 1000}kbps"


def shutdown_all_buses():
    """Close any interface still open. Idempotent; safe from any thread.

    Anything this has to close was NOT closed by its owner, which is a real
    defect worth seeing rather than silently papering over — so each rescue is
    reported with the call site that opened it.
    """
    for rec in list(_OPEN_BUSES):
        bus = rec["bus"]
        try:
            if getattr(bus, "_is_shutdown", True):
                continue                     # already closed by its owner
            # shutdown() FIRST, reporting second. The reverse order is what
            # broke this once already: the log line raised and the bus was
            # left open — the safety net defeated by its own diagnostics.
            bus.shutdown()
            _safe_print(f"[can] SAFETY NET: {rec['label']} was still open at "
                        f"exit and has been closed. Opened at: {rec['where']}")
        except Exception as exc:
            _safe_print(f"[can] SAFETY NET: failed to close {rec['label']}: {exc}")
    _OPEN_BUSES.clear()


import atexit  # noqa: E402  (registered next to the registry it protects)
atexit.register(shutdown_all_buses)


# ── Connection helper ───────────────────────────────────────────────── #
def open_bus():
    """
    Try each connection in CAN_CANDIDATES in order and return
    (bus, label, error):

        bus    — an opened can.BusABC, or None if none opened.
        label  — human-readable description of the connection that opened.
        error  — the last exception seen (None on success).

    Catches broadly because a missing backend can raise CanError, OSError,
    or ImportError depending on the adapter/driver.
    """
    last_exc = None
    for cand in CAN_CANDIDATES:
        if not candidate_available(cand):
            continue                # no driver / no device node — can't open
        try:
            bus = _new_bus(cand["interface"], cand["channel"])
            return bus, _bus_label(cand["interface"], cand["channel"]), None
        except Exception as exc:
            last_exc = exc
            continue
    return None, None, last_exc


def open_buses():
    """
    Open EVERY available CAN interface so the app can listen to both channels
    at once. Returns (buses, errors):

        buses  — list of (bus, label) for each interface that opened.
        errors — list of (channel, exception) for every one that didn't.

    Strategy: open the two SocketCAN channels (can0 AND can1) — the car wires
    devices across both, so we read them in parallel. If NEITHER SocketCAN
    channel opens (e.g. running off-Pi through a USB adapter), fall back to the
    non-SocketCAN CAN_CANDIDATES and open the first that works.
    """
    buses = []
    errors = []

    # 1) Both on-board SocketCAN channels.
    for ch in CAN_LINK_CHANNELS:            # ("can0", "can1")
        try:
            buses.append((_new_bus("socketcan", ch), _bus_label("socketcan", ch)))
        except Exception as exc:
            errors.append((ch, exc))

    # 2) USB-adapter fallback — only if no SocketCAN channel came up.
    if not buses:
        for cand in CAN_CANDIDATES:
            if cand["interface"] == "socketcan":
                continue                    # already tried above
            if not candidate_available(cand):
                continue                    # no driver / no device node
            try:
                buses.append((_new_bus(cand["interface"], cand["channel"]),
                              _bus_label(cand["interface"], cand["channel"])))
                break
            except Exception as exc:
                errors.append((cand["channel"], exc))

    return buses, errors


def open_usb_candidates():
    """
    Try every non-socketcan entry in CAN_CANDIDATES (the USB-to-CAN adapters)
    and return (bus, label) for the first that opens, or (None, None) if none
    do.

    Split out of open_bus()/open_buses() so SmartCANWorker's silence fallback
    can search for a USB adapter WITHOUT touching can0/can1 first — those stay
    open and are still read in parallel; the USB bus is simply added alongside
    them if one turns up.
    """
    for cand in CAN_CANDIDATES:
        if cand["interface"] == "socketcan":
            continue                    # that's the HAT, not USB
        if not candidate_available(cand):
            # Nothing to open: the backend's driver library isn't installed,
            # or the serial node isn't there. Skipping keeps the silent-bus
            # retry free instead of constructing a doomed bus every 5 s.
            continue
        try:
            bus = _new_bus(cand["interface"], cand["channel"])
            return bus, _bus_label(cand["interface"], cand["channel"])
        except Exception:
            continue
    return None, None


# ----------------------------------------------------------------------- #
# Bring the Pi CAN HAT channels up automatically at boot.
#
# A ready-made unit lives at deploy/can-up.service — do not hand-write one:
#
#   sudo cp deploy/can-up.service /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now can-up.service
#
# It uses the CAN_BITRATES above (both channels at 500 kbit/s) and txqueuelen
# 65536, matching what the app expects, and brings up can1 as well when the car
# has a second channel. Keep the unit and CAN_BITRATES in step by hand, because
# systemd cannot read this file — that is the whole reason the rates are
# written out twice, and it stays true now that they are equal: whoever changes
# one number has to change the other. A unit still carrying the old 1 Mbit/s
# for can1 would hold the MMS wire at the wrong speed, and a wrong bitrate
# presents exactly like a dead bus.
#
# The txqueuelen bump matters: the default CAN queue is tiny (~10 frames),
# and the BMS poller sends a 16-frame burst — without it you can hit
# "no buffer space available" (ENOBUFS) even on a healthy bus.
#
# Verify traffic with:   candump can0
# Inspect bus health / errors:   ip -details -statistics link show can0
# ----------------------------------------------------------------------- #


# ----------------------------------------------------------------------- #
# WHICH BATTERY IS "THE BATTERY"
# ----------------------------------------------------------------------- #
# The car has two packs, each with its own BMS: A on can0, B on can1 (B's
# readings are the ones prefixed bms2_). Wherever the car needs ONE answer --
# the driver's SoC gauge, the battery current beside it, and the charge
# detector's "is current flowing into the pack" -- it reads this pack.
#
# "A", the normal setting. It was "B" for the end of the 2026-09-19/20 race
# only, after pack A's BMS froze at about 01:50 that night, mid-race:
# it went on reporting 77 %, 53.7 V and +35.9 A, its last values from the
# charger, for over an hour while the car was driving and pack B fell from
# 68 % to 59 %. The driver's gauge read 77 %. Worse, +35.9 A is CHARGING as
# far as the charge detector is concerned, so the first time the car stood
# still for five seconds it would have announced a charge that was not
# happening -- and the pit counts those against the three the regulations
# allow.
#
# Both packs are still read, logged and sent to the pit exactly as before, and
# a fault on EITHER pack still reaches the driver. This only chooses which one
# answers when the question is "the battery's". Pit_Dashboard/constants.py has
# the pit's copy of the same setting; change them together.
MAIN_BMS = "A"
