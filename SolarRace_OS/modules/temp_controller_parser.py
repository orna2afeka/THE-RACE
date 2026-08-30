"""
temp_controller_parser.py
==========================================
Decodes the Orion BMS Thermistor Expansion Module (a J1939-style CAN device).
Datasheet: reference/thermistor_module_canbus.pdf.

Unlike the BMS, this module BROADCASTS continuously (~100 ms) — no polling
is required. Two frames matter:

    CAN ID 0x1839F3xx  — "Thermistor Module -> BMS" broadcast (8 bytes)
        Byte 0 : Thermistor module number
        Byte 1 : Lowest  thermistor value  (int8, °C)
        Byte 2 : Highest thermistor value  (int8, °C)
        Byte 3 : Average thermistor value  (int8, °C)
        Byte 4 : Number of thermistors enabled (bit 0x80 = fault present)
        Byte 5 : Highest thermistor ID on module
        Byte 6 : Lowest  thermistor ID on module
        Byte 7 : Checksum

    CAN ID 0x1838F3xx  — "Thermistor General" broadcast (8 bytes) — DS003
        Byte 0-1 : Thermistor ID relative to ALL configured modules (u16,
                   little-endian — module #1 starts at 0, module #2 at 80,
                   etc; see Note #5. Only matters once a second module is
                   ever added — this car has one)
        Byte 2   : Thermistor value (int8, °C) for the ID below (Note #7)
        Byte 3   : Thermistor ID relative to THIS module, zero-based
                   (bit 0x80 = fault present for this one sensor, Note #6)
        Byte 4   : Lowest  thermistor value on module (int8, °C)
        Byte 5   : Highest thermistor value on module (int8, °C)
        Byte 6   : Highest thermistor ID on module (zero based)
        Byte 7   : Lowest  thermistor ID on module (zero based)

    THE ROUND-ROBIN, AND WHY "NOT CONFIGURED" HAS NO WIRE SIGNAL OF ITS OWN
    0x1838F3xx reports ONE thermistor per frame, cycling through every
    thermistor that has been individually LOADED/ENABLED on the module via
    Orion's own Thermistor Utility software (Note #4). A thermistor that
    has not been enabled is not skipped-with-a-placeholder — it is simply
    never transmitted, forever, indistinguishable on the wire from "not
    configured yet" versus "hasn't cycled around since I started
    listening". The caller must remember the last value seen per ID (this
    module carries no per-frame state of its own — see its docstring's "free
    of CAN/Qt imports" design) and let a sensor stay unreported until its
    first-ever frame arrives; there is no frame that means "configure me".

The ID is a 29-bit (extended) J1939 identifier. Its low byte is the J1939
source address (the module number), so module #1 = 0x1839F380/0x1838F380,
module #2 = 0x1839F381/0x1838F381, etc. We mask the source address off so
any module is accepted.

Temperatures are 8-bit SIGNED °C (so sub-zero readings work too).
"""

import struct

# Base PGN of the "module -> BMS" summary frame, with the source-address
# (low) byte masked out so every module number matches.
_TEMP_SUMMARY_ID = 0x1839F300
_TEMP_ID_MASK = 0xFFFFFF00

# Base PGN of the per-thermistor "General Broadcast" frame — DS003's source.
_THERM_GENERAL_ID = 0x1838F300
_THERM_ID_MASK = 0xFFFFFF00

# DS003 of the technical regulations: "Temperature of all battery Cells (30
# sensors)". Thermistor IDs at or beyond this (module-relative, 1-indexed)
# are outside the compliance screen's scope and simply ignored — mirrors
# db.BMS_CELL_COLUMN_COUNT's role for cell voltages, though that one is sized
# to the JBD protocol's own wire limit rather than a fixed regulation count:
# the Orion module supports up to 80 thermistors, this car only needs 30.
THERMISTOR_COUNT = 30


def parse_temp_controller_message(arb_id, data_bytes):
    """
    Parse a battery-temperature controller SUMMARY frame (0x1839F3xx).

    Returns a dict with the pack temperature summary, or None if the frame
    is not the summary broadcast we care about.

    'battery_temp_C' mirrors the average and is the single headline value
    the pit dashboard and driver HUD display; low/high/avg are kept for
    completeness. 'thermistors_enabled_count' is DS003's own "has the car
    been configured yet" signal — see parse_thermistor_general_message's
    docstring for why the per-sensor frame can't answer that on its own.
    """
    if (arb_id & _TEMP_ID_MASK) != _TEMP_SUMMARY_ID:
        return None
    if len(data_bytes) < 4:
        return None

    # 'b' = signed 8-bit. Bytes 1/2/3 = lowest / highest / average °C.
    lowest_C, highest_C, average_C = struct.unpack_from("bbb", data_bytes, 1)

    result = {
        "battery_temp_low_C": lowest_C,
        "battery_temp_high_C": highest_C,
        "battery_temp_avg_C": average_C,
        "battery_temp_C": average_C,   # headline value for the pit dashboard
        "temp_module": data_bytes[0],
    }
    if len(data_bytes) >= 5:
        enabled_byte = data_bytes[4]
        result["thermistors_enabled_count"] = enabled_byte & 0x7F
        result["thermistors_enabled_fault"] = bool(enabled_byte & 0x80)
    return result


def parse_thermistor_general_message(arb_id, data_bytes):
    """
    Parse ONE frame of the per-thermistor round-robin broadcast (0x1838F3xx)
    — DS003's data source. Each frame carries exactly one sensor's current
    reading; the caller accumulates these into a full 1..THERMISTOR_COUNT
    snapshot over time, the same way main.py already accumulates per-cell
    BMS voltages out of bms_parser's 3-cells-per-frame messages.

    Returns None for a frame that isn't this broadcast, or whose
    module-relative ID falls outside THERMISTOR_COUNT (DS003's own scope —
    a car with a second, differently-numbered module would need widening
    this, not silently misfiling into slot 1's cell).
    """
    if (arb_id & _THERM_ID_MASK) != _THERM_GENERAL_ID:
        return None
    if len(data_bytes) < 8:
        return None

    global_id = data_bytes[0] | (data_bytes[1] << 8)
    # 'b' = signed 8-bit (the two module-wide bounds are temperatures too and
    # can be sub-zero); 'B' = unsigned (the ID+fault-bit byte).
    value_C, module_rel_byte, module_lowest_C, module_highest_C = \
        struct.unpack_from("bBbb", data_bytes, 2)
    module_rel_id = module_rel_byte & 0x7F        # zero-based (Note #6)
    fault = bool(module_rel_byte & 0x80)

    cell_num = module_rel_id + 1                  # 1-indexed, matches bms_cell_NN_V
    if not (1 <= cell_num <= THERMISTOR_COUNT):
        return None

    return {
        "cell_num": cell_num,
        "value_C": value_C,
        "fault": fault,
        "global_id": global_id,
        "module_lowest_C": module_lowest_C,
        "module_highest_C": module_highest_C,
    }
