"""
main.py — Master Race OS (Final Pit-Wall Edition)
=================================================
This is the core execution file for the Solar Car Telemetry System.
It integrates background CAN bus reading, fallback simulation, 
real-time data parsing (BMS/MMS), Firebase cloud synchronization, 
and the PySide6 Driver HUD in a fully thread-safe architecture.
"""
import sys
import os
# Ensure this folder (SolarRace_OS/) is importable no matter how the app is
# launched (repo root, inside the folder, or a systemd unit on the Pi).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Repo root too, for drivetrain.py — the speed/gearing definitions the pit
# dashboard also imports, so both ends of the telemetry link agree.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import time
import json
import struct
import can
import signal
import traceback
import logging

# ---------------------------------------------------------------------------
# Make stdout/stderr unbreakable BEFORE anything prints.
#
# deploy/start_hud.sh redirects all output to a log FILE, and Python takes that
# stream's encoding from the locale. A desktop autostart session often has no
# LANG set, which yields ASCII — and every emoji in this codebase (there are
# dozens: the CAN status lines, the lap messages, the GPS banner) then raises
# UnicodeEncodeError on print(). Inside the CAN worker loop that kills the
# worker thread outright, leaving the CAN interfaces open, which is how
# python-can ends up reporting "<Bus> was not properly shut down".
#
# errors="replace" means an unencodable character degrades to '?' instead of
# raising. A log line must never be able to take the car off the air.
# ---------------------------------------------------------------------------
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass        # Python <3.7 or an already-wrapped stream; best effort
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QGuiApplication
from PySide6.QtCore import QObject, Signal, QTimer
import subprocess
# --- Core Logic & Parsing Modules ---
# NOTE: the HUD this app runs is driver_dash_v2.RacingDashboard, and it is the
# only one. A second, standalone pyqtgraph dashboard used to live in
# dashboard.py with a dead `import dashboard` here; the import pulled in
# pyqtgraph and a whole second Qt GUI at startup, so the app refused to boot on
# any machine that had not installed a dependency it never used. Both are gone.
# `dashboard` below is a LOCAL variable in main(), unrelated.
from modules.bms_parser import parse_jbd_bms_message
# Rule 3.5.6 rolling 2-hour cell extremes (repo root, shared with the pit).
from cell_extremes import RollingExtremes
from modules.mms_parser import parse_mms_message
# The same module again, by name: _request_gpio_report() needs the GPIO-over-CAN
# protocol constants and the request builder, and writing them as
# `mms_parser.GPIO_REQUEST_ID` at the point of use says where they came from
# instead of leaving four more bare names in this file's namespace.
from modules import mms_parser
from modules.temp_controller_parser import (
    parse_temp_controller_message, parse_thermistor_general_message,
)
from modules.gps_reader import GPSReader
from modules.lap_tracker import LapTracker
from modules.charge_detector import ChargeDetector
from modules.lap_command import LapCommandInbox, StrategyCommandInbox
from modules.vehicle_inputs import VehicleInputs
from modules.regen_light import RegenLight

# --- Cloud Sync Modules ---
from cloud.firebase_client import (
    initialize_firebase, push_telemetry_to_cloud, listen_driver_command,
    ack_lap_command, ack_strategy, stop_telemetry_uploader)

# --- Upgraded GUI Modules ---
from can_worker import CANWorker, _word_to_alerts, _ERROR_BITS, _LIMIT_BITS
import driver_dash_v2
from driver_dash_v2 import RacingDashboard, RACING_QSS

# --- Connection configuration (shared CAN bus + BMS polling) ---
from config import (
    open_buses,
    open_usb_candidates,
    can_link_state,
    shutdown_all_buses,
    CAN_REGISTRY_BUILD,
    CAN_BITRATE,
    bitrate_for,
    CAN_SILENCE_TIMEOUT_S,
    USB_SILENCE_FALLBACK_S,
    USB_FALLBACK_RETRY_S,
    BMS_POLL_IDS,
    BMS_POLL_BYTE,
    BMS_POLL_INTERVAL_S,
    BMS_POLL_CHANNELS,
    BMS_CELL_OFFSETS,
    BMS_PRIMARY_CHANNEL,
    THROTTLE_GPIO_REQUEST_ENABLED,
    THROTTLE_GPIO_CHANNEL,
    THROTTLE_GPIO_ADDRESS,
    THROTTLE_GPIO_BANK,
    THROTTLE_GPIO_START_ID,
    THROTTLE_GPIO_END_ID,
    THROTTLE_GPIO_PERIOD_MS,
    THROTTLE_GPIO_DELAY_MS,
    THROTTLE_REQUEST_INTERVAL_S,
    HUD_SCREEN_NAME,
)

# --- Race & Vehicle Constants ---
# Gear ratio and wheel size now come from drivetrain.py at the repo root, shared
# with the pit so the odometer, the HUD speedometer and the pit tiles all agree.
# Re-exported here because this module's own name for them is referenced widely.
import drivetrain      # noqa: E402  (sys.path prepared at the top of this file)
import track           # noqa: E402  circuit geometry, shared with the pit
import limits          # noqa: E402  battery temp = hottest plausible cell
import speed_profile   # noqa: E402  target-speed curves, shared with the pit

GEAR_RATIO = drivetrain.GEAR_RATIO
WHEEL_CIRCUMFERENCE_METERS = drivetrain.TIRE_CIRCUMFERENCE_METERS
TRACK_LENGTH_METERS = track.TRACK_LENGTH_METERS   # Circuit Zolder, Belgium

# --- GPS publishing -------------------------------------------------------- #
# Telemetry is normally pushed from _decode_message, i.e. only when a CAN frame
# arrives. GPS is independent of CAN, so on its own that would mean no position
# in the pit whenever the bus is quiet (car parked, ignition off, CAN unplugged)
# — exactly when you most want to check the map is working. So the read loop
# also publishes on this timer, which keeps the pit's position alive with or
# without CAN traffic. Matches firebase_client's own 0.5s throttle, so this adds
# no extra writes while CAN is live.
GPS_PUBLISH_INTERVAL_S = 0.5

# --- Health heartbeat ------------------------------------------------------ #
# The Pi publishes at least this often NO MATTER WHAT -- no CAN traffic, no GPS
# fix, nothing.
#
# Without it there is one failure the pit cannot see at all. Telemetry is
# normally pushed from _decode_message (a CAN frame arrived) or from the GPS
# timer, and that timer is gated on actually having a fix. So a Pi that is
# powered, networked and running, but whose CAN is unplugged, in a garage with
# no sky, publishes NOTHING -- and on the pit wall that is indistinguishable
# from a dead Pi, a flat battery or a WiFi dropout. The crew would go looking
# for the wrong fault.
#
# With the heartbeat, that case says exactly what it is: the feed stays live and
# reports "can0 silent 47s, no GPS fix".
#
# Cheap: push_telemetry_to_cloud throttles itself to PUBLISH_INTERVAL_SECONDS,
# so while CAN is live the 0.5 s pushes already satisfy this and the heartbeat
# adds no writes at all. It only actually fires when nothing else is publishing.
HEARTBEAT_INTERVAL_S = 5.0

# A channel quiet for longer than this is called out by name in the health
# string. Deliberately longer than CAN_SILENCE_TIMEOUT_S: this is "worth telling
# the pit about", not "blank the driver's gauges".
CHANNEL_QUIET_AFTER_S = 3.0

# How often the lap trigger looks at GPS. Faster than the publish rate on
# purpose — see _sample_lap_gps.
LAP_GPS_SAMPLE_INTERVAL_S = 0.1

# --- Lap-tracker reboot persistence ----------------------------------------- #
# LapTracker.state_dict()/.restore() exist so a Pi reboot mid-race doesn't
# throw away the running distance/energy totals -- but until this, nothing
# ever called them: every restart genuinely started odometer_m and the energy
# totals from zero while the motor controller's own hardware TRIP counter
# (mms_trip_m) kept counting underneath it, unaffected. That mismatch is
# exactly what showed up as a tiny "Odometer" beside a much larger "Trip" on
# the pit dashboard.
#
# Stored next to main.py (not the repo root, not inside modules/) so it reads
# as what it is: this Pi's own runtime state, the same way
# Pit_Dashboard/telemetry.db is that machine's runtime state -- both are
# gitignored for the same reason.
LAP_CHECKPOINT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "lap_checkpoint.json")

# Written on the CAN worker thread inside the read loop (same as _publish_gps
# below), so a value this small is cheap: it's a stat + a small JSON dump, not
# something worth measuring against a tick this frequent. 15 s bounds how much
# gets lost to a crash (as opposed to a clean quit, which saves unconditionally
# in _teardown) without meaningfully wearing an SD card over a 24 h race.
LAP_CHECKPOINT_INTERVAL_S = 15.0

# Rule 3.5.6 report (HUD screen R3.5.6): the highest/lowest cell temperature and
# voltage over the last 2 hours. Saved beside the lap checkpoint so a Pi restart
# mid-race does not wipe up to two hours of the report. Once a minute is enough:
# the window is kept in one-minute buckets, so a more frequent save would only
# rewrite the same buckets.
CELL_EXTREMES_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cell_extremes.json")
CELL_EXTREMES_SAVE_S = 60.0
# The report changes at most once per reading; the screen needs it once a second.
CELL_EXTREMES_EMIT_S = 1.0

# How often the brake/lights switches are read. 5 Hz is instant to a human eye
# and costs nothing; gpiozero debounces the contacts for us.
VEHICLE_INPUT_POLL_S = 0.2

# Target speed refresh. 5 Hz: fast enough that the number
# tracks the car down a straight, slow enough to be free.
PROFILE_TICK_S = 0.2

# Which profile the car runs until the pit says otherwise. The baseline is the
# safe default — it is the lap the team actually measured.
DEFAULT_STRATEGY = "base_210s"

