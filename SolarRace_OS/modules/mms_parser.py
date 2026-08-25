import os
import struct
import sys

try:                        # normal import (package layout on the Pi)
    from modules import pt1000
except ImportError:         # when modules/ is itself on sys.path
    import pt1000

# drivetrain.py lives at the repo root, shared with the pit. Needed here for
# GEAR_RATIO alone — see decode_vehicle_speed_kmh() for why a speed field the
# controller already reports in km/h still has to be divided by the gear ratio.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import drivetrain           # noqa: E402
# Pedal calibration + the Eco/Normal/Power boundaries, shared with the pit wall
# so the driver's zone bar and the pit's throttle tile cannot disagree.
import efficiency           # noqa: E402

# ==============================================================================
# VEHICLE SPEED — 0x610 bytes 4-5
# ==============================================================================
# The raw field is NOT km/h, despite the LYNX docs labelling it "Vehicle speed
# [km/h]". Reading it as km/h is what put 2,569 rows over 200 km/h in
# telemetry.db and got the field barred from the Excel export.
#
# What it actually is, measured against two independent captures (377 frames in
# data/can_dump.txt and 3,923 moving rows in Pit_Dashboard/telemetry.db):
#
#   raw / motor_rpm  ->  median 1.036359   (p1 1.0072, p99 1.0400)
#
# That constant is not arbitrary. A speed in 0.1 km/h units, computed from a
# 1.7273 m wheel circumference with NO gear reduction, predicts exactly
# 1.7273 x 60 / 1000 x 10 = 1.03638. So the controller is:
#
#   * reporting in 0.1 km/h, not km/h            -> divide by 10
#   * using its own configured wheel size, which
#     independently implies 1.7273 m — within
#     0.03 % of drivetrain's 1.7276 m            -> nothing to do
#   * treating the motor as DIRECT DRIVE: its gear
#     ratio was never configured, so it is 1:1    -> divide by GEAR_RATIO
#
# Applying both corrections agrees with drivetrain.speed_kmh() to a mean of
# 0.014 km/h (worst case 0.147 km/h) over those 3,923 rows, and puts zero rows
# above 200 km/h.
#
# ⚠️ CONSEQUENCE, worth knowing before trusting this as a cross-check: because
# the controller's configured circumference happens to match drivetrain's
# placeholder, this field is numerically almost IDENTICAL to the RPM-derived
# speed. It does not escape the unmeasured-tire problem — it relocates it from
# TIRE_DIAMETER_METERS into the controller's own configuration. If you
# re-measure the tire and update drivetrain.py, reconfigure the controller too
# or the two numbers will start to disagree by exactly the correction you made.
#
# The right permanent fix is to configure the gear ratio in the controller; then
# GEAR_CORRECTION below becomes 1.0 and this comment becomes history. Verify
# with a re-capture before changing it — do not assume a firmware update did it.
# ==============================================================================
VEHICLE_SPEED_UNIT_KMH = 0.1        # raw counts -> km/h
VEHICLE_SPEED_GEAR_CORRECTED = True # the field still needs scaling; see below


def decode_vehicle_speed_kmh(raw):
    """0x610 bytes 4-5 (raw INT16) -> true road speed in km/h, or None.

    None, never 0.0, when there is nothing to decode: a missing speed and a
    stationary car are different facts and the dashboards render them
    differently.
    """
    if raw is None:
        return None
    kmh = abs(raw) * VEHICLE_SPEED_UNIT_KMH
    if VEHICLE_SPEED_GEAR_CORRECTED:
        # CONTROLLER_SPEED_DIVISOR, *not* GEAR_RATIO. These were the same
        # constant until 2026-08-22 and that coincidence hid a 2x RPM error for
        # two days: the speed here looked perfect while everything derived from
        # RPM was out by 2.57x. They are separate quantities - one is the belt,
        # the other is how this controller happens to scale a field it computes
        # from its own halved RPM. See drivetrain section 3b.
        kmh /= drivetrain.CONTROLLER_SPEED_DIVISOR
    return kmh


