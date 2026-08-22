"""
temp_controller_parser.py
==========================================
Decodes the battery-temperature controller (a J1939 thermistor module).

Unlike the BMS, this module BROADCASTS continuously (~100 ms) — no polling
is required. We only care about the pack-temperature summary frame:

    CAN ID 0x1839F3xx  — "Thermistor Module -> BMS" broadcast (8 bytes)
        Byte 0 : Thermistor module number
        Byte 1 : Lowest  thermistor value  (int8, °C)
        Byte 2 : Highest thermistor value  (int8, °C)
        Byte 3 : Average thermistor value  (int8, °C)
        Byte 4 : Number of thermistors enabled
        Byte 5 : Highest thermistor ID on module
        Byte 6 : Lowest  thermistor ID on module
        Byte 7 : Checksum

The ID is a 29-bit (extended) J1939 identifier. Its low byte is the J1939
source address (the module number), so module #1 = 0x1839F380,
module #2 = 0x1839F381, etc. We mask the source address off so any module
is accepted.

Temperatures are 8-bit SIGNED °C (so sub-zero readings work too).
"""

import struct

# Base PGN of the "module -> BMS" summary frame, with the source-address
# (low) byte masked out so every module number matches.
_TEMP_SUMMARY_ID = 0x1839F300
_TEMP_ID_MASK = 0xFFFFFF00


def parse_temp_controller_message(arb_id, data_bytes):
    """
    Parse a battery-temperature controller frame.

    Returns a dict with the pack temperature summary, or None if the frame
    is not the summary broadcast we care about.

    'battery_temp_C' mirrors the average and is the single headline value
    the pit dashboard displays; low/high/avg are kept for completeness.
    """
    if (arb_id & _TEMP_ID_MASK) != _TEMP_SUMMARY_ID:
        return None
    if len(data_bytes) < 4:
        return None

    # 'b' = signed 8-bit. Bytes 1/2/3 = lowest / highest / average °C.
    lowest_C, highest_C, average_C = struct.unpack_from("bbb", data_bytes, 1)

    return {
        "battery_temp_low_C": lowest_C,
        "battery_temp_high_C": highest_C,
        "battery_temp_avg_C": average_C,
        "battery_temp_C": average_C,   # headline value for the pit dashboard
        "temp_module": data_bytes[0],
    }