# ==============================================================================
# SMART CAN WORKER (Core Background Thread)
# ==============================================================================
class SmartCANWorker(CANWorker):
    """
    Extends the UI CANWorker to drive the full pit-wall build.

    All three devices share ONE CAN bus (see config.py). This worker reads
    that single bus and decodes BMS + MMS + temp frames off it (their IDs
    don't overlap). The BMS is master/slave, so it is also polled here.

    This worker is ALWAYS in real (live) mode — it never replays a log. When
    the bus can't be opened, or is open but silent, every gauge is forced to
    zero and the status bar reports why: "CAN NOT CONNECTED" when the OS says
    the interface is down/absent, or "CAN CONNECTED — SILENCE" when the link
    is up but no frames are arriving. That distinction is made with a bash
    `ip link show` probe (see config.can_link_state).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # All open CAN interfaces we read in parallel (can0 + can1). Each entry
        # is a (bus, label) tuple. The base class keeps a single self._bus we
        # don't use here.
        self._buses: list = []
        # Health tracking. Per CHANNEL, not one global "last frame": two CAN
        # channels carry different devices (can1 is the second BMS), so one of
        # them dying while the other keeps talking is a real and otherwise
        # invisible fault -- the aggregate would stay healthy the whole time.
        self._boot_ts = time.time()
        self._last_frame_by_channel: dict = {}
        self._frames_by_channel: dict = {}
        self._can_state = "starting"
        self.vehicle_state = {
            "battery": {},          # JBD BMS
            "motor": {},            # SiliXcon LYNX MMS
            "temp_controller": {},  # J1939 battery-temperature module
            "gps": {},
        }
        # Distance, energy, laps and lap timing all live in one object so they
        # cannot drift apart — a lap is defined by distance, lap energy is the
        # integral between two triggers, lap time the interval between them.
        self.laps = LapTracker()
        self._last_checkpoint_save = 0.0
        self._load_lap_checkpoint()
        # Rule 3.5.6 rolling 2 h extremes. Owned by this (CAN worker) thread,
        # like LapTracker: readings are offered where they are decoded, and the
        # report is emitted from the loop on a timer.
        self.cell_extremes = RollingExtremes()
        kept = self.cell_extremes.load(CELL_EXTREMES_PATH)
        if kept:
            print(f"📋 rule 3.5.6 report resumed: {kept} minute(s) of the last 2 h")
        self._last_extremes_emit = 0.0
        # Live BMS NTC probes per pack, {"A": {1: °C, ...}, "B": {...}}.
        self._bms_probe_C = {}
        self._last_extremes_save = time.monotonic()
        self.lap_inbox = LapCommandInbox()
        # Detects a real charging stop from bms_current_A + mms_rpm — see
        # charge_detector.py for why both readings are needed (current alone
        # can't tell a charger from regen braking). Fed on the same ~2 Hz timer
        # as the GPS publish below, in _publish_gps; the two latest readings
        # are cached as they arrive off the bus (see _decode_message).
        self._charge_detector = ChargeDetector()
        self._last_bms_current_A = None
        self._last_mms_rpm = None
        self._last_lap_gps_sample = 0.0
        # Parking brake + lights switches, wired to the Pi's GPIO (the motor
        # controller has no visibility of them). Reports None until the pins
        # are configured in modules/vehicle_inputs.py, which the HUD shows as
        # UNKNOWN rather than as a confident "off".
        self.vehicle_inputs = VehicleInputs()
        self._last_input_poll = 0.0
        # Brake light, driven by regenerative braking. The ESC reports negative
        # power when it is recovering energy, which means the car is slowing --
        # and nothing else on the car knows that, because the brake pedal switch
        # does not move when the driver simply lifts and lets regen do the work.
        # See modules/regen_light.py, and READ ITS ELECTRICAL NOTE before wiring:
        # a GPIO pin switches a MOSFET, it does not drive a lamp.
        self.regen_light = RegenLight()
        # Target speed. The car holds every generated
        # profile and the pit switches between them by name, so a strategy
        # change is a few bytes over the link instead of a 400-row table.
        self.strategy_inbox = StrategyCommandInbox()
        self.profiles = speed_profile.load_all(
            lap_length_m=track.TRACK_LENGTH_METERS)
        self.active_strategy = DEFAULT_STRATEGY
        self._last_profile_tick = 0.0
        # Lap start last sent to the HUD stopwatch. A sentinel, not None: None
        # is a real value ("lap start unknown") that must still be sent once.
        self._lap_timer_sent = object()
        self.last_cloud_print = time.time()
        self._last_bms_poll = 0.0
        self._poll_fail_count = 0
        # Throttle report: the ESC only sends GPIO readings after being asked,
        # and forgets the request when it reboots — so this is re-armed on a
        # timer rather than sent once. See _request_gpio_report().
        self._last_gpio_request = 0.0
        self._gpio_fail_count = 0
        self._gpio_requested_ok = False
        self._have_bms_soc = False   # once True, ignore the LYNX SoC estimate
        # DS003 — individual cell temperatures from the Orion Thermistor
        # Expansion Module's per-sensor broadcast. _thermistor_configured is
        # STICKY (see cell_temps_updated's docstring in can_worker.py): once a
        # real per-sensor frame ever arrives, it stays True for the rest of
        # the session even through a later bus silence, because a thermistor
        # slot that was never loaded/enabled on the module has NO frame of
        # its own that means "not configured" -- see
        # modules/temp_controller_parser.py's docstring. _cell_temps_C is NOT
        # sticky: _emit_zeros() clears it like every other reading.
        self._thermistor_configured = False
        self._cell_temps_C: dict = {}      # {cell_num (1-indexed): temp_C}
        # DS004 — individual cell voltages, accumulated the same way as
        # DS003's temperatures (the JBD BMS reports 3 cells per frame across
        # 10 CAN IDs). No "configured" flag needed: unlike the Thermistor
        # Expansion Module, the BMS's own cell taps are always present once
        # it is polled at all -- gate on bms_string_count instead, exactly
        # like the pit dashboard's DS004 section already does.
        self._cell_voltages_V: dict = {}   # {cell_num (1-indexed): volts}
        # The driver HUD's alert bar shows a SINGLE combined list, but MMS
        # (0x600) and BMS (0x102) faults arrive on separate frames. Cache each
        # so a fresh frame from one device doesn't wipe the other's alerts.
        self._mms_alerts: list = []   # (label, severity) from the MMS status word
        # JBD protections, keyed by CAN channel — one entry per BMS/pack. A
        # dict, not a list, because the two packs report independently and
        # pack A clearing its faults must not erase pack B's.
        self._bms_alerts_by_channel: dict = {}
        # Each pack's own bms_string_count, so the published figure can be the
        # SUM across packs (see _remap_bms_frame).
        self._bms_string_counts: dict = {}
        # Throttle for the "no data" status probe so we don't spawn `ip link`
        # (a subprocess) on every idle loop iteration.
        self._last_link_check = 0.0
        self._last_status_text = ""
        # USB fallback: when can0/can1 open but go silent, we start searching
        # for a USB-to-CAN adapter too (see _maybe_fallback_to_usb). Tracks
        # when the current silence began and whether a USB bus is already in
        # self._buses, so we don't add it twice or hammer the device search.
        self._silent_since = 0.0
        self._usb_fallback_tried_at = 0.0
        self._usb_bus_active = False
        self._bus_label = None
        # GPS via gpsd. Its own daemon thread does the blocking socket reads, so
        # reading a position here never stalls the CAN loop. Constructing it
        # cannot fail (no gpsd / no receiver just means "no fix yet").
        self.gps = GPSReader()
        self._last_gps_publish = 0.0
        self._last_heartbeat = 0.0
        self._last_gps_log = ""
        # Same change-only logging as _last_gps_log, for the receiver-hardware
        # debug line: it must appear the moment a GPS is plugged in or drops
        # off, and never once a second in between.
        self._last_gps_hw_log = ""

    def run(self) -> None:
        """QThread entry point — runs the read loop with GUARANTEED teardown.

        The loop body lives in _run_loop(); this wrapper exists solely so that
        every CAN interface is released no matter HOW the loop ends.

        Why it matters: the loop's own try/except only catches can.CanError,
        but the loop also runs Firebase pushes, GPS sampling, lap/strategy
        commands and frame decoding. An unexpected exception from any of those
        used to propagate straight out of run(), killing the worker thread with
        the buses still open — _shutdown_bus() was after the while loop, not in
        a finally, so it never ran. The open PcanBus then survived until
        interpreter exit, where python-can's BusABC.__del__ printed
        "PcanBus was not properly shut down". That warning was the SYMPTOM; a
        silently dead worker thread was the actual fault.
        """
        try:
            self._run_loop()
        except BaseException:
            # Print it: a worker thread dying used to be invisible apart from
            # the frozen gauges, which is far harder to diagnose than a stack.
            print("💥 CAN worker thread died with an unhandled exception:")
            traceback.print_exc()
            raise
        finally:
            self._teardown()

    def _teardown(self) -> None:
        """Release everything the worker owns. Safe to call on any exit path.

        Each step is independently guarded so that one failing helper cannot
        stop the others — in particular it must never stop _shutdown_bus(),
        which is the one that actually closes the CAN hardware.
        """
        for name, stop in (
            # Unconditional (force=True) so a clean quit doesn't lose up to
            # LAP_CHECKPOINT_INTERVAL_S of distance/energy to the throttle.
            ("lap checkpoint", lambda: self._save_lap_checkpoint(force=True)),
            ("cell extremes", lambda: self.cell_extremes.save(CELL_EXTREMES_PATH)),
            # Bounded: uploads what fits in a few seconds, the rest stays in
            # telemetry_outbox.db and goes up at the next start.
            ("telemetry outbox", stop_telemetry_uploader),
            ("gps", self.gps.stop),
            ("lap inbox", self.lap_inbox.stop),
            ("strategy inbox", self.strategy_inbox.stop),
            ("vehicle inputs", self.vehicle_inputs.stop),
            # Before the bus goes down, so the lamp is explicitly extinguished
            # rather than left showing whatever it was doing when we quit.
            ("regen brake light", self.regen_light.stop),
            ("CAN bus(es)", self._shutdown_bus),
        ):
            try:
                stop()
            except Exception as exc:
                print(f"⚠️ Error shutting down {name}: {exc}")

    def _run_loop(self) -> None:
        """
        Always-real read loop (never replays a log), reading BOTH can0 and can1
        in parallel.

        Continuously:
          • (re)open every available CAN interface if none are open;
          • drain and decode every queued frame from ALL buses → LIVE;
          • poll the BMS on a timer (on every bus) while live;
          • when no bus opens, or all buses are silent for
            CAN_SILENCE_TIMEOUT_S, force all gauges to zero and report the
            reason ("NOT CONNECTED" vs "SILENCE", decided by a bash probe).
        """
        self._running = True
        self._bus_label = None
        last_real = 0.0
        state = None  # one of: "live", "silent", "disconnected"

        # Start streaming positions from gpsd in the background. Independent of
        # CAN: GPS keeps working (and keeps reaching the pit) even if the bus
        # never opens.
        self.gps.start()
        # Give the reader its first round trip to gpsd before reporting, so
        # both lines below state what IS rather than "connecting...". Bounded
        # at 1 s, and only paid in full when gpsd is down — which is itself
        # what the lines then say.
        self.gps.wait_for_devices(1.0)
        self._last_gps_log = self.gps.log_line()
        print(f"🛰️ {self._last_gps_log}")
        # The receiver itself, not the fix: gpsd's device list next to the
        # kernel's serial ports. Printed at startup (and again below whenever
        # it changes) because "no fix" has three very different causes and
        # this line is the one that says which — see GPSReader.hardware().
        self._last_gps_hw_log = self.gps.hardware()
        print(f"🛰️ {self._last_gps_hw_log}")
        print(f"🏁 {track.TRACK_LENGTH_METERS:.0f} m lap, finish line "
              f"{track.FINISH_LINE_LAT:.6f}, {track.FINISH_LINE_LON:.6f}")

        # Pit lap commands. The car must run without Firebase (no network at
        # scrutineering, credentials missing on a bench Pi), exactly as main()
        # already tolerates a missing driver-command listener.
        try:
            self.lap_inbox.start()
            print("🏁 Listening for pit lap commands...")
        except Exception as exc:
            print(f"🏁 Lap-command listener unavailable: {exc}")

        self.vehicle_inputs.start()
        print(f"🔌 {self.vehicle_inputs.status()}")

        self.regen_light.start()
        print(f"🛑 {self.regen_light.status()}")

        if self.profiles:
            print(f"🎯 {len(self.profiles)} speed profile(s) loaded; "
                  f"active: {self.active_strategy}")
            try:
                self.strategy_inbox.start()
                print("🎯 Listening for pit strategy changes...")
            except Exception as exc:
                print(f"🎯 Strategy listener unavailable: {exc}")
        else:
            print("⚠️ No speed profiles found — no target speed. "
                  "Generate them with: python tools/generate_profiles.py")

        while self._running:
            # ---- Pit commands and lap detection, CAN or no CAN ------------ #
            # First in the loop, deliberately: several branches below `continue`
            # (no bus opened, CAN read error), and those are exactly the cases
            # where a GPS-only push is the pit's only position source — and
            # where the pit most needs its manual Cut Lap to still work.
            self._apply_lap_commands()
            self._apply_strategy_commands()
            self._sample_lap_gps()
            self._poll_vehicle_inputs()
            # A tick, not a poll: the lamp's minimum-on hold and its stale
            # release are timers, and a quiet bus is exactly when they matter.
            self.regen_light.tick()
            self._tick_profile()
            self._publish_lap_timer()
            self._publish_gps()
            self._save_lap_checkpoint()
            self._tick_cell_extremes()

            # ---- (Re)open the buses if we have none ---------------------- #
            if not self._buses:
                self._buses, errors = open_buses()
                if not self._buses:
                    if state != "disconnected":
                        state = self._can_state = "disconnected"
                        print(f"⚠️ No CAN bus opened ({errors}).")
                        self._emit_zeros()
                    self._report_no_data(bus_open=False)
                    self._interruptible_sleep(1.0)
                    continue
                self._bus_label = " + ".join(lbl for _, lbl in self._buses)
                print(f"✅ CAN bus(es) open: {self._bus_label}. Live mode.")
                last_real = time.time()   # brief grace before calling it silent
                state = self._can_state = "silent"
                self._silent_since = last_real
                # open_buses() already falls back to a USB adapter itself when
                # NEITHER can0 nor can1 opens — if that's what happened, a USB
                # bus (e.g. PCAN) is already in self._buses. Mark the fallback
                # as done so _maybe_fallback_to_usb() never opens a SECOND
                # handle to the same USB channel (that's what was producing
                # "PCAN bus was not properly shut down" — two Bus() instances
                # fighting over one physical adapter).
                self._usb_bus_active = any(
                    not lbl.startswith("socketcan:") for _, lbl in self._buses)

            # ---- Drain whatever real traffic is queued on ANY bus -------- #
            got_any = False
            try:
                for bus, _ in self._buses:
                    # The channel travels with the frame from here on. Two BMS
                    # units answer on identical ids, so this is the ONLY thing
                    # that says which pack a reading belongs to — dropping it
                    # here is what let one pack overwrite the other.
                    channel = getattr(bus, "channel", None)
                    while True:
                        msg = bus.recv(timeout=0.0)
                        if msg is None:
                            break
                        got_any = True
                        last_real = time.time()
                        if state != "live":
                            state = self._can_state = "live"
                            print("📡 Live CAN traffic detected.")
                            self._set_status(f"● CAN LIVE  |  {self._bus_label}")
                        self._decode_message(msg, channel)
            except can.CanError as exc:
                print(f"⚠️ CAN read error: {exc} — reopening buses.")
                self._shutdown_bus()          # forces a reopen next iteration
                state = self._can_state = "disconnected"
                self._emit_zeros()
                self._report_no_data(bus_open=False)
                self._interruptible_sleep(0.5)
                continue

            now = time.time()

            if state == "live":
                # Poll the BMS on a timer (it only answers when queried).
                if now - self._last_bms_poll >= BMS_POLL_INTERVAL_S:
                    self._poll_bms()
                    self._last_bms_poll = now
                # Re-arm the ESC's throttle report. Deliberately on its own
                # timer and not folded into the BMS poll: they go to different
                # devices on different wires at different rates, and a change
                # to one must not silently retime the other.
                if (THROTTLE_GPIO_REQUEST_ENABLED and
                        now - self._last_gpio_request >= THROTTLE_REQUEST_INTERVAL_S):
                    self._request_gpio_report()
                    self._last_gpio_request = now
                # Fall to silent if ALL buses go completely quiet.
                if CAN_SILENCE_TIMEOUT_S and now - last_real > CAN_SILENCE_TIMEOUT_S:
                    state = self._can_state = "silent"
                    self._silent_since = now
                    print(f"⚠️ {CAN_SILENCE_TIMEOUT_S:.0f}s CAN silence — zeroing gauges.")
                    self._emit_zeros()

            if state == "silent":
                # Buses are open but no frames — zero gauges & report why.
                self._report_no_data(bus_open=True)
                self._maybe_fallback_to_usb(now)

            # The heartbeat, last in the pass so it reports the state this
            # iteration just settled on. Unconditional: no CAN and no GPS fix
            # is precisely the case it exists for.
            if now - self._last_heartbeat >= HEARTBEAT_INTERVAL_S:
                self._last_heartbeat = now
                self._publish_heartbeat()

            if not got_any:
                time.sleep(0.05)  # yield the CPU while idle

        # No teardown here on purpose — run()'s finally calls _teardown(), so
        # it happens on EVERY exit path (clean stop, crash, or stop() request),
        # not just when the loop ends normally.

    # ------------------------------------------------------------------ #
    # No-data handling (zero the HUD + report the reason)                 #
    # ------------------------------------------------------------------ #
    def _emit_zeros(self) -> None:
        """Blank every HUD gauge and clear the alert bar.

        Blank, not zero. This runs when the bus has gone quiet, and a quiet bus
        means we do not know any of these values — it does not mean they are 0.
        Zeroing them was actively misleading: a driver glancing down saw 0 A and
        0 °C and read a coasting car with a cold motor, when the truth was that
        the car had stopped telling us anything. The gauges render None as an em
        dash (see driver_dash_v2._NO_DATA), which the motor-temp field and the
        map badge below already did via their own sentinels.
        """
        self.rpm_updated.emit(None)
        # Speed is its own reading now, not a function of RPM, so it has to be
        # blanked explicitly — otherwise the speedo would freeze on the last
        # number the controller sent while everything around it went to dashes.
        self.speed_updated.emit(None)
        self.voltage_updated.emit(None)
        self.soc_updated.emit(None)
        self.power_updated.emit(None)
        self.controller_temp_updated.emit(None)
        self.motor_current_updated.emit(None)
        self.battery_current_updated.emit(None)
        # The pedal blanks to a dash like every gauge. It must NOT fall back to
        # the neutral point: "coasting" is a thing the driver is doing, and a
        # dead bus is not evidence that they are doing it.
        self.throttle_updated.emit(None, None, None)
        self.cell_temp_updated.emit(None)
        # Values blank like every other gauge; _thermistor_configured does
        # NOT reset -- a module that has already proven it's configured stays
        # configured through a later dead bus.
        self._cell_temps_C = {}
        self.vehicle_state["temp_controller"]["battery_temp_C"] = None
        self.cell_temps_updated.emit(self._thermistor_configured, {})
        # DS004 blanks like every other gauge — no sticky flag to preserve,
        # since a dead bus means the BMS itself has gone quiet too.
        self._cell_voltages_V = {}
        self.cell_voltages_updated.emit(None, {})
        # Live BMS probes blank like every other live reading. The R3.5.6
        # extremes above them are NOT blanked — see _tick_cell_extremes.
        self._bms_probe_C = {}
        self.bms_probe_temps_updated.emit({})
        # 0 Ω is below the PT1000's physical floor, so the HUD reads it as
        # "no sensor data" and blanks both fields rather than showing the
        # -246 °C that extrapolating 0 Ω would imply.
        self.motor_temp_updated.emit(0.0, -1000.0, "no_reading")
        self.alerts_updated.emit([])
        # The controller has stopped talking, so we can no longer say it is on.
        # Brake/lights are GPIO-sourced and unaffected by a dead CAN bus, so
        # they keep whatever the Pi can actually read.
        gpio = (self.vehicle_inputs.read() if self.vehicle_inputs is not None
                else {"parking_brake": None, "lights_on": None})
        self._last_flags = {"ecu_on": False, "reverse": False, **gpio}
        self.vehicle_flags_updated.emit(dict(self._last_flags))
        # Clear the power map too. A stale "Map 3" badge sitting on screen while
        # the bus is dead would tell the driver something we no longer know.
        self._last_motor_map = None
        self.motor_map_updated.emit("", -1)

    def _report_no_data(self, bus_open: bool) -> None:
        """
        Set the top-left status while no telemetry is arriving.

        Uses a bash `ip link show` probe to tell the two cases apart:
          • an interface is UP  → "CAN CONNECTED — SILENCE (no data)"
          • all down / absent   → "CAN NOT CONNECTED"
        The probe is throttled to once per second (it spawns a subprocess).
        When `ip` isn't available (dev off-Pi) we fall back to whether the
        python-can bus handle opened.
        """
        now = time.time()
        if now - self._last_link_check < 1.0:
            return
        self._last_link_check = now

        states = can_link_state()
        up = [ch for ch, s in states.items() if s == "UP"]
        if states:                              # bash probe worked
            if up:
                text = f"◐ CAN CONNECTED — SILENCE (no data)  |  {', '.join(up)}"
            else:
                text = "○ CAN NOT CONNECTED  |  interface down"
        else:                                   # `ip` unavailable — use bus handle
            text = ("◐ CAN CONNECTED — SILENCE (no data)" if bus_open
                    else "○ CAN NOT CONNECTED")
        self._set_status(text)

    def _maybe_fallback_to_usb(self, now: float) -> None:
        """
        If can0/can1 opened fine but have produced no traffic for
        USB_SILENCE_FALLBACK_S, also start listening on a USB-to-CAN adapter —
        the car may be wired up that way this run instead of through the HAT.

        Added ALONGSIDE the existing buses, not instead of them: if the HAT
        starts talking again, both keep being read. Only ever adds one USB bus
        per connection cycle (_usb_bus_active), and retries the device search
        no more than every USB_FALLBACK_RETRY_S so a missing adapter doesn't
        get probed on every loop iteration.
        """
        if not USB_SILENCE_FALLBACK_S or self._usb_bus_active:
            return
        if now - self._silent_since < USB_SILENCE_FALLBACK_S:
            return
        if now - self._usb_fallback_tried_at < USB_FALLBACK_RETRY_S:
            return
        self._usb_fallback_tried_at = now

        bus, usb_label = open_usb_candidates()
        if bus is None:
            return
        self._usb_bus_active = True
        self._buses.append((bus, usb_label))
        self._bus_label = " + ".join(lbl for _, lbl in self._buses)
        print(f"🔌 No CAN traffic for {USB_SILENCE_FALLBACK_S:.0f}s — "
              f"found a USB adapter too, now also listening on {usb_label}.")

    def _set_status(self, text: str) -> None:
        """Emit a status string only when it changes (avoids UI churn)."""
        if text != self._last_status_text:
            self._last_status_text = text
            self.status_updated.emit(text)

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep in small slices so stop() stays responsive."""
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(0.05)

    def _poll_bms(self) -> None:
        """
        Send the JBD query frames so the BMS will answer. Each query is the
        wanted ID carrying a single 0x5A byte; the BMS replies on the same
        ID and those replies are decoded by the normal read loop.

        BMS_POLL_CHANNELS lists the channels that actually carry a BMS — one
        pack per wire on this car. EVERY one of them has to be polled: JBD is
        master/slave, so a channel left out here is a pack that answers
        nothing at all, silently. That is precisely what limited the pit to 12
        cells when only can0 was polled.

        If unset (None), every open bus is polled. The old bus-off worry that
        once restricted this to a single channel does not apply here — see the
        note in config.py: any node on a bus acks a valid frame, not just the
        addressee, and both wires carry other live devices.
        """
        if not self._buses:
            return
        target_buses = [
            (bus, lbl) for bus, lbl in self._buses
            if BMS_POLL_CHANNELS is None
            or getattr(bus, "channel", None) in BMS_POLL_CHANNELS
        ]
        for query_id in BMS_POLL_IDS:
            if not self._running:
                break
            msg = can.Message(
                arbitration_id=query_id,
                data=[BMS_POLL_BYTE],
                is_extended_id=False,
            )
            for bus, _ in target_buses:
                try:
                    # timeout>0 makes SocketCAN wait briefly for TX-buffer space
                    # instead of failing instantly when the queue is momentarily
                    # full.
                    bus.send(msg, timeout=0.05)
                except can.CanError as exc:
                    # ENOBUFS / TX timeout: frames aren't being ACKed, so the
                    # queue never drains. Almost always a bus issue — BMS not
                    # powered/on the bus, wrong bitrate, or missing 120Ω
                    # termination. Warn only occasionally so we don't flood the
                    # console.
                    self._poll_fail_count += 1
                    if self._poll_fail_count % 20 == 1:
                        print(f"⚠️ BMS poll TX failed @ {hex(query_id)}: {exc} — "
                              "check the BMS is powered & on the bus, the bitrate "
                              "matches, and the bus is terminated (2× 120Ω).")
                else:
                    self._poll_fail_count = 0

    def _request_gpio_report(self) -> None:
        """Ask the motor controller to start reporting the throttle GPIO.

        The ESC does not broadcast its GPIO inputs. One 8-byte frame to 0x147
        tells it which inputs to sample, how fast, and which reply bank to use;
        the replies then arrive on 0x150 and are decoded by the normal read
        loop like any other frame (see mms_parser's GPIO section).

        WHY THIS REPEATS instead of being sent once on connect: the report
        configuration lives in the controller's RAM, so it is lost on any ESC
        power cycle — and the ESC can be power-cycled without the Pi noticing
        (the Pi runs off its own supply). A one-shot request would mean the
        throttle trace dies at the first time the car is switched off in the
        pit lane and never comes back until someone restarts the software.

        RESTRICTED TO ONE CHANNEL, exactly like _poll_bms. A request sent down
        the wrong wire is never answered, and unanswered transmit retries are
        what walked can0 to bus-off on this car. THROTTLE_GPIO_CHANNEL is can1,
        the MMS's wire.
        """
        if not self._buses:
            return
        target_buses = [
            (bus, lbl) for bus, lbl in self._buses
            if THROTTLE_GPIO_CHANNEL is None
            or getattr(bus, "channel", None) == THROTTLE_GPIO_CHANNEL
        ]
        if not target_buses:
            return

        start_id = (mms_parser.THROTTLE_INPUT_ID if THROTTLE_GPIO_START_ID is None
                    else THROTTLE_GPIO_START_ID)
        end_id = start_id if THROTTLE_GPIO_END_ID is None else THROTTLE_GPIO_END_ID
        try:
            payload = mms_parser.build_gpio_request(
                bank=THROTTLE_GPIO_BANK,
                start_id=start_id,
                end_id=end_id,
                period_ms=THROTTLE_GPIO_PERIOD_MS,
                delay_ms=THROTTLE_GPIO_DELAY_MS,
                address=THROTTLE_GPIO_ADDRESS,
            )
        except ValueError as exc:
            # A misconfiguration in config.py. Say so once and stop trying,
            # rather than putting a malformed frame on the traction bus 12
            # times a minute for the rest of the race.
            if not self._gpio_fail_count:
                print(f"⚠️ Throttle GPIO request is misconfigured, not sending: "
                      f"{exc} — check config.THROTTLE_GPIO_*.")
            self._gpio_fail_count += 1
            return

        msg = can.Message(
            arbitration_id=mms_parser.GPIO_REQUEST_ID,
            data=payload,
            is_extended_id=False,
        )
        for bus, _ in target_buses:
            try:
                bus.send(msg, timeout=0.05)
            except can.CanError as exc:
                self._gpio_fail_count += 1
                # Same throttled reporting as the BMS poll: a failing TX means
                # frames are not being ACKed, which floods the console if every
                # attempt is logged.
                if self._gpio_fail_count % 20 == 1:
                    print(f"⚠️ Throttle GPIO request TX failed on "
                          f"{THROTTLE_GPIO_CHANNEL}: {exc} — check the MMS is "
                          "on this wire, the bitrate matches, and the bus is "
                          "terminated (2× 120Ω).")
            else:
                self._gpio_fail_count = 0
                if not self._gpio_requested_ok:
                    self._gpio_requested_ok = True
                    print(f"🦶 Throttle report armed: GPIO input "
                          f"{start_id:#x}..{end_id:#x} -> bank "
                          f"{THROTTLE_GPIO_BANK} (0x"
                          f"{mms_parser.GPIO_REPORT_IDS[THROTTLE_GPIO_BANK]:X}) "
                          f"every {THROTTLE_GPIO_PERIOD_MS} ms.")

    # ------------------------------------------------------------------ #
    # GPS (gpsd) → vehicle_state → Firebase → pit                         #
    # ------------------------------------------------------------------ #
    def _refresh_gps(self) -> None:
        """Copy the newest gpsd fix into vehicle_state["gps"].

        The pit reads car_data.gps.lat / .lon (Pit_Dashboard/db.py), so writing
        those two keys here is the whole handoff. Extra keys (speed, altitude,
        satellites, fix age) ride along and are kept in the pit's raw_json.

        With no fix, the gps dict is left EMPTY rather than filled with zeros —
        0,0 is a real place in the Atlantic, and the pit must be able to tell
        "no position" from "position". Empty means lat/lon land as NULL.
        """
        fix = self.gps.get_coordinates()
        if fix:
            self.vehicle_state["gps"] = fix

    def _sample_lap_gps(self) -> None:
        """Feed GPS to the lap trigger at 10 Hz.

        Deliberately NOT folded into _publish_gps: that is throttled to 0.5 s to
        match the Firebase write rate, which is the wrong cadence for crossing
        detection. Sampling faster guarantees every gpsd report is seen, so the
        segments the swept-capsule test builds stay as short as gpsd allows.
        Duplicate polls are free — LapTracker discards a fix it has already
        seen — and get_coordinates() is just a brief lock plus a dict copy.
        """
        now = time.monotonic()
        if now - self._last_lap_gps_sample < LAP_GPS_SAMPLE_INTERVAL_S:
            return
        self._last_lap_gps_sample = now

        event = self.laps.update_gps(self.gps.get_coordinates(), now)
        if event == "lap":
            self._print_lap()
        elif event == "start":
            print("🏁 Finish line acquired — lap timing armed.")
        # "resync" prints its own line from LapTracker, with the distance.
        self.vehicle_state["motor"].update(self.laps.snapshot())

    def _print_lap(self) -> None:
        """One console line per counted lap. Any figure may be unknown — a lap
        timed across a reboot has no time, one cut with the CAN bus dead has no
        energy — and a lap must never fail to print because of it."""
        laps = self.laps

        def fmt(value, spec, unit):
            return "—" if value is None else f"{value:{spec}}{unit}"

        print(f"🏁 LAP {laps.lap_count} [{laps.last_lap_kind}] — "
              f"{fmt(laps.last_lap_time_s, '.1f', ' s')}, "
              f"{fmt(laps.last_lap_energy_wh, '.1f', ' Wh')}, "
              f"{fmt(laps.last_lap_distance_m, '.0f', ' m')} "
              f"({laps.lap_source}"
              f"{'; ' + ', '.join(laps.last_lap_flags) if laps.last_lap_flags else ''})")

    def _poll_vehicle_inputs(self) -> None:
        """Refresh the GPIO-sourced indicators, independently of CAN.

        The brake and lights switches are wired to the Pi, so their state must
        keep updating when the motor controller is off or the bus is down —
        which is exactly when someone is most likely to be walking around the
        car pulling the parking brake.
        """
        now = time.monotonic()
        if now - self._last_input_poll < VEHICLE_INPUT_POLL_S:
            return
        self._last_input_poll = now
        # No frame argument: keep the CAN-derived flags and refresh only the
        # GPIO ones. _emit_vehicle_flags emits only when something changed.
        self._emit_vehicle_flags(None)

    def _tick_profile(self) -> None:
        """Target speed for the driver, from the active profile.

        Runs on the CAN worker thread beside everything else that reads
        LapTracker, so no locking is needed. The value comes from the SAME
        shared module the pit uses, so the target the driver is chasing is the
        target the strategist is judging them against.
        """
        now = time.monotonic()
        if now - self._last_profile_tick < PROFILE_TICK_S:
            return
        self._last_profile_tick = now

        profile = self.profiles.get(self.active_strategy)
        if profile is None:
            return

        # GPS lap position when it is fresh, the odometer's otherwise, and None
        # in the pit lane, where there is a speed limit and no target. None goes
        # to the HUD and to the pit as None: a target left over from the last
        # corner of the in-lap is worse than a dash.
        lap_distance = self.laps.profile_distance_m(now)
        target_kmh = (None if lap_distance is None
                      else float(profile.speed_kmh_at(lap_distance)))
        self.target_speed_updated.emit(target_kmh, self.active_strategy)
        self.vehicle_state["motor"]["target_speed_kmh"] = (
            None if target_kmh is None else round(target_kmh, 1))
        self.vehicle_state["motor"]["active_strategy"] = self.active_strategy

    def _publish_lap_timer(self) -> None:
        """Tell the HUD stopwatch when the current lap started, on change only.

        Every lap cut (GPS, distance fallback, the pit's Cut Lap) re-datums
        LapTracker._lap_start_ts, so watching that one value catches them all.
        The finished lap's time goes with it only when a lap was actually
        COUNTED: the first sighting of the line and a pit lap-number correction
        restart the clock with nothing to show. Same process and the same
        time.monotonic(), so the HUD can count up from the start itself.
        """
        start = self.laps.lap_start_ts
        if start == self._lap_timer_sent:
            return
        self._lap_timer_sent = start
        finished = (self.laps.last_lap_time_s
                    if start is not None
                    and self.laps.last_lap_finished_ts == start else None)
        self.lap_timer_updated.emit(start, finished)

    def _apply_strategy_commands(self) -> None:
        """Switch the active speed profile when the pit selects a new strategy.

        Applied on THIS thread; the Firebase listener only queues (see
        modules/lap_command.CommandInbox).
        """
        for cmd in self.strategy_inbox.drain():
            wanted = str(cmd.get("value") or "").strip()
            applied = wanted in self.profiles
            note = None
            if applied:
                self.active_strategy = wanted
                profile = self.profiles[wanted]
                print(f"🎯 STRATEGY -> {wanted} "
                      f"({profile.lap_time_s():.0f}s lap, "
                      f"{profile.average_kmh():.1f} km/h avg)")
                # Force the next tick so the new target shows immediately.
                self._last_profile_tick = 0.0
            else:
                note = (f"unknown strategy {wanted!r}; "
                        f"have {sorted(self.profiles)}")
                print(f"⚠️ {note}")
            ack_strategy(cmd.get("id"), self.active_strategy, applied, note)

    def _apply_lap_commands(self) -> None:
        """Apply queued pit commands ON THIS THREAD.

        The Firebase listener runs on a background thread and only queues; every
        mutation of the lap tracker happens here, on the CAN worker thread that
        owns it. That is what keeps LapTracker and vehicle_state lock-free.
        """
        for cmd in self.lap_inbox.drain():
            action = cmd.get("action")
            applied = True
            if action == "cut_lap":
                self.laps.force_lap("manual")
                print(f"🏁 PIT CUT LAP -> lap {self.laps.lap_count}")
            elif action == "set_lap":
                # A command with no number is refused, not read as 0: `or 0`
                # here once meant a malformed message could zero the race.
                value = cmd.get("value")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    self.laps.set_lap(value)
                    print(f"🏁 PIT SET LAP -> {self.laps.lap_count}")
                else:
                    applied = False
                    print(f"⚠️ PIT SET LAP ignored: no lap number in {cmd!r}")
            elif action == "restart_lap":
                self.laps.restart_lap()
                print("🏁 PIT RESTART LAP — nothing counted, looking for the line")
            elif action == "reset_energy":
                self.laps.reset_energy()
                print("🏁 PIT RESET ENERGY")
            elif action == "reset_trip":
                self.laps.reset_trip()
                print("🏁 PIT RESET TRIP")
            else:
                applied = False
            self.vehicle_state["motor"].update(self.laps.snapshot())
            ack_lap_command(cmd.get("id"), action, applied,
                            lap=self.laps.lap_count)
            if applied:
                # A pit command is a deliberate, infrequent edit to state that
                # a reboot must not silently undo -- don't make it wait for the
                # next throttled tick (up to LAP_CHECKPOINT_INTERVAL_S away).
                self._save_lap_checkpoint(force=True)

    def _load_lap_checkpoint(self) -> None:
        """Restore LapTracker's running totals from disk, if a checkpoint exists.

        Called from __init__, before the worker thread starts -- so this needs
        no locking even though LapTracker is otherwise single-thread-owned by
        the CAN worker (see lap_tracker.py's docstring): nothing else can be
        touching self.laps yet.

        Best-effort, matching restore()'s own contract: a missing or corrupt
        checkpoint must never stop the car's telemetry from starting. restore()
        already tolerates a malformed dict; this only has to handle the file
        not existing or not parsing as JSON at all.
        """
        try:
            with open(LAP_CHECKPOINT_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception as exc:
            print(f"⚠️ lap checkpoint unreadable, starting from zero: {exc}")
            return
        if self.laps.restore(data):
            age_s = time.time() - (data.get("saved_at") or time.time())
            print(f"🔢 resumed from checkpoint: odometer {self.laps.odometer_m:.0f} m, "
                  f"lap {self.laps.lap_count}, saved {age_s:.0f}s ago")

    def _save_lap_checkpoint(self, force: bool = False) -> None:
        """Persist LapTracker's running totals, throttled to
        LAP_CHECKPOINT_INTERVAL_S. `force=True` (used by _teardown, on a clean
        quit) bypasses the throttle so the very latest state is captured.

        Runs on the CAN worker thread, same as _publish_gps -- LapTracker is
        single-thread-owned, so reading it via state_dict() here is safe by
        the same reasoning as every other place that touches self.laps.

        Writes to a temp file and os.replace()s over the real one, so a power
        cut mid-write (a real risk on a Pi with no UPS) can never leave a
        torn/corrupt checkpoint behind. restore() already tolerates a missing
        OR corrupt file either way, but avoiding the corrupt case outright
        costs nothing.
        """
        now = time.time()
        if not force and now - self._last_checkpoint_save < LAP_CHECKPOINT_INTERVAL_S:
            return
        self._last_checkpoint_save = now
        tmp_path = LAP_CHECKPOINT_PATH + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.laps.state_dict(), f)
            os.replace(tmp_path, LAP_CHECKPOINT_PATH)
        except Exception as exc:
            print(f"⚠️ failed to save lap checkpoint: {exc}")

    def _tick_cell_extremes(self) -> None:
        """Emit the rule 3.5.6 report once a second and save it once a minute.

        Deliberately NOT blanked by _emit_zeros: a quiet bus ends the flow of
        new readings, it does not make the extremes of the last 2 hours
        untrue. They age out of the window on their own.
        """
        now = time.monotonic()
        if now - self._last_extremes_emit >= CELL_EXTREMES_EMIT_S:
            self._last_extremes_emit = now
            self.cell_extremes_updated.emit(self.cell_extremes.result())
        if now - self._last_extremes_save >= CELL_EXTREMES_SAVE_S:
            self._last_extremes_save = now
            try:
                self.cell_extremes.save(CELL_EXTREMES_PATH)
            except Exception as exc:
                print(f"⚠️ failed to save cell extremes: {exc}")

    def _publish_gps(self) -> None:
        """Refresh GPS and push telemetry on a timer, independent of CAN.

        Runs on the CAN worker thread — the SAME thread that pushes from
        _decode_message — so vehicle_state is never serialised by one thread
        while another mutates it. _decode_message needs no GPS call of its own:
        this runs every pass of the read loop, so the gps dict it publishes is
        already fresh, and we avoid touching a lock on every CAN frame.
        """
        now = time.time()
        if now - self._last_gps_publish < GPS_PUBLISH_INTERVAL_S:
            return
        self._last_gps_publish = now
        self._refresh_gps()

        # A charging stop just began: re-datum "current stint" to start
        # counting from here. Does NOT touch total_race_energy/regen_energy —
        # see LapTracker.mark_stint_start().
        if self._charge_detector.update(self._last_bms_current_A,
                                        self._last_mms_rpm):
            self.laps.mark_stint_start()
            print("🔌 CHARGING DETECTED — stint reset")

        # Log GPS state only when the summary changes, so the console shows the
        # moment a fix is acquired or lost without scrolling every second.
        # log_line(), not status(): status() carries the fix age and report
        # count, which change on every pass — logging it "only when it changes"
        # would print twice a second forever. The state transitions are the
        # thing worth a line; the numbers live in the health payload below.
        status = self.gps.log_line()
        if status != self._last_gps_log:
            self._last_gps_log = status
            print(f"🛰️ {status}")

        # And the hardware behind it, on the same change-only terms: a receiver
        # plugged in mid-race, or a device gpsd has just lost, shows up here.
        hardware = self.gps.hardware()
        if hardware != self._last_gps_hw_log:
            self._last_gps_hw_log = hardware
            print(f"🛰️ {hardware}")

        # Only push when we actually have a position. Without this guard a Pi
        # left powered with the car off would write an all-empty payload to
        # Firebase twice a second forever, growing telemetry_history for nothing.
        # With no GPS the behaviour is exactly as before: _decode_message is the
        # only publisher, so nothing is sent while the bus is quiet.
        if self.vehicle_state["gps"]:
            self.vehicle_state["health"] = self._health_snapshot()
            push_telemetry_to_cloud(self.vehicle_state)

    # ------------------------------------------------------------------ #
    # Health — what the pit needs to tell a dead bus from a dead Pi          #
    # ------------------------------------------------------------------ #
    def _health_snapshot(self) -> dict:
        """Small, always-computable block describing the Pi itself.

        Never raises and never depends on CAN or GPS having worked, because the
        entire point of it is to survive both of them failing.

        `can_detail` is built HERE rather than in the pit because the channel
        names live here: socketcan gives can0/can1, a USB adapter gives
        something else entirely, and the pit should not have to guess. It names
        only channels that are actually quiet, so a healthy car sends an empty
        string and the pit shows nothing.
        """
        now = time.time()
        try:
            open_names = [getattr(bus, "channel", None) or lbl
                          for bus, lbl in self._buses]
        except Exception:
            open_names = []

        ages = {}
        for nm in open_names:
            last = self._last_frame_by_channel.get(nm)
            ages[nm] = (now - last) if last else None

        # Silence across the WHOLE car: the freshest channel wins, because one
        # live bus means the car is still talking to us.
        seen = [a for a in ages.values() if a is not None]
        can_silent_s = min(seen) if seen else None

        quiet = []
        for nm, age in sorted(ages.items(), key=lambda kv: str(kv[0])):
            if age is None:
                quiet.append(f"{nm} no frames yet")
            elif age > CHANNEL_QUIET_AFTER_S:
                quiet.append(f"{nm} silent {age:.0f}s")

        if not open_names:
            detail = "no CAN bus open"
        else:
            detail = ", ".join(quiet)

        gps_fix = 1 if self.vehicle_state.get("gps") else 0
        try:
            gps_detail = self.gps.status()
        except Exception:
            gps_detail = None

        return {
            "pi_uptime_s": round(now - self._boot_ts, 1),
            "can_state": self._can_state,
            "can_silent_s": (round(can_silent_s, 1)
                             if can_silent_s is not None else None),
            "can_detail": detail,
            "can_frames": sum(self._frames_by_channel.values()) or 0,
            "gps_fix": gps_fix,
            "gps_detail": gps_detail,
        }

    def _publish_heartbeat(self) -> None:
        """Push telemetry regardless of CAN and GPS. See HEARTBEAT_INTERVAL_S."""
        try:
            self.vehicle_state["health"] = self._health_snapshot()
            push_telemetry_to_cloud(self.vehicle_state)
        except Exception as exc:
            # A heartbeat that can crash the read loop is worse than no
            # heartbeat: it would take the car's telemetry down with it.
            print(f"[Heartbeat] {type(exc).__name__}: {exc}")

    def _shutdown_bus(self) -> None:
        """Release every open CAN interface (overrides the single-bus base)."""
        for bus, _ in self._buses:
            try:
                bus.shutdown()
            except Exception:
                pass  # best effort; we're tearing down regardless
        self._buses = []
        # A fresh reopen should be free to search for a USB adapter again.
        self._usb_bus_active = False

    def _pack_name(self, channel) -> str:
        """'A', 'B', ... for the pack on `channel`, ordered by its cell offset.

        Derived from BMS_CELL_OFFSETS rather than hard-coded, so the letter a
        fault is labelled with always matches the cell range that pack owns
        (C_A*/C_B* on the temperature screen, Modules 1-13/14-26 on DS004)."""
        offsets = BMS_CELL_OFFSETS or {}
        if not offsets:
            return "A"
        order = sorted(set(offsets.values()))
        try:
            return chr(ord("A") + order.index(offsets.get(channel, 0)))
        except ValueError:
            return "A"

    def _remap_bms_frame(self, bms_data: dict, channel) -> dict:
        """Tag one decoded JBD frame with the pack it came from.

        Both BMS units answer on the SAME ids, so the arbitration id says
        nothing about which pack a reading belongs to — only the channel it
        arrived on does (see config.BMS_POLL_CHANNELS). Without this, the two
        packs write into identical keys and the last frame to arrive wins,
        roughly 2x a second.

        Three different things happen to three kinds of field:

        * CELL VOLTAGES are renumbered into the combined 1..26 space the pit
          and HUD display, by the channel's BMS_CELL_OFFSETS entry. can0's
          cell 1 stays cell 1 (module A); can1's cell 1 becomes cell 14
          (module B).
        * bms_string_count is SUMMED across packs, because everything
          downstream reads it as "how many cells does this car have" — and
          because the pit gates cell tiles on it, leaving it at one pack's 13
          would hide the other pack's cells even once they arrive.
        * EVERY OTHER FIELD (voltage, current, SoC, temps, protections) is
          per-pack. The primary channel keeps the plain names the whole
          codebase already reads; the secondary's are prefixed `bms2_` so the
          readings are still published and logged, but cannot overwrite the
          headline ones. Letting two packs alternate in one SoC field at 2 Hz
          would show the driver a gauge swinging between them.
        """
        offset = (BMS_CELL_OFFSETS or {}).get(channel, 0)
        is_primary = (BMS_PRIMARY_CHANNEL is None or channel is None
                      or channel == BMS_PRIMARY_CHANNEL)

        # How many cells this pack actually has, if it has told us yet. JBD
        # answers in whole 3-cell frames, so a 13-cell pack's last frame can
        # carry readings for cells 14 and 15 that do not exist. Unclamped,
        # those land in the NEXT pack's range and quietly overwrite real
        # readings from the other battery — a wrong number presented as a
        # measurement, which is the one outcome worth going out of the way to
        # prevent. Accept everything until the pack states its size.
        own_count = self._bms_string_counts.get(channel)

        out = {}
        for key, value in bms_data.items():
            if key.startswith("bms_cell_") and key.endswith("_V"):
                n = int(key[len("bms_cell_"):-len("_V")])
                if own_count is not None and n > own_count:
                    continue                      # cell this pack does not have
                if offset:
                    out[f"bms_cell_{n + offset:02d}_V"] = value
                else:
                    out[key] = value
            elif key == "bms_string_count":
                # Remember each pack's own count, publish the total.
                self._bms_string_counts[channel] = value
                out[key] = sum(self._bms_string_counts.values())
            elif is_primary:
                out[key] = value
            else:
                out[f"bms2_{key[len('bms_'):]}" if key.startswith("bms_")
                    else f"bms2_{key}"] = value
        return out

    def _decode_message(self, msg, channel=None) -> None:
        """
        Intercepts incoming CAN frames, pushes to Firebase,
        and then triggers the UI update signals.

        `channel` is the CAN channel the frame arrived on. It matters for the
        BMS and ONLY for the BMS: two packs answer on identical ids, so this
        is the one piece of information that tells them apart. Defaults to
        None so the base class and any other caller keep working unchanged.
        """
        # Health first, before any decoding can raise: the question this
        # answers is "is the bus alive", and a frame we failed to parse still
        # proves that it is.
        name = channel or "bus"
        self._last_frame_by_channel[name] = time.time()
        self._frames_by_channel[name] = self._frames_by_channel.get(name, 0) + 1

        data_bytes = bytes(msg.data)
        msg_id = msg.arbitration_id

        bms_data = parse_jbd_bms_message(msg_id, data_bytes)
        if bms_data:
            bms_data = self._remap_bms_frame(bms_data, channel)
            self.vehicle_state["battery"].update(bms_data)
            # DS004 — accumulate per-cell voltages the same way DS003
            # accumulates per-cell temperatures (3 cells land per frame,
            # across 10 CAN IDs); push a full snapshot + the BMS's own wired
            # count on every BMS frame, not just the ones carrying a cell.
            string_count = self.vehicle_state["battery"].get("bms_string_count")
            for _k, _v in bms_data.items():
                if _k.startswith("bms_cell_") and _k.endswith("_V"):
                    _cell = int(_k[len("bms_cell_"):-len("_V")])
                    self._cell_voltages_V[_cell] = _v
                    # Rule 3.5.6. Same string-count gate as DS004, plus a
                    # plausibility range, so an unwired tap's 0.000 V never
                    # becomes the "lowest cell voltage" handed to officials.
                    self.cell_extremes.add_volt(_cell, _v, string_count)
            self.cell_voltages_updated.emit(
                self.vehicle_state["battery"].get("bms_string_count"),
                dict(self._cell_voltages_V))
            # R3.5.6 screen: the BMS's own NTC probes (0x105), per pack. The
            # probe number is read off the key, so the primary pack's
            # bms_temp_N_C and the secondary's remapped bms2_temp_N_C land the
            # same way, under the pack letter the fault labels already use.
            probes = {int(k.split("_")[2]): v for k, v in bms_data.items()
                      if k.startswith(("bms_temp_", "bms2_temp_")) and k.endswith("_C")}
            if probes:
                self._bms_probe_C.setdefault(self._pack_name(channel), {}).update(probes)
                self.bms_probe_temps_updated.emit(
                    {pack: dict(t) for pack, t in self._bms_probe_C.items()})
            # Drive the driver HUD's SoC gauge from the REAL BMS (same value the
            # pit shows). The LYNX 0x618 SoC is only the controller's estimate
            # and is suppressed once we have a real reading (see _decode_battery).
            if "bms_soc_percent" in bms_data:
                self._have_bms_soc = True
                self.soc_updated.emit(int(round(bms_data["bms_soc_percent"])))
            # Surface BMS protection faults on the HUD alert bar too — not just
            # the pit. `bms_protections` is present only on the 0x102 frame and
            # is [] when nothing is active, so this also CLEARS them on recovery.
            # Prefixed "BMS" so the driver can tell them from MMS controller
            # alerts (both have an "overvoltage", for instance).
            # Real battery current for DS002 (the HUD otherwise derives it as
            # P/V, which is only an estimate and goes wrong at low voltage).
            if "bms_current_A" in bms_data:
                self.battery_current_updated.emit(float(bms_data["bms_current_A"]))
                self._last_bms_current_A = float(bms_data["bms_current_A"])
            # Faults are tracked PER PACK. After _remap_bms_frame the second
            # pack's key is bms2_protections, so testing only the plain name
            # would have made every fault on pack B invisible to the driver —
            # the one class of BMS reading that must never be dropped.
            # Keyed by channel so one pack clearing its faults cannot wipe the
            # other's; the pack letter travels in the label so the driver knows
            # which battery to worry about.
            prot = bms_data.get("bms_protections")
            if prot is None:
                prot = bms_data.get("bms2_protections")
            if prot is not None:
                pack = self._pack_name(channel)
                self._bms_alerts_by_channel[channel] = [
                    (f"BMS {pack} {label}", "error") for label in prot]
                self._emit_alerts()

        # Battery-temperature controller (J1939) — pit/Firebase only,
        # intentionally NOT surfaced on the driver HUD.
        temp_data = parse_temp_controller_message(msg_id, data_bytes)
        if temp_data:
            # Summary only (low/high/avg). It does NOT drive battery temp:
            # that is the hottest per-sensor reading, below.
            self.vehicle_state["temp_controller"].update(temp_data)

        # DS003 — the per-sensor round-robin frame. Each one updates exactly
        # ONE cell's entry in self._cell_temps_C, which is why this dict is
        # allowed to persist across many frames (see the module's docstring)
        # rather than being rebuilt from scratch each time: a full 30-sensor
        # snapshot only exists once the module has cycled through all of them
        # at least once, same as bms_cell_NN_V accumulating out of the JBD
        # BMS's 3-cells-per-frame voltage messages.
        therm_data = parse_thermistor_general_message(msg_id, data_bytes)
        if therm_data:
            # Any frame at all -- fault or not -- proves this cell has been
            # loaded/enabled on the module; that is what "configured" means
            # (see the parser's docstring). Only the VALUE is untrustworthy
            # when the fault bit (Note #6) is set -- e.g. an open-circuit
            # sensor reporting a nonsense -41 C, seen in real captures on
            # this car's own cells 14-20 -- so that alone is what's skipped.
            # A persistently faulted cell simply never gets a value (stays
            # unreported, same as one never loaded at all) rather than
            # freezing on a number known to be wrong.
            self._thermistor_configured = True
            key = f"bms_cell_temp_{therm_data['cell_num']:02d}_C"
            if therm_data["fault"]:
                # A sensor that faults AFTER reporting must lose its last good
                # value, or a once-hot reading would stay the battery temp
                # (the pack maximum) for as long as the sensor stays broken.
                self._cell_temps_C.pop(therm_data["cell_num"], None)
                self.vehicle_state["temp_controller"].pop(key, None)
            else:
                self.vehicle_state["temp_controller"][key] = therm_data["value_C"]
                self._cell_temps_C[therm_data["cell_num"]] = therm_data["value_C"]
                # Rule 3.5.6 — the fault-bit skip above plus the shared
                # plausibility gate inside add_temp.
                self.cell_extremes.add_temp(therm_data["cell_num"], therm_data["value_C"])
            self.cell_temps_updated.emit(True, dict(self._cell_temps_C))
            # Battery temp, everywhere (MAX CELL gauges, pit, spectator page):
            # the hottest plausible cell. Published in vehicle_state under the
            # name the pit has always read.
            batt = limits.battery_temp_from_cells(self._cell_temps_C.values())
            self.vehicle_state["temp_controller"]["battery_temp_C"] = batt
            self.cell_temp_updated.emit(batt)

        mms_data = parse_mms_message(msg_id, data_bytes)
        if mms_data: 
            self.vehicle_state["motor"].update(mms_data)

            # Distance and energy are integrated ONLY from frames that actually
            # carry the value. The old code advanced a single shared timestamp
            # on every MMS frame — including status/battery/temperature frames,
            # which carry no RPM — so those intervals were consumed without
            # contributing any distance and the odometer read far low. Each
            # accumulator now owns its own clock inside LapTracker.
            if "mms_rpm" in mms_data:
                self.laps.update_motion(mms_data["mms_rpm"])
                self._last_mms_rpm = float(mms_data["mms_rpm"])
            if "mms_power_W" in mms_data:
                self.laps.update_energy(mms_data["mms_power_W"])
                # Negative power is the car slowing itself down, so the brake
                # light comes on. Driven from the same frame that feeds the
                # energy integrator, so the lamp can never disagree with the
                # regen figure the pit is reading.
                self.regen_light.update(mms_data["mms_power_W"])
            # The controller broadcasts its own TRIP counter (0x620). Prefer it
            # over our integration: it comes from the controller's configured
            # wheel size rather than our unmeasured tire constant, and being a
            # counter it cannot lose distance to a dropped frame.
            if "mms_trip_m" in mms_data:
                self.laps.update_odometer(mms_data["mms_trip_m"])

            # Merged last so the tracker's derived keys (odometer_m,
            # calculated_lap, energy, lap timing) always win over the parser's.
            self.vehicle_state["motor"].update(self.laps.snapshot())

        push_telemetry_to_cloud(self.vehicle_state)
        
        if time.time() - self.last_cloud_print > 1.0:
            print(f"☁️ Cloud Sync Payload: Battery {len(self.vehicle_state['battery'])} keys, Motor {len(self.vehicle_state['motor'])} keys")
            self.last_cloud_print = time.time()

        super()._decode_message(msg)

    def _decode_status(self, data: bytes) -> None:
        """Override the MMS status decoder so it CACHES the controller alerts and
        re-emits the combined (MMS + BMS) list, instead of overwriting the bar
        with MMS-only alerts the way the base CANWorker does."""
        # The base class emits the power map from here; this override does not
        # call super(), so it has to do it too or the pit-wall build would never
        # show a map at all.
        self._emit_motor_map(data)

        if len(data) < 8:
            return
        limit_word = struct.unpack_from("<H", data, 4)[0]
        error_word = struct.unpack_from("<H", data, 6)[0]
        self._mms_alerts = (
            _word_to_alerts(error_word, _ERROR_BITS, "error")
            + _word_to_alerts(limit_word, _LIMIT_BITS, "limit")
        )
        self._emit_alerts()

    def _emit_alerts(self) -> None:
        """Push the merged alert list to the HUD. Critical faults first — MMS
        errors and BMS protections — then MMS limits, capped at 3 so the bar
        never overflows (same cap the base worker used)."""
        mms_errors = [a for a in self._mms_alerts if a[1] == "error"]
        mms_limits = [a for a in self._mms_alerts if a[1] != "error"]
        # Both packs' faults, in a stable channel order so the bar does not
        # reshuffle between frames.
        bms_alerts = [a for ch in sorted(self._bms_alerts_by_channel,
                                         key=lambda c: (c is None, c))
                      for a in self._bms_alerts_by_channel[ch]]
        combined = mms_errors + bms_alerts + mms_limits
        self.alerts_updated.emit(combined[:3])

    def _decode_battery(self, data: bytes) -> None:
        """
        Override of the LYNX 0x618 decoder. The motor controller reports a rough
        SoC ESTIMATE here; we prefer the real JBD BMS SoC (emitted from
        _decode_message). So emit voltage as usual, but only use the LYNX SoC as
        a fallback until the first real BMS reading arrives.
        """
        if len(data) < 6:
            return
        voltage_raw = struct.unpack_from("<H", data, 4)[0]
        self.voltage_updated.emit(round(voltage_raw * 0.01, 2))
        if not self._have_bms_soc:
            soc = struct.unpack_from("<B", data, 2)[0]
            # Zero means "not populated", not "empty pack". This controller
            # never fills the field in: it reads exactly 0 in all 44,088
            # recorded samples, and a car that is driving cannot be at 0 % SoC
            # anyway. Emitting that 0 as a measurement would be the exact lie
            # the em dash exists to prevent.
            #
            # It matters more now than it used to. SoC has a LOW-side threshold
            # (limits.SOC), so a literal 0 would show a blinking red gauge from
            # the moment the car wakes up until the first BMS reply lands - and
            # forever if the BMS never answers, which is a failure this class
            # already handles elsewhere. None leaves an em dash instead, which
            # is the truth: we do not know the charge yet.
            self.soc_updated.emit(soc if soc > 0 else None)