# ==============================================================================
# LYNX PROTOCOL CAN IDs (Silixcon MMS)
# ==============================================================================
ID_STATUS  = 0x600   # Status, mode, power map, protections, limits, errors
ID_MOTOR   = 0x610   # Motor current, RPM, VEHICLE SPEED (0.1 km/h), power
ID_BATTERY = 0x618   # Controller's estimation of Battery Voltage & SOC
ID_ODO     = 0x620   # TRIP (0.01 km) + ODO (0.1 km), broadcast at 1 Hz
ID_TEMP    = 0x628   # Motor thermistor resistance, PTC temp, controller temp

# ------------------------------------------------------------------------------
# Temperature frame — "Temperature data (0x630 + ADDR)" in the Silixcon FALCON
# ESC docs. ADDR is the controller's node address, added to every base ID so
# several controllers can share one bus, so the frame can land anywhere in
# 0x630..0x63F. We accept that whole span plus the 0x628 this code has always
# used, because the two disagree and only the car can settle it: keeping both
# means the decoder works before and after you confirm the controller's address.
#
# Documented payload (all little-endian):
#     bytes 0-1  INT16   motor temperature, ×0.01 °C, 0x7FFF = not available
#     bytes 2-3  INT16   PTC temperature — the PT1000 sensor channel
#     byte  4    UINT8   controller temperature, °C
#     byte  5    UINT8   disarm reason
#     bytes 6-7          reserved
# ------------------------------------------------------------------------------
ID_TEMP_BASE = 0x630
TEMP_FRAME_IDS = frozenset({ID_TEMP} | {ID_TEMP_BASE + addr for addr in range(16)})

# Where the RAW PT1000 RESISTANCE sits, and how to scale it to Ohms.
#
# The LYNX temperature frame (0x628), documented byte for byte:
#     bytes 0-1  UINT16  /driver/motor/RThermistor   0xFFFF = DISCONNECTED
#     bytes 2-3  UINT16  /driver/ptctemp
#     byte  4    UINT8   /driver/temp   (controller temperature)
#     byte  5    UINT8   /disarm_reason
#     bytes 6-7          reserved
#
# Bytes 0-1 are the MOTOR THERMISTOR RESISTANCE — the raw PT1000 reading this
# feature converts. An earlier version of this file took bytes 2-3 instead,
# because that layout came from Silixcon's FALCON documentation rather than
# LYNX; on FALCON bytes 0-1 are a pre-converted motor temperature and 2-3 are
# the PTC channel. This car runs LYNX, where it is the other way round, so the
# offset below is 0 and the original code in this repo had it right all along.
#
# It is UNSIGNED here (a resistance cannot be negative), and the documented
# "not connected" sentinel is 0xFFFF, not 0x7FFF.
PTC_OHMS_OFFSET = 0
PTC_OHMS_FORMAT = "<H"      # UINT16 little-endian, per the LYNX docs
PTC_OHMS_SCALE = 1.0        # raw count -> Ohms

# 0xFFFF on the thermistor channel means the probe is DISCONNECTED — the
# controller telling us the sensor is unplugged or the wire is broken, which is
# a fault worth surfacing rather than a temperature.
THERMISTOR_DISCONNECTED = 0xFFFF

# Sentinel used on the controller's own converted temperature fields.
TEMP_NOT_AVAILABLE = 0x7FFF

# ------------------------------------------------------------------------------
# Current power map — byte 2 of the LYNX status frame (0x600 + ADDR).
#
# Documented layout of that frame:
#     byte  0    UINT8   LYNX ID, always 10
#     byte  1    UINT8   LYNX mode
#     byte  2    UINT8   CURRENT POWER MAP   <- this feature
#     byte  3    UINT8   driver status word
#     bytes 4-5  UINT16  driver limit word
#     bytes 6-7  UINT16  driver error word
#
# ⚠️ PLACEHOLDER VALUES — the docs name the field but publish no integer-to-map
# table, so the numbers below are assumed, not confirmed. Fix them here and
# both dashboards follow; this dict is the only place map numbering lives.
#
# To find the real values: switch maps on the bench and watch `mms_motor_map_raw`
# (published raw, on purpose) on the pit dashboard or the driver HUD badge. The
# badge shows the raw number for anything unrecognised, so a wrong guess is
# visible immediately rather than silently mislabelling a map.
# ------------------------------------------------------------------------------
STATUS_MAP_BYTE = 2

