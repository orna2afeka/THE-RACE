"""
can_worker.py — CAN Bus Background Thread
==========================================
Owns all hardware I/O with the car's CAN bus. The connection itself
(USB-to-CAN adapter *or* the Pi's CAN HAT) is described in config.py —
this thread just tries each listed connection in order and uses the
first that opens. Runs entirely inside a QThread so the GUI thread is
never blocked.

Decoded parameters (LYNX / SiliXcon protocol):
  CAN ID 0x600  →  Status / Limits / Errors
                   Limit Word (bytes 4-5, uint16 LE bitmask)
                   Error Word (bytes 6-7, uint16 LE bitmask)
  CAN ID 0x610  →  Motor RPM        (bytes 2-3, int16 LE)
                   Motor Power      (bytes 6-7, uint16 LE, Watts)
  CAN ID 0x618  →  Battery Voltage  (bytes 4-5, uint16 LE × 0.01 V)
                   Battery SOC      (byte 2,    uint8  %)
  CAN ID 0x628  →  Controller Temp  (byte 4,    uint8  °C)
                   Motor Thermistor (bytes 0-1,  uint16 LE, raw ADC)
"""

import struct
from typing import Optional

import can
from PySide6.QtCore import QThread, Signal

from config import open_bus
from modules.mms_parser import (
    TEMP_FRAME_IDS, parse_driver_state, parse_motor_map, parse_motor_temp_frame,
    reverse_active)


# ── SiliXcon LYNX — Status Word bit definitions ──────────────────────────── #
# Each entry: (bit_index, human_readable_label)
# Ordered most-critical-first within each word so the slice [:3] keeps the
# highest-priority alerts when more than 3 flags are set simultaneously.

# Limit Word (bytes 4-5 of ID 0x600)
_LIMIT_BITS: list[tuple[int, str]] = [
    (0,  "Over-voltage Limit"),
    (1,  "Under-voltage Limit"),
    (2,  "Controller Thermal Limit"),
    (3,  "Motor Thermal Limit"),
    (4,  "Over-current Limit"),
    (5,  "Speed Limit"),
    (6,  "Regenerative Limit"),
    (7,  "Power Limit"),
    (8,  "Motor Stall Limit"),
    (9,  "Throttle Fault Limit"),
]

# Error Word (bytes 6-7 of ID 0x600) — errors are more critical than limits
_ERROR_BITS: list[tuple[int, str]] = [
    (0,  "Over-voltage Error"),
    (1,  "Under-voltage Error"),
    (2,  "Controller Over-temp"),
    (3,  "Motor Over-temp"),
    (4,  "Over-current Fault"),
    (5,  "Hall Sensor Fault"),
    (6,  "Communication Fault"),
    (7,  "Hardware Fault"),
    (8,  "Throttle Error"),
    (9,  "Phase Imbalance"),
]


def _word_to_alerts(word: int, bit_defs: list[tuple[int, str]], severity: str) -> list[tuple[str, str]]:
    """Return a list of (label, severity) tuples for each set bit in *word*."""
    return [
        (label, severity)
        for bit, label in bit_defs
        if word & (1 << bit)
    ]


