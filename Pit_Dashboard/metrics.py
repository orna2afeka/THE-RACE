# metrics.py — the chartable-metric catalogue for the React/FastAPI dashboard.
#
# THE SINGLE SOURCE for History charts. The backend's history endpoints, the
# CSV/workbook exports and the frontend (which receives it as JSON) all read
# this list; nothing downstream retypes a label, unit or colour.
#
# Lifted from the earlier Streamlit dashboard's inline HISTORY_CHARTS when the
# React dashboard replaced it, and kept in step with it by a drift guard until
# that app was removed (2026-09-16). Solar Current was dropped the same day:
# the car runs without an MPPT, so there is no panel reading.
#
# This module imports nothing but the stdlib, so a FastAPI worker can load it
# in microseconds. Keep it that way.

from collections import namedtuple

# key      — the chart column name the history endpoints produce.
# label    — what the pit reads on screen and in an export header.
# unit     — drives the y-axis grouping; metrics sharing a unit share an axis.
# color    — fixed per metric so a trace keeps its identity between the screen,
#            the CSV and the workbook. Never retyped downstream.
# source   — the telemetry.db column this reads.
# divisor  — unit conversion, or None. A DIVISOR rather than a multiplier so the
#            arithmetic is bit-for-bit the same everywhere (odo / 1000.0).
Metric = namedtuple("Metric", "key label unit color source divisor")

HISTORY_CHARTS = [
    # The controller's own speed field, the same one the driver HUD reads.
    # Deliberately NO fallback to a value derived from RPM.
    Metric("Speed", "Speed", "km/h", "#00FFCC",
           "mms_vehicle_speed_kmh", None),
    Metric("Power", "Motor Power", "W", "#00B3FF",
           "mms_power_W", None),
    Metric("RPM", "Motor RPM", "rpm", "#9b59b6",
           "mms_rpm", None),
    Metric("SoC", "Battery SoC", "%", "#f1c40f",
           "bms_soc_percent", None),
    # The CONTROLLER's measurement, not the BMS's. NO fallback to bms_voltage_V:
    # the two disagree by ~2.25x, so gap-filling from the other would draw a
    # trace stepping between 50 V and 113 V that looks like a real electrical
    # event. A gap is honest.
    Metric("Voltage", "Battery Voltage", "V", "#2ecc71",
           "mms_measured_voltage_V", None),
    Metric("Current", "Battery Current", "A", "#e67e22",
           "bms_current_A", None),
    Metric("BattTemp", "Battery Temp", "°C", "#ff9900",
           "battery_temp_C", None),
    # The motor's OWN PT1000 sensor. The controller temperature is charted
    # separately, under its real name.
    Metric("MotorTemp", "Motor Temp", "°C", "#ff5e5e",
           "mms_motor_temp_C", None),
    Metric("CtrlTemp", "Controller Temp", "°C", "#e74c3c",
           "mms_temperature_C", None),
    Metric("MotorOhms", "Motor Sensor", "Ω", "#c39bd3",
           "mms_motor_ohms", None),
    # The store keeps metres; the chart reads kilometres.
    Metric("Distance", "Distance", "km", "#1abc9c",
           "odometer_m", 1000.0),
    Metric("Lap", "Lap", "#", "#7f8c9b",
           "calculated_lap", None),
    # Accumulated motor energy — the consumption curve. The running total the
    # CAR integrates, so it survives telemetry dropouts. NET of regen, so the
    # trace can legitimately dip on a long descent.
    Metric("Energy", "Total Race Energy", "Wh", "#58D68D",
           "total_race_energy", None),
]

_keys = [m.key for m in HISTORY_CHARTS]
if len(_keys) != len(set(_keys)):
    raise RuntimeError("duplicate metric key in HISTORY_CHARTS: %r" % (_keys,))


def value_from_row(row, metric):
    """Pull one metric's value out of a telemetry.db row, applying its divisor.

    A reading the car never sent stays None — NOT 0. The old `or 0` coalescing
    turned every telemetry dropout into a confident lie: the pack "reaching
    0 V", the battery "at 0 °C", and those zeros being averaged into stint
    statistics.

    A column the database does not have (an older store, before solar current
    or throttle existed) reads as None rather than raising, so the backend
    keeps serving every other metric.
    """
    try:
        raw = row[metric.source]
    except (IndexError, KeyError):
        return None
    if raw is None:
        return None
    return raw / metric.divisor if metric.divisor else raw