# MEASURED against 46,836 recorded samples. What this controller actually sends:
#
#     raw  1  ->  22,985 samples   normal driving
#     raw 10  ->     260 samples   reverse
#     raw  3  ->      86 samples   reverse (an older configuration)
#     raw  2  ->      35 samples
#
# raw 0 has never once been seen, so the old note claiming "raw 0 = Map 1 ...
# raw 3 = Reverse" described a numbering the car does not use.
#
# Note there are TWO reverse values, which is why REVERSE_MAP_RAWS below exists
# and why nothing may test the reverse condition by comparing display names.
MOTOR_MAPS = {
    0: "Map 1",
    1: "NORMAL MODE",
    2: "ECO MODE-NOT CONFIG!",
    3: "Reverse",
    4: "Map 4",
    5: "Map 5",
    6: "Map 6",
    7: "Map 7",
    8: "Map 8",
    9: "Map 9",
    10: "REVERSE MODE",
}


# ------------------------------------------------------------------------------
# Driver status word — byte 3 of the status frame.
#
# THIS IS A PROTECTIONS / WARNING WORD, NOT VEHICLE STATE. The Silixcon docs
# describe it as a "warning indication" where a "non-zero value signifies that a
# protection was triggered", with each bit standing for one protection
# situation (the list of which is not published).
#
# It is therefore NOT a source for parking-brake or lights status, and an
# earlier version of this file was wrong to read those from it: those bits are
# zero because nothing has faulted, which would have shown the driver a
# confident "brake released / lights off" drawn from a completely unrelated
# field — and would have lit those indicators when some protection tripped.
#
# A motor controller has no inherent knowledge of a parking brake or headlights
# anyway; they are separate circuits. On this car those switches are being wired
# into the Raspberry Pi's GPIO instead — see modules/vehicle_inputs.py.
#
# The raw byte is still published as `mms_driver_state_raw` because it is
# genuinely useful diagnostically: a non-zero value means the controller is
# flagging a protection, and watching which bit sets identifies it.
# ------------------------------------------------------------------------------
STATUS_DRIVER_STATE_BYTE = 3


def parse_driver_state(data_bytes):
    """Byte 3 of the status frame -> the raw protections/warning word.

    Deliberately returns only the raw value and a "something is flagged" bool.
    No bit is decoded into a named vehicle state, because none of the bit
    meanings are documented and this field does not describe vehicle state.
    """
    if data_bytes is None or len(data_bytes) <= STATUS_DRIVER_STATE_BYTE:
        return {}
    raw = struct.unpack_from("<B", data_bytes, STATUS_DRIVER_STATE_BYTE)[0]
    return {
        "mms_driver_state_raw": raw,
        "mms_driver_protection_active": bool(raw),
    }


# Raw power-map values that mean reverse. Both are real: 3 is an older
# controller configuration and 10 is what the car sends now, and recorded
# telemetry contains both.
#
# ⚠️ MATCH ON THESE NUMBERS, NEVER ON THE DISPLAY NAME. This used to be
# `motor_map_name(map_raw) == "Reverse"`, which silently failed for raw 10
# because that renders as "REVERSE MODE" — so selecting reverse lit nothing,
# and the REV lamp only came on later via the negative-RPM path, i.e. once the
# car was already rolling backwards. The HUD badge had the same bug one layer
# up: `"Reverse" in name` is case-sensitive and does not match "REVERSE MODE".
#
# A name is for a human to read. Renaming a map must never be able to turn a
# safety indicator off.
REVERSE_MAP_RAWS = frozenset({3, 10})


def is_reverse_map(map_raw):
    """Is this raw power-map value one of the reverse maps?

    Tolerates None and a float (the pit reads these back out of SQLite as REAL)
    so callers do not each need their own guard.
    """
    if map_raw is None:
        return False
    try:
        return int(map_raw) in REVERSE_MAP_RAWS
    except (TypeError, ValueError):
        return False


