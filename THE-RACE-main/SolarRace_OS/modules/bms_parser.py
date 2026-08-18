"""
bms_parser.py
==========================================
Decodes JBD-protocol CAN frames from the Battery Management System (BMS).

Key protocol facts (see BMS_CAN_PROTOCOL.pdf):
  • CAN 2.0B, 11-bit standard IDs, 500 kbit/s.
  • Master/slave: the BMS only replies after being polled (the host sends
    the wanted ID + one 0x5A byte; the BMS answers on the SAME ID). The
    polling lives in main.py / config.BMS_POLL_*; this file only DECODES
    the responses.
  • Big-Endian byte order (high byte first).
  • Each real-time response is 6 data bytes + a 2-byte CRC-16 (so DLC 8).
    We read the documented data bytes and ignore the trailing CRC.
"""

import struct

# ── Protection-status bit definitions (ID 0x102, bytes 4-5) ─────────── #
# A set bit (=1) means that protection is currently active. Order/labels
# come straight from "Note 1" of the protocol document.
_PROTECTION_BITS = [
    (0,  "Cell Overvoltage"),
    (1,  "Cell Undervoltage"),
    (2,  "Pack Overvoltage"),
    (3,  "Pack Undervoltage"),
    (4,  "Charge Over-temp"),
    (5,  "Charge Under-temp"),
    (6,  "Discharge Over-temp"),
    (7,  "Discharge Under-temp"),
    (8,  "Charge Overcurrent"),
    (9,  "Discharge Overcurrent"),
    (10, "Short Circuit Protection"),
    (11, "IC Error (Front-end)"),
]


def parse_jbd_bms_message(arb_id, data_bytes):
    """
    Parse a single JBD BMS response frame.

    Returns a dict of telemetry for recognised IDs, or None if the ID is
    not handled or the frame is too short for its documented layout.
    """
    parsed_data = {}

    # 1. Basic status — voltage / current / remaining capacity (needs 6 B)
    if arb_id == 0x100 and len(data_bytes) >= 6:
        # >HhH = Big-Endian: uint16, int16 (signed), uint16
        voltage_raw, current_raw, rem_cap_raw = struct.unpack_from(">HhH", data_bytes, 0)
        parsed_data["bms_voltage_V"] = round(voltage_raw * 0.01, 2)     # 10 mV units
        parsed_data["bms_current_A"] = round(current_raw * 0.01, 2)     # 10 mA units, +=charge
        parsed_data["bms_remaining_Ah"] = round(rem_cap_raw * 0.01, 2)  # 10 mAh units

    # 2. Capacity, cycles, and true state-of-charge
    elif arb_id == 0x101 and len(data_bytes) >= 6:
        full_cap_raw, cycles, rsoc = struct.unpack_from(">HHH", data_bytes, 0)
        parsed_data["bms_full_capacity_Ah"] = round(full_cap_raw * 0.01, 2)
        parsed_data["bms_cycles"] = cycles
        parsed_data["bms_soc_percent"] = rsoc

    # 3. Balancing & protection flags
    elif arb_id == 0x102 and len(data_bytes) >= 6:
        # Bytes 0-3 = cell-balancing bitmask (cells 1-33); bytes 4-5 = protection
        balance_mask = struct.unpack_from(">I", data_bytes, 0)[0]
        protection_flags = struct.unpack_from(">H", data_bytes, 4)[0]

        parsed_data["bms_balancing_active"] = balance_mask != 0
        parsed_data["bms_balance_mask"] = balance_mask

        active = [label for bit, label in _PROTECTION_BITS if protection_flags & (1 << bit)]
        parsed_data["bms_has_error"] = bool(active)
        parsed_data["bms_error_code"] = protection_flags
        parsed_data["bms_protections"] = active

    # 4. Hardware specs — string count and NTC probe count
    elif arb_id == 0x104 and len(data_bytes) >= 2:
        parsed_data["bms_string_count"] = data_bytes[0]
        parsed_data["bms_ntc_count"] = data_bytes[1]

    # 5. Temperature probes (NTC1-3). Unit 0.1 K -> °C = K*0.1 - 273.15
    elif arb_id == 0x105 and len(data_bytes) >= 6:
        ntc1, ntc2, ntc3 = struct.unpack_from(">HHH", data_bytes, 0)
        parsed_data["bms_temp_1_C"] = round((ntc1 * 0.1) - 273.15, 1)
        parsed_data["bms_temp_2_C"] = round((ntc2 * 0.1) - 273.15, 1)
        parsed_data["bms_temp_3_C"] = round((ntc3 * 0.1) - 273.15, 1)

    # 6. Cell voltages — 3 cells per ID, incrementing 0x107..0x110 (1 mV units)
    elif 0x107 <= arb_id <= 0x110 and len(data_bytes) >= 6:
        first_cell = (arb_id - 0x107) * 3 + 1
        cell_a, cell_b, cell_c = struct.unpack_from(">HHH", data_bytes, 0)
        for offset, raw_mv in enumerate((cell_a, cell_b, cell_c)):
            parsed_data[f"bms_cell_{first_cell + offset:02d}_V"] = round(raw_mv * 0.001, 3)

    return parsed_data if parsed_data else None
