"""
config.py — Hardware / connection configuration for SolarRace OS
=================================================================
Topology: ONE shared high-speed CAN bus (ISO 11898-2) on a single channel
(the Pi's CAN HAT can0). All three devices sit on that one bus:

    Device   Protocol             IDs                Notes
    -------  -------------------  -----------------  ---------------------------
    mms      SiliXcon LYNX        0x600-0x628 (11b)  Motor controller (HUD)
    bms      JBD (query/response) 0x100-0x110 (11b)  Battery — must be POLLED
    temp     J1939 thermistor     0x1839F3xx (29b)   Battery-temp module (broadcasts)

The three message-ID ranges don't overlap (11-bit BMS vs 11-bit MMS vs
29-bit extended J1939), so every frame is decoded off the single bus with
no collisions, and the BMS poller transmits on the same bus.

⚠️ SINGLE-BUS REQUIREMENT: because they share one wire, EVERY device must
be configured to the SAME bitrate as CAN_BITRATE below. The MMS/LYNX runs
at 500 kbit/s; the BMS baud is user-definable (set it to match); the J1939
temp module is often fixed at 250 kbit/s — if it cannot be changed to this
rate, it cannot share this bus and would need its own adapter. Verify all
three agree before racing.

The same code also works through a USB-to-CAN adapter instead of the HAT:
the candidates below are tried in order and the first that opens is used.

SocketCAN reminder — bring the channel up at this bitrate first, e.g.:
    sudo ip link set can0 up type can bitrate 500000
"""

# ── Shared bus bitrate — ALL devices must match this ────────────────── #
CAN_BITRATE = 1_000_000

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
BMS_POLL_IDS = [
    0x100,  # Basic status: voltage / current / remaining capacity
    0x101,  # Capacity & cycles & SOC
    0x102,  # Balancing & protection flags
    0x104,  # Hardware specs (string / NTC count)
    0x105,  # Temperatures (NTC 1-3)
    0x107, 0x108, 0x109, 0x10A, 0x10B,  # Cell voltages (3 cells per ID)
    0x10C, 0x10D, 0x10E, 0x10F, 0x110,
]

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
_OPEN_BUSES = []


def _new_bus(interface, channel):
    """Open one bus and register it for guaranteed cleanup at exit.

    Strong references on purpose: a weak one would let the collector reach the
    bus first, which is the very thing that produces the warning. The list is
    pruned of already-closed buses on every call, so it only ever holds the
    interfaces that are actually open.
    """
    import can  # local import so this module stays light for tooling

    # Drop buses that have already been shut down properly, so a car that
    # reconnects for hours doesn't accumulate dead entries.
    _OPEN_BUSES[:] = [b for b in _OPEN_BUSES
                      if not getattr(b, "_is_shutdown", True)]

    bus = can.interface.Bus(
        interface=interface,
        channel=channel,
        bitrate=CAN_BITRATE,
    )
    _OPEN_BUSES.append(bus)
    return bus


def _bus_label(interface, channel):
    """The one place a connection's human-readable name is built."""
    return f"{interface}:{channel} @ {CAN_BITRATE // 1000}kbps"


def shutdown_all_buses():
    """Close any interface still open. Idempotent; safe from any thread."""
    for bus in list(_OPEN_BUSES):
        try:
            if not getattr(bus, "_is_shutdown", True):
                bus.shutdown()
        except Exception:
            pass  # best effort — we are shutting down regardless
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
# It uses CAN_BITRATE (1 Mbit/s) and txqueuelen 65536, matching what the app
# expects, and brings up can1 as well when the car has a second channel. An
# earlier version of this comment documented 500 kbit/s and txqueuelen 1000 for
# can0 only — a unit built from that would put the bus at half speed with a
# queue too small for the BMS burst, which presents exactly like a dead bus.
#
# The txqueuelen bump matters: the default CAN queue is tiny (~10 frames),
# and the BMS poller sends a 16-frame burst — without it you can hit
# "no buffer space available" (ENOBUFS) even on a healthy bus.
#
# Verify traffic with:   candump can0
# Inspect bus health / errors:   ip -details -statistics link show can0
# ----------------------------------------------------------------------- #