def reverse_active(map_raw=None, rpm=None):
    """Is the car in reverse? Decided from real, documented signals only.

    Two independent signals, either one sufficient:

    * A negative motor RPM — bytes 2-3 of 0x610 are a documented signed INT16
      "Motor speed [rpm]", so a negative value means the motor is physically
      turning backwards. The strongest evidence available, needing no
      undocumented bit. But it is also LATE: it cannot be true until the car has
      actually started moving backwards.
    * The reverse power map — true the moment the driver selects it, which is
      when the warning is actually wanted. This is the half that was broken.
    """
    if rpm is not None and rpm < 0:
        return True
    return is_reverse_map(map_raw)


def motor_map_name(raw):
    """Human name for a raw power-map value.

    Unknown values are rendered WITH the number rather than hidden behind a
    generic "Unknown": if the placeholder numbering above is wrong, the driver
    and the pit both see exactly which value the controller is sending, which
    is what you need to correct MOTOR_MAPS.
    """
    if raw is None:
        return None
    return MOTOR_MAPS.get(raw, f"Map ?({raw})")


def parse_odometer_frame(data_bytes):
    """0x620 -> the controller's own TRIP and ODO, in metres.

        bytes 0-3  UINT32  TRIP [0.01 km]  (resettable)
        bytes 4-7  UINT32  ODO  [0.1 km]   (lifetime total)

    Worth having even though we integrate our own distance: this comes from the
    controller's wheel-size configuration rather than from
    drivetrain.TIRE_DIAMETER_METERS, which has never been measured. TRIP in
    particular is a far better basis for the lap-distance gate — it is a real
    counter rather than the running sum of an integration that has to survive
    frame drops and restarts.

    Converted to metres here so callers never have to remember which field is in
    0.01 km and which is in 0.1 km — a difference of 10x, and exactly the kind
    of mix-up that produces a plausible but wrong number.
    """
    if data_bytes is None or len(data_bytes) < 8:
        return {}
    trip_raw, odo_raw = struct.unpack_from("<II", data_bytes, 0)
    return {
        "mms_trip_m": round(trip_raw * 0.01 * 1000.0, 1),   # 0.01 km -> m
        "mms_odo_m": round(odo_raw * 0.1 * 1000.0, 1),      # 0.1 km  -> m
    }


def parse_motor_map(data_bytes):
    """Extract the power map from a status frame -> {raw, name}, or {} if absent.

    Shared by the driver HUD's CAN worker and the pit parser so a map can never
    read differently on the two screens.
    """
    if data_bytes is None or len(data_bytes) <= STATUS_MAP_BYTE:
        return {}
    raw = struct.unpack_from("<B", data_bytes, STATUS_MAP_BYTE)[0]
    return {"mms_motor_map_raw": raw, "mms_motor_map": motor_map_name(raw)}

# Status-word bit labels. These MIRROR can_worker.py so the pit shows exactly
# the same alerts the driver HUD does. The HUD decodes BOTH the Limit word
# (bytes 4-5) and the Error word (bytes 6-7); decoding only the error word here
# was why limit alerts (e.g. "Power Limit") showed on the HUD but not the pit.
_LIMIT_BITS = [
    (0, "Over-voltage Limit"),    (1, "Under-voltage Limit"),
    (2, "Controller Thermal Limit"), (3, "Motor Thermal Limit"),
    (4, "Over-current Limit"),    (5, "Speed Limit"),
    (6, "Regenerative Limit"),    (7, "Power Limit"),
    (8, "Motor Stall Limit"),     (9, "Throttle Fault Limit"),
]
_ERROR_BITS = [
    (0, "Over-voltage Error"),    (1, "Under-voltage Error"),
    (2, "Controller Over-temp"),  (3, "Motor Over-temp"),
    (4, "Over-current Fault"),    (5, "Hall Sensor Fault"),
    (6, "Communication Fault"),   (7, "Hardware Fault"),
    (8, "Throttle Error"),        (9, "Phase Imbalance"),
]


def _labels(word, bit_defs):
    """Active human-readable labels for the set bits in *word*."""
    return [label for bit, label in bit_defs if word & (1 << bit)]