class CANWorker(QThread):
    """
    Background QThread that reads the CAN bus in a tight loop,
    decodes LYNX protocol frames, and emits Qt Signals.

    Signal → Slot connections are automatically queued across thread
    boundaries by Qt, so all slot handlers run safely in the GUI thread.
    """

    # ------------------------------------------------------------------ #
    # Public signals — connect these to GUI slots                         #
    # ------------------------------------------------------------------ #
    rpm_updated             = Signal(int)    # Motor RPM           (signed RPM)
    voltage_updated         = Signal(float)  # Battery voltage     (Volts)
    soc_updated             = Signal(int)    # State of Charge     (0-100 %)
    controller_temp_updated = Signal(int)    # Controller temp     (°C)
    # Motor PT1000 sensor, sent as one atomic reading:
    #   (resistance Ω, temperature °C or -1000.0 if unconvertible, status)
    motor_temp_updated      = Signal(float, float, str)
    power_updated           = Signal(int)    # Instant power       (Watts)
    alerts_updated          = Signal(list)   # Active alerts: [(label, severity), ...]
    # Active power map: (display name, raw byte). Raw travels with the name so
    # an unrecognised value can be identified instead of just looking wrong.
    motor_map_updated       = Signal(str, int)
    motor_current_updated   = Signal(float)  # Motor phase current (A, q axis)
    battery_current_updated = Signal(float)  # Battery current from the BMS (A)
    cell_temp_updated       = Signal(float)  # Hottest battery cell (°C)
    # Boolean dashboard indicators: parking_brake / lights_on / ecu_on / reverse
    # (+ the raw status byte they came from). One dict so the HUD repaints the
    # whole indicator row from a single consistent snapshot.
    vehicle_flags_updated   = Signal(dict)
    # Target speed from the active profile: (target km/h, strategy name).
    target_speed_updated    = Signal(float, str)
    # Upcoming corner: (metres ahead, max km/h there, speed drop km/h). All
    # zeros means "nothing ahead" and clears the alert.
    turn_alert_updated      = Signal(float, float, float)
    connection_error        = Signal(str)    # Fatal error message
    status_updated          = Signal(str)    # Human-readable status string

    # ------------------------------------------------------------------ #
    # LYNX protocol CAN arbitration IDs                                   #
    # ------------------------------------------------------------------ #
    ID_STATUS  = 0x600   # Status / Limit / Error words
    ID_MOTOR   = 0x610   # Motor RPM + Power
    ID_BATTERY = 0x618   # Battery voltage + SOC
    ID_TEMP    = 0x628   # Controller & motor temperatures

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running: bool = False
        self._bus: Optional[can.BusABC] = None
        # Last power map emitted, so the badge only repaints when it changes.
        # None (not 0) means "nothing seen yet" — 0 is a valid map value.
        self._last_motor_map: Optional[int] = None
        self._last_flags: dict = {}      # last indicator row emitted
        self._last_rpm: int = 0          # newest RPM, for reverse detection
        # Parking brake / lights come from the Pi's GPIO, not from CAN — the
        # motor controller cannot see them. Assigned by main.py; None here so
        # the standalone HUD still runs and simply reports them as unknown.
        self.vehicle_inputs = None

    # ================================================================== #
    # QThread entry point — executes in the worker thread                 #
    # ================================================================== #
    def run(self) -> None:
        """Open the shared CAN bus, then loop reading and decoding LYNX frames.

        The standalone driver HUD only decodes the MMS (LYNX) frames; the
        full pit-wall build (main.py / SmartCANWorker) decodes BMS + temp
        off the same bus and polls the BMS.
        """
        self._running = True

        # ---- 1. Open the shared CAN bus (CAN HAT or USB adapter) ------ #
        self._bus, label, err = open_bus()
        if self._bus is None:
            self.connection_error.emit(
                f"Cannot open CAN bus:\n{err}\n\n"
                "• Verify the USB-to-CAN adapter is plugged in, OR\n"
                "• Verify the CAN HAT is mounted and can0 is up\n"
                "    (sudo ip link set can0 up type can bitrate 500000).\n"
                "• Check CAN_CANDIDATES in config.py.\n"
                "• Ensure no other application is using the channel."
            )
            self._running = False
            return

        self.status_updated.emit(f"● CONNECTED  |  {label}")

        # ---- 2. Main read loop ---------------------------------------- #
        # try/finally, so the bus is released even if _decode_message raises
        # something that is not a CanError. Without it the thread would die
        # with the interface still open, and python-can's BusABC.__del__ would
        # later report "<Bus> was not properly shut down" at process exit.
        try:
            while self._running:
                try:
                    # recv() blocks for up to 1 s, returning None on timeout.
                    # The 1-second timeout lets the loop check self._running
                    # periodically so stop() can exit cleanly.
                    msg = self._bus.recv(timeout=1.0)
                    if msg is not None:
                        self._decode_message(msg)
                except can.CanError as exc:
                    self.connection_error.emit(f"CAN read error: {exc}")
                    break
        finally:
            # ---- 3. Teardown ------------------------------------------ #
            self._shutdown_bus()

    # ================================================================== #
    # LYNX protocol decoder                                               #
    # ================================================================== #
    def _decode_message(self, msg: can.Message) -> None:
        """Dispatch a raw CAN frame to the correct sub-decoder."""
        data = bytes(msg.data)
        arb_id = msg.arbitration_id

        if arb_id == self.ID_STATUS:
            self._decode_status(data)
        elif arb_id == self.ID_MOTOR:
            self._decode_motor(data)
        elif arb_id == self.ID_BATTERY:
            self._decode_battery(data)
        elif arb_id in TEMP_FRAME_IDS:
            self._decode_temp(data)
        # All other IDs are silently ignored — future extensibility here.

    def _decode_status(self, data: bytes) -> None:
        """
        CAN ID 0x600  — Status message (8 bytes expected)

          Bytes 4-5  : Limit Word  — uint16 LE bitmask
          Bytes 6-7  : Error Word  — uint16 LE bitmask

        Errors are treated as higher priority than limits and always
        appear first in the emitted alerts list.
        """
        self._emit_motor_map(data)
        self._emit_vehicle_flags(data)

        if len(data) < 8:
            return

        # '<H' = little-endian unsigned short (2 bytes)
        limit_word: int = struct.unpack_from("<H", data, 4)[0]
        error_word: int = struct.unpack_from("<H", data, 6)[0]

        # Errors prepend limits — both sorted most-critical-first by definition
        alerts = (
            _word_to_alerts(error_word, _ERROR_BITS, "error")
            + _word_to_alerts(limit_word, _LIMIT_BITS, "limit")
        )
        # Emit at most 3 most critical active alerts
        self.alerts_updated.emit(alerts[:3])

    def _emit_motor_map(self, data: bytes) -> None:
        """Push the active power map (byte 2 of the status frame) to the HUD.

        Split out of _decode_status because SmartCANWorker overrides that method
        to cache alerts and does NOT call super() — without a shared helper the
        pit-wall build would silently never show a map. Called from both.

        Emits only on change: the status frame repeats continuously, and the
        badge does not need repainting several times a second to say the same
        thing.
        """
        fields = parse_motor_map(data)
        if not fields:
            return
        raw = fields["mms_motor_map_raw"]
        if raw == self._last_motor_map:
            return
        self._last_motor_map = raw
        self.motor_map_updated.emit(fields["mms_motor_map"], raw)

    def _emit_vehicle_flags(self, data: bytes = None) -> None:
        """Push the boolean indicator row.

        Every value here is sourced from something we can actually justify:

          ecu_on        we are decoding a LYNX status frame right now, so the
                        controller is powered and talking. That IS what "ECU on"
                        means — and it needs no undocumented bit. It clears via
                        _emit_zeros when the bus goes quiet.
          reverse       negative motor RPM (a documented signed field), backed
                        up by the reverse power map.
          parking_brake read from the Pi's own GPIO — the switches are wired to
          lights_on     the Pi, not to the motor controller. None (not False)
                        whenever no pin is configured or readable, so the HUD
                        can show UNKNOWN instead of a confident "off".

        Nothing is taken from byte 3 any more: that is a protections word, so
        those bits are zero because nothing has faulted, not because the brake
        is released.
        """
        flags = {}

        if data is not None:
            # A status frame is in hand, so the controller is alive.
            flags["ecu_on"] = True
            map_fields = parse_motor_map(data)
            flags["reverse"] = reverse_active(
                map_raw=map_fields.get("mms_motor_map_raw"),
                rpm=self._last_rpm,
            )
            # Keep the raw protections word visible for diagnostics.
            flags.update(parse_driver_state(data))
        else:
            # Called from the input poller with no new CAN frame: keep whatever
            # the bus last told us and refresh only the GPIO-sourced values.
            flags = {k: v for k, v in self._last_flags.items()}

        if self.vehicle_inputs is not None:
            flags.update(self.vehicle_inputs.read())
        else:
            flags.setdefault("parking_brake", None)
            flags.setdefault("lights_on", None)

        if flags == self._last_flags:
            return          # status frame repeats constantly; only repaint on change
        self._last_flags = dict(flags)
        self.vehicle_flags_updated.emit(flags)

    def _decode_motor(self, data: bytes) -> None:
        """
        CAN ID 0x610  — Motor message (8 bytes expected)

          Bytes 0-1  : Motor current — int16 LE ("Motor current [A] (q axis)")
          Bytes 2-3  : Motor RPM    — int16 LE (signed, direct RPM)
          Bytes 6-7  : Motor Power  — int16 LE (signed Watts; negative on regen)
        """
        if len(data) < 8:
            return

        # '<h' = little-endian signed short (2 bytes), offset 2
        rpm: int = struct.unpack_from("<h", data, 2)[0]
        # Cached for reverse detection: a negative RPM is direct physical
        # evidence the car is going backwards (see _emit_vehicle_flags).
        self._last_rpm = rpm
        self.rpm_updated.emit(rpm)

        # Motor phase current — signed (negative on regen). Distinct from the
        # BATTERY current the BMS reports; DS002 shows both side by side.
        motor_current: int = struct.unpack_from("<h", data, 0)[0]
        self.motor_current_updated.emit(float(motor_current))

        # '<h' = little-endian SIGNED short (2 bytes), offset 6. Power is signed —
        # negative during regen/coasting. Decoding unsigned ("<H") wrapped small
        # negatives to ~65000 W (the bogus power spikes on the HUD/pit).
        power: int = struct.unpack_from("<h", data, 6)[0]
        self.power_updated.emit(power)

    def _decode_battery(self, data: bytes) -> None:
        """
        CAN ID 0x618  — Battery message (6+ bytes expected)

          Byte 2     : SOC              — uint8,  direct %
          Bytes 4-5  : Battery voltage  — uint16 LE, × 0.01 → Volts
        """
        if len(data) < 6:
            return

        soc: int = struct.unpack_from("<B", data, 2)[0]
        voltage_raw: int = struct.unpack_from("<H", data, 4)[0]
        voltage: float = round(voltage_raw * 0.01, 2)

        self.soc_updated.emit(soc)
        self.voltage_updated.emit(voltage)

    def _decode_temp(self, data: bytes) -> None:
        """
        Temperature message (0x630 + ADDR, or the legacy 0x628)

        Decoding lives in mms_parser.parse_motor_temp_frame so the HUD and the
        pit convert the identical bytes the identical way — the motor
        temperature must never differ between the two screens.

        Emits the raw resistance and the converted temperature TOGETHER in one
        signal. Sending them separately would let the HUD paint a °C from one
        frame beside an Ω from another, which is precisely the mismatch this
        validation display exists to rule out.
        """
        fields = parse_motor_temp_frame(data)
        if not fields:
            return

        if "mms_temperature_C" in fields:
            self.controller_temp_updated.emit(int(fields["mms_temperature_C"]))

        if "mms_motor_ohms" in fields:
            self.motor_temp_updated.emit(
                float(fields["mms_motor_ohms"]),
                # -1000.0 is the "did not convert" marker: Signal(float) cannot
                # carry None, and no real reading can reach it (the table stops
                # at -79 °C). The HUD shows the raw Ω plus the reason instead.
                float(fields.get("mms_motor_temp_C", -1000.0)),
                fields.get("mms_motor_temp_status", ""),
            )

    # ================================================================== #
    # Lifecycle helpers                                                    #
    # ================================================================== #
    def stop(self) -> None:
        """
        Ask the run loop to exit and block the caller until it does.
        Safe to call from the GUI thread.
        """
        self._running = False
        # wait() joins the OS thread — guaranteed clean exit before we return.
        self.wait()

    def _shutdown_bus(self) -> None:
        """Release the CAN hardware. Always called, even on error paths."""
        if self._bus is not None:
            try:
                self._bus.shutdown()
            except Exception:
                pass  # Best effort; we're shutting down regardless
            finally:
                self._bus = None
        self.status_updated.emit("● DISCONNECTED")