# ==============================================================================
# APPLICATION BOOTSTRAP
# ==============================================================================
def bring_up_can_buses():
    """Bring up any CAN interface that isn't already up.

    Preferred setup is the `can-up.service` systemd unit (see deploy/), which
    runs as root at boot and has the buses live before this app starts. When
    that is in place this function finds everything already UP and does nothing.

    Why it checks first rather than always shelling out:

    * `ip link set canX up` FAILS on an interface that is already up
      ("Device or resource busy"), so the unconditional version printed errors
      on every launch and taught everyone to ignore the startup output.
    * Under the autostarted desktop session there is no terminal, so a `sudo`
      that actually needs a password fails with "no tty present" — noisy, and
      impossible to answer. Not attempting it is better than failing it.
    * `can1` usually does not exist on a single-HAT car; probing avoids trying
      to configure an interface that was never there.

    Reuses config.can_link_state(), which already reports UP / DOWN / ABSENT per
    channel via `ip link show`, and returns {} when `ip` is unavailable (a
    laptop), in which case we skip the whole thing.
    """
    states = can_link_state()
    if not states:
        print("ℹ️ No `ip` command here — skipping CAN bring-up (not a Pi?).")
        return

    for channel, state in states.items():
        if state == "UP":
            print(f"✅ {channel} already up (systemd unit or a previous run).")
            continue
        if state == "ABSENT":
            print(f"ℹ️ {channel} does not exist on this machine — skipping.")
            continue

        # Per-channel: can0 and can1 are independent controllers and the car
        # runs them at different rates. Using one global bitrate here is how a
        # channel ends up silently misconfigured — it opens fine and simply
        # never decodes a frame, which looks identical to unplugged wiring.
        rate = bitrate_for(channel)
        print(f"⚙️ {channel} is down — bringing it up at "
              f"{rate // 1000} kbit/s...")
        for cmd in (
            # restart-ms 100 arms automatic bus-off recovery. Without it a
            # controller that hits 256 TX errors stays off the bus for good,
            # keeping the UP flag while carrying nothing - see can-up.service.
            f"sudo -n ip link set {channel} up type can bitrate {rate} restart-ms 100",
            f"sudo -n ip link set {channel} txqueuelen 65536",
        ):
            # -n = never prompt. Without a tty a prompt cannot be answered, so
            # failing immediately with a clear message beats hanging the boot.
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode != 0:
                err = (result.stderr or "").strip().splitlines()
                print(f"⚠️ Could not configure {channel}: "
                      f"{err[-1] if err else 'unknown error'}")
                print("   Install the can-up.service unit (see deploy/README) so "
                      "the bus is brought up at boot as root — that is the fix, "
                      "not granting this app sudo.")
                break