def parse_motor_temp_frame(data_bytes):
    """Decode the temperature frame into motor-temperature fields.

    Split out of parse_mms_message so the driver HUD's CAN worker and the pit's
    parser share ONE decode — if these ever drifted apart, the HUD and the pit
    would show different motor temperatures for the same frame.

    Returns a dict with, when available:
        mms_motor_ohms            raw PT1000 resistance (Ω) — what we measured
        mms_motor_temp_C          that resistance converted via the datasheet table
        mms_motor_temp_status     pt1000.STATUS_* — why °C is missing, if it is
        mms_motor_temp_reported_C the controller's OWN conversion (cross-check)
        mms_temperature_C         controller (not motor) temperature
    """
    parsed = {}
    if data_bytes is None:
        return parsed

    # --- raw PT1000 resistance: the value this feature exists to surface ---- #
    end = PTC_OHMS_OFFSET + struct.calcsize(PTC_OHMS_FORMAT)
    if len(data_bytes) >= end:
        raw = struct.unpack_from(PTC_OHMS_FORMAT, data_bytes, PTC_OHMS_OFFSET)[0]
        if raw == THERMISTOR_DISCONNECTED:
            # The controller is explicitly telling us the probe is unplugged.
            # Say so, rather than letting 65535 Ω fall out as "above range" and
            # look like a mysteriously hot motor.
            parsed["mms_motor_temp_status"] = "disconnected"
        else:
            ohms = round(raw * PTC_OHMS_SCALE, 2)
            celsius, status = pt1000.read(ohms)
            # Always publish the Ohms, even when they convert to nothing: a raw
            # value that is out of range is exactly what diagnoses a
            # disconnected or shorted probe, and hiding it hides the fault.
            parsed["mms_motor_ohms"] = ohms
            parsed["mms_motor_temp_status"] = status
            if celsius is not None:
                parsed["mms_motor_temp_C"] = celsius

    # --- the controller's own PTC temperature channel (bytes 2-3) ----------- #
    # /driver/ptctemp, the value the controller's own thermal protection acts
    # on. Kept as an independent cross-check of our table conversion: if the two
    # disagree at a known ambient, one of them is wrong and it is worth knowing
    # which before trusting either on track.
    if len(data_bytes) >= 4:
        ptctemp = struct.unpack_from("<H", data_bytes, 2)[0]
        if ptctemp != TEMP_NOT_AVAILABLE and ptctemp != THERMISTOR_DISCONNECTED:
            parsed["mms_ptctemp_raw"] = ptctemp

    # --- controller temperature (unchanged behaviour) ----------------------- #
    if len(data_bytes) >= 5:
        parsed["mms_temperature_C"] = struct.unpack_from("<B", data_bytes, 4)[0]

    # --- why the controller disarmed, when it has --------------------------- #
    if len(data_bytes) >= 6:
        disarm = struct.unpack_from("<B", data_bytes, 5)[0]
        if disarm:
            parsed["mms_disarm_reason"] = disarm

    return parsed


