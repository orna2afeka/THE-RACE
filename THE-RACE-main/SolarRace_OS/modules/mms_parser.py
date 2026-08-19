import struct

try:                        # normal import (package layout on the Pi)
    from modules import pt1000
except ImportError:         # when modules/ is itself on sys.path
    import pt1000

# ==============================================================================
# LYNX PROTOCOL CAN IDs (Silixcon MMS)
# ==============================================================================
ID_STATUS  = 0x600   # Status, mode, power map, protections, limits, errors
ID_MOTOR   = 0x610   # Motor current, RPM, VEHICLE SPEED (km/h), power
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

# Confirmed against the car: raw 0 = Map 1 ... raw 3 = Reverse.
# If the controller ever turns out to send 1-4 instead, shift these four keys
# and nothing else changes — every screen reads the name from here, and both
# also display the raw number so a mismatch is visible immediately.
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


def reverse_active(map_raw=None, rpm=None):
    """Is the car in reverse? Decided from real, documented signals only.

    A negative motor RPM is a direct physical measurement — bytes 2-3 of 0x610
    are a documented signed INT16 "Motor speed [rpm]", so a negative value means
    the motor is physically turning backwards. That is the strongest evidence
    available and it needs no undocumented bit.

    The reverse POWER MAP corroborates it. Note the map NUMBERING is still a
    placeholder (see MOTOR_MAPS), so it is treated as supporting evidence rather
    than proof — but a car in the reverse map with negative RPM is unambiguous.
    """
    if rpm is not None and rpm < 0:
        return True
    if map_raw is not None and motor_map_name(map_raw) == "Reverse":
        return True
    return False


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

def parse_mms_message(arb_id, data_bytes):
    """
    Parses LYNX protocol messages from the Silixcon Motor Management System (MMS).
    Returns a dictionary of parsed telemetry data, or None if ID is not recognized.
    """
    parsed_data = {}
    
    # 1. Motor Data (RPM & Power)
    if arb_id == ID_MOTOR and len(data_bytes) >= 8:
        # <h = Little-Endian, signed int16 (2 bytes) starting at index 2
        rpm = struct.unpack_from("<h", data_bytes, 2)[0]
        # <h = Little-Endian, SIGNED int16 (2 bytes) at index 6. Power is signed:
        # it goes negative during regen/coasting. Decoding it unsigned ("<H") made
        # small negatives wrap to ~65000 W (the "unrealistic power" on the pit/HUD).
        power_W = struct.unpack_from("<h", data_bytes, 6)[0]

        # Bytes 0-1: "Motor current [A] (q axis)" — INT16, signed (negative on
        # regen). This is the MOTOR's phase current, distinct from the battery
        # current the BMS reports; DS002 shows both.
        motor_current_A = struct.unpack_from("<h", data_bytes, 0)[0]

        # Bytes 4-5: "Vehicle speed [km/h]", INT16. The controller computes this
        # itself from its own configured wheel size, so it needs neither our
        # gear ratio nor our tire diameter — which matters, because
        # drivetrain.TIRE_DIAMETER_METERS is still an unmeasured placeholder.
        # Published alongside our computed speed so the two can be compared:
        # a persistent ratio between them IS the tire calibration error.
        vehicle_kmh = struct.unpack_from("<h", data_bytes, 4)[0]

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