def main():
    print("Starting Endurance Race Telemetry System v2.0...")

    # Timestamp python-can's own log records. Its "<Bus> was not properly shut
    # down" warning is emitted from BusABC.__del__, i.e. by the garbage
    # collector — so the ONLY way to tell whether it fires mid-race or during
    # interpreter teardown is to see the time next to it. Without this the
    # message lands bare in the log and says nothing about when.
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    # Proves which build the car is actually running. If this line is missing
    # from the log, the Pi is running an older copy of config.py and no amount
    # of local fixing will change what it does.
    print(f"🔖 CAN bus registry: {CAN_REGISTRY_BUILD}")

    bring_up_can_buses()
    db_url = "https://solar-race-telemetry-default-rtdb.europe-west1.firebasedatabase.app/"
    try:
        initialize_firebase("SolarRace_OS/cloud/serviceAccountKey.json", db_url)
    except Exception as e:
        print(f"Firebase Init Error: {e}")

    app = QApplication(sys.argv)
    
    app.setStyleSheet(RACING_QSS)
    
    driver_dash_v2.CANWorker = SmartCANWorker
    
    dashboard = RacingDashboard()

    # Wayland gives a client no window positioning, but it CAN request
    # fullscreen on a specific output — QWindow.setScreen() before going
    # fullscreen. windowHandle() is None until the native window exists, so
    # winId() is called first to force its creation. HUD_SCREEN_NAME is None
    # unless SOLARRACE_HUD_SCREEN is set (see config.py), in which case this
    # block is a no-op and behaviour is exactly what it was before screen
    # targeting existed — a panel that's unplugged or renamed must never stop
    # the HUD from starting.
    if HUD_SCREEN_NAME:
        dashboard.winId()
        handle = dashboard.windowHandle()
        available = QGuiApplication.screens()
        target = next((s for s in available if s.name() == HUD_SCREEN_NAME), None)
        if handle is not None and target is not None:
            handle.setScreen(target)
            print(f"🖥️ HUD targeting screen '{HUD_SCREEN_NAME}'")
        else:
            logging.warning(
                "SOLARRACE_HUD_SCREEN=%r not applied (windowHandle=%s, "
                "available screens=%s) — falling back to compositor default.",
                HUD_SCREEN_NAME, handle is not None, [s.name() for s in available],
            )
    dashboard.showFullScreen()

    # --- Pit-to-driver messages: subscribe to /driver_command (push, not poll) --
    # The listener callback runs on a firebase-admin background thread, so it only
    # emits a Qt signal; the connected slot updates the HUD on the GUI thread.
    class _CmdBridge(QObject):
        received = Signal(object)
    _bridge = _CmdBridge()
    _bridge.received.connect(dashboard.set_pit_message)
    try:
        dashboard._cmd_reg = listen_driver_command(
            lambda e: _bridge.received.emit(e.data if getattr(e, "path", "/") == "/" else None))
        print("Listening for pit driver commands...")
    except Exception as cmd_err:
        # HUD still runs without it (e.g. a Windows demo with no Firebase creds).
        print(f"Driver-command listener unavailable: {cmd_err}")

    print("Auto-starting CAN Bus...")
    dashboard._start_can()

    # ---- Clean shutdown on SIGTERM/SIGINT ------------------------------- #
    # closeEvent() already stops the CAN worker (and so calls bus.shutdown())
    # when a person closes the window — but start_hud.sh restarts the HUD on
    # ANY non-42 exit, including a `kill`/systemd stop/session logout, which
    # deliver SIGTERM and previously bypassed closeEvent entirely. With the
    # worker thread just torn down alongside the process, bus.shutdown() was
    # never called, and the next launch's PCAN open reported "not properly
    # shut down" — a stale handle left over from the ungraceful exit, not
    # from anything wrong in the current run. Catching the signal lets us
    # stop the worker (and the bus) before the process actually dies.
    def _handle_shutdown_signal(signum, _frame):
        # _fast_exit() bounds the wait on the CAN worker and then exits the
        # process outright. The old path called _stop_can() (an unbounded join
        # across blocking Firebase calls) and then app.quit(), which left the
        # interpreter waiting on the Firebase listener threads on top of that —
        # so deploy/stop_hud.sh regularly hit its grace period and had to
        # SIGKILL, which is precisely the ungraceful exit this handler exists
        # to avoid. Exit code stays 0: a bare `kill` still means "restart" to
        # start_hud.sh, and stop_hud.sh writes the stop file to say otherwise.
        dashboard._fast_exit(0, f"received signal {signum}")

    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    # Stop the worker on EVERY Qt exit route. closeEvent and _quit_for_good
    # each call _stop_can() themselves, but aboutToQuit is the only hook that
    # also covers routes neither of them sees (app.quit() from anywhere, the
    # session manager ending the app, last-window-closed). _stop_can() is
    # idempotent, so the overlap is harmless — and without this, a quit that
    # skipped both left the CAN worker thread running with its buses open.
    app.aboutToQuit.connect(dashboard._stop_can)

    # Qt's C++ event loop can otherwise delay Python signal delivery
    # indefinitely; a periodic no-op timer gives the interpreter a regular
    # chance to run the handler above promptly.
    _signal_pump = QTimer()
    _signal_pump.timeout.connect(lambda: None)
    _signal_pump.start(200)

    sys.exit(app.exec())

if __name__ == "__main__":
    main()