# ==============================================================================
# THROTTLE PEDAL — GPIO0 over CAN (siliXcon ESC API)
# ==============================================================================
# Reference: https://docs.silixcon.com/docs/fw/modules/esc_api/examples/read_gpio_can
#
# ⚠️ THIS ONE IS NOT A BROADCAST. Every other frame in this file arrives on its
# own without being asked. GPIO readings do not: the controller sends NOTHING
# on 0x150 until a configuration frame is transmitted to 0x147 telling it which
# inputs to sample, how often, and which of its four reporting "banks" to use.
# That request is sent by main.py (see config.THROTTLE_GPIO_*), and it has to be
# re-sent after the controller reboots, because the configuration does not
# survive a power cycle. A silent throttle is therefore far more likely to mean
# "nobody armed the report" than "the pedal is broken" — check that first.
#
# ⚠️ AND IT IS BIG-ENDIAN. Every LYNX broadcast frame decoded above is
# little-endian ("<h", "<H"); the ESC API is documented as "All payloads use
# big-endian byte order" and uses ">H" here. Reading a 2058 mV pedal with the
# wrong endianness gives 2568 mV — not an obviously wrong number, just a wrong
# one, which is the kind that survives a bench test and lies all race.
#
# REQUEST — 8 bytes to CAN ID 0x147:
#     byte  0    UINT8   receiver address (the ESC's node address; 0 by default)
#     byte  1    UINT8   bank selector, 0x8..0xB for banks 0..3
#     byte  2    UINT8   start input ID
#     byte  3    UINT8   end input ID   (inclusive)
#     bytes 4-5  UINT16  sampling period [ms], big-endian
#     bytes 6-7  UINT16  delay between transmissions [ms], big-endian; 0 disables
#
# RESPONSE — periodic frames on the bank's ID (0x150/0x158/0x160/0x168):
#     byte  0    UINT8   device signature; BIT 0 IS AN ERROR FLAG
#     byte  1    UINT8   the input ID of the FIRST value in this frame
#     bytes 2-3  UINT16  value for that ID, in MILLIVOLTS, big-endian
#     bytes 4-5  UINT16  value for ID+1   (present when the range is wider)
#     bytes 6-7  UINT16  value for ID+2
# A range of more than three inputs continues in a second frame on the same ID,
# whose byte 1 names where it resumes — which is why the decode below walks IDs
# from byte 1 rather than assuming the frame starts at the requested start ID.
# ==============================================================================

# Where the ESC sends each bank's readings. Fixed by the protocol, not by the
# node address — unlike the 0x600/0x630 broadcast bases, these IDs do not shift
# with the controller's address; the address travels inside the REQUEST payload.
GPIO_REQUEST_ID = 0x147
GPIO_REPORT_IDS = {0: 0x150, 1: 0x158, 2: 0x160, 3: 0x168}
GPIO_REPORT_ID_SET = frozenset(GPIO_REPORT_IDS.values())

# The bank selector is NOT the bank number: bank 0 is requested as 0x8.
GPIO_BANK_SELECTOR_BASE = 0x8

# Input IDs. The docs' example reads "Start ID 0x8 (GPIO0)" through "End ID 0xC
# (GPIO4)", so the GPIOs are contiguous from 0x8.
GPIO_INPUT_ID_BASE = 0x8


def gpio_input_id(gpio_number):
    """GPIO number (0, 1, 2...) -> the input ID the ESC API addresses it by."""
    return GPIO_INPUT_ID_BASE + gpio_number


# Which GPIO the throttle pedal is wired to. GPIO0 per the feature request.
#
# ⚠️ UNVERIFIED ON THIS CAR. Nothing in the recorded telemetry proves the pedal
# is on GPIO0 rather than the ESC's dedicated throttle input — this is the
# number the feature was specified with, not a measurement. If the throttle
# trace stays flat while the car is clearly accelerating, widen the requested
# range (config.THROTTLE_GPIO_END_ID) to sweep GPIO0..GPIO4 and watch which one
# moves with the pedal; then set this to that GPIO.
THROTTLE_GPIO_NUMBER = 0
THROTTLE_INPUT_ID = gpio_input_id(THROTTLE_GPIO_NUMBER)

# Bit 0 of byte 0 means the controller could not serve the request (unknown
# input ID, bank misconfigured). The values in such a frame are meaningless.
GPIO_ERROR_FLAG = 0x01


def build_gpio_request(bank=0, start_id=THROTTLE_INPUT_ID,
                       end_id=THROTTLE_INPUT_ID, period_ms=100,
                       delay_ms=100, address=0):
    """The 8 payload bytes that ask the ESC to start reporting inputs.

    Defaults mirror the documented example (100 ms period, 100 ms delay)
    narrowed to a single input, because that is a configuration the vendor
    actually shows working. 100 ms is also the right cadence for the pit's
    throttle trace: fast enough to show a driver dabbing the pedal, slow enough
    that it costs one frame per 10 Hz on a 500 kbit/s wire.

    Raises ValueError on a request the ESC cannot answer, rather than putting a
    malformed frame on the motor controller's bus.
    """
    if bank not in GPIO_REPORT_IDS:
        raise ValueError(f"GPIO bank must be 0-3, got {bank!r}")
    if not 0 <= start_id <= 0xFF or not 0 <= end_id <= 0xFF:
        raise ValueError(f"input IDs must be a byte, got {start_id}..{end_id}")
    if end_id < start_id:
        raise ValueError(f"end ID {end_id:#x} is below start ID {start_id:#x}")
    if not 0 <= period_ms <= 0xFFFF or not 0 <= delay_ms <= 0xFFFF:
        raise ValueError("period and delay must fit in a UINT16 (ms)")
    return struct.pack(
        ">BBBBHH",                       # '>' — big-endian, see the note above
        address & 0xFF,
        GPIO_BANK_SELECTOR_BASE + bank,
        start_id,
        end_id,
        period_ms,
        delay_ms,
    )


def parse_gpio_report(arb_id, data_bytes):
    """A bank report frame -> {input_id: millivolts}, or {} if not one.

    Returns {} — not zeros — for a frame the controller flagged as an error,
    for a truncated frame, and for any ID that is not a GPIO report. Callers
    therefore cannot accidentally read a failed request as a pedal at rest.
    """
    if arb_id not in GPIO_REPORT_ID_SET:
        return {}
    if data_bytes is None or len(data_bytes) < 4:
        return {}          # signature + ID + at least one value
    if data_bytes[0] & GPIO_ERROR_FLAG:
        # The ESC is telling us the request itself was bad. Whatever is in the
        # value bytes is not a measurement.
        return {}

    first_id = data_bytes[1]
    values = {}
    # Walk 16-bit words from byte 2, assigning consecutive input IDs. Driven by
    # the frame's own length so a 6-byte continuation frame yields two values
    # rather than one real value and one read off the end.
    for slot in range((len(data_bytes) - 2) // 2):
        offset = 2 + slot * 2
        values[first_id + slot] = struct.unpack_from(">H", data_bytes, offset)[0]
    return values


def parse_throttle_frame(arb_id, data_bytes, input_id=THROTTLE_INPUT_ID):
    """A bank report frame -> the throttle fields both dashboards consume.

    Returns {} when this frame carries nothing about the throttle input, so a
    report covering other GPIOs never blanks a good reading.

        mms_throttle_mv        the raw millivolts, ALWAYS published when present
        mms_throttle_percent   0-100, or absent when the raw value is implausible
        mms_throttle_status    efficiency.THROTTLE_* — why the percent is missing
        mms_throttle_zone      "eco" | "normal" | "power" (absent with no percent)

    The raw mV is published even when it converts to nothing, for exactly the
    reason mms_motor_ohms is: an out-of-range raw value is what diagnoses a
    disconnected pedal, and it is also the number the team reads off the pit
    wall to replace efficiency.py's placeholder calibration.
    """
    values = parse_gpio_report(arb_id, data_bytes)
    if input_id not in values:
        return {}

    millivolts = values[input_id]
    percent, status = efficiency.throttle_percent(millivolts)

    parsed = {
        "mms_throttle_mv": millivolts,
        "mms_throttle_status": status,
    }
    if percent is not None:
        parsed["mms_throttle_percent"] = percent
        parsed["mms_throttle_zone"] = efficiency.zone(percent)
    return parsed


def parse_mms_message(arb_id, data_bytes):
    """
    Parses LYNX protocol messages from the Silixcon Motor Management System (MMS).
    Returns a dictionary of parsed telemetry data, or None if ID is not recognized.
    """
    parsed_data = {}
    
    # 1. Motor Data (RPM & Power)
    if arb_id == ID_MOTOR and len(data_bytes) >= 8:
        # <h = Little-Endian, signed int16 (2 bytes) starting at index 2.
        # Negative means reverse - the field is documented signed.
        #
        # Corrected to the TRUE motor speed right here, because this controller
        # reports exactly half of it (measured against gpsd over 1,955 moving
        # samples: multiplier 2.0101). Doing it at the decoder means every
        # consumer - the driver's gauge, the pit chart, the stored column - gets
        # a real RPM, and there is exactly one place doing the correction.
        # See drivetrain.RPM_REPORT_SCALE for the measurement and the real fix.
        rpm = drivetrain.true_motor_rpm(
            struct.unpack_from("<h", data_bytes, 2)[0])
        # <h = Little-Endian, SIGNED int16 (2 bytes) at index 6. Power is signed:
        # it goes negative during regen/coasting. Decoding it unsigned ("<H") made
        # small negatives wrap to ~65000 W (the "unrealistic power" on the pit/HUD).
        power_W = struct.unpack_from("<h", data_bytes, 6)[0]

        # Bytes 0-1: "Motor current [A] (q axis)" — INT16, signed (negative on
        # regen). This is the MOTOR's phase current, distinct from the battery
        # current the BMS reports; DS002 shows both.
        motor_current_A = struct.unpack_from("<h", data_bytes, 0)[0]

        # Bytes 4-5: the controller's own vehicle speed. The raw value is in
        # 0.1 km/h and is NOT gear-corrected — see decode_vehicle_speed_kmh()
        # above for the measurement that establishes both. Decoding it raw is
        # what produced the "6583 km/h" rows.
        vehicle_kmh = decode_vehicle_speed_kmh(
            struct.unpack_from("<h", data_bytes, 4)[0])

        parsed_data["mms_rpm"] = rpm
        parsed_data["mms_power_W"] = power_W
        parsed_data["mms_current_A"] = motor_current_A
        parsed_data["mms_vehicle_speed_kmh"] = vehicle_kmh

    # 2. MMS Measured Battery (Controller's perspective, NOT the real BMS)
    elif arb_id == ID_BATTERY and len(data_bytes) >= 6:
        # <B = Unsigned int8 at index 2
        soc = struct.unpack_from("<B", data_bytes, 2)[0]
        # <H = Little-Endian, unsigned int16 at index 4
        voltage_raw = struct.unpack_from("<H", data_bytes, 4)[0]
        
        # We prefix these with 'mms_' so they don't overwrite the real BMS data
        parsed_data["mms_estimated_soc_percent"] = soc
        parsed_data["mms_measured_voltage_V"] = round(voltage_raw * 0.01, 2)

    # 3. Odometer — the controller's own TRIP / ODO counters
    elif arb_id == ID_ODO:
        parsed_data.update(parse_odometer_frame(data_bytes))

    # 4. Temperature Data — controller temp + the PT1000 motor sensor
    elif arb_id in TEMP_FRAME_IDS:
        parsed_data.update(parse_motor_temp_frame(data_bytes))

    # 5. Throttle pedal — the GPIO bank report we asked the ESC for. Unlike
    # every branch above, this frame only exists because main.py transmitted a
    # request for it; see the GPIO section near the top of this file.
    elif arb_id in GPIO_REPORT_ID_SET:
        parsed_data.update(parse_throttle_frame(arb_id, data_bytes))

    # 4. Status, Limits, and Errors (same decode the driver HUD uses)
    elif arb_id == ID_STATUS and len(data_bytes) >= 8:
        # <H = Little-Endian unsigned int16. Limit word at byte 4, error at byte 6.
        limit_word = struct.unpack_from("<H", data_bytes, 4)[0]
        error_word = struct.unpack_from("<H", data_bytes, 6)[0]

        # Errors are more critical than limits, so list them first (like the HUD).
        alerts = _labels(error_word, _ERROR_BITS) + _labels(limit_word, _LIMIT_BITS)

        # Active power map (byte 2) — which energy configuration the car is on.
        parsed_data.update(parse_motor_map(data_bytes))
        # Driver status word (byte 3) — parking brake / lights / ECU / reverse.
        parsed_data.update(parse_driver_state(data_bytes))

        parsed_data["mms_error_code"] = error_word
        parsed_data["mms_limit_code"] = limit_word
        parsed_data["mms_alerts"] = alerts
        # Flag a fault only when a RECOGNISED bit is set, so undefined housekeeping
        # bits (e.g. 0x4000 that some controllers always set) don't false-alarm.
        parsed_data["mms_has_error"] = bool(alerts)

    return parsed_data if parsed_data else None