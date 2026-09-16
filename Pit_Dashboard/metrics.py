# metrics.py — the chartable-metric catalogue for the React/FastAPI dashboard.
#
# ⚠️ THIS IS A SECOND COPY, AND THAT IS A DELIBERATE (TEMPORARY) CHOICE.
#
# pit_dashboard.py is kept BYTE-IDENTICAL to the race-day app in ../THE RACE, so
# the Streamlit dashboard in this clone behaves exactly like the one that runs
# the race. That means its HISTORY_CHARTS stays inline, and the FastAPI backend
# cannot import it (doing so pulls Streamlit into a web worker and executes
# page-level code). So the catalogue is mirrored here.
#
# The cost is real: this list and pit_dashboard.py's must be changed together or
# the two dashboards disagree about a metric's colour or its source column.
# _verify() below is the guard — it re-reads pit_dashboard.py at import and
# fails loudly if the two have drifted. Nothing silent.
#
# Re-applying the single-source extraction (pit_dashboard.py importing from
# here) is about 30 minutes of work and removes this whole problem; it was
# reverted only to keep the Streamlit app a byte-for-byte reference copy.
#
# REGENERATED 2026-09-14 from pit_dashboard.py at commit bde0877, which added
# Throttle and Solar Current. Solar Current removed 2026-09-16 to match
# pit_dashboard.py: the car runs without an MPPT, so there is no panel reading.
#
# This module imports nothing but the stdlib, so a FastAPI worker can load it
# in microseconds. Keep it that way.

import os
import re
from collections import namedtuple

# key      — the DataFrame / chart column name, as read_history_df() produces.
# label    — what the pit reads on screen and in an export header.
# unit     — drives the y-axis grouping; metrics sharing a unit share an axis.
# color    — fixed per metric so a trace keeps its identity between the screen,
#            the CSV and the workbook. Never retyped downstream.
# source   — the telemetry.db column this reads, from read_history_df().
# divisor  — unit conversion, or None. A DIVISOR rather than a multiplier so the
#            arithmetic is bit-for-bit what read_history_df does (odo / 1000.0).
Metric = namedtuple("Metric", "key label unit color source divisor")

HISTORY_CHARTS = [
    # The controller's own speed field, the same one the driver HUD reads.
    # Deliberately NO fallback to a value derived from RPM.
    Metric("Speed", "Speed", "km/h", "#00FFCC",
           "mms_vehicle_speed_kmh", None),
    # The pit wall's driver-coaching signal, read together with Speed: speed is
    # what the car did, throttle is what the driver asked for, and the gap is
    # where the energy goes. NaN never 0 — a dropout must not read as lift-off.
    Metric("Throttle", "Throttle", "%", "#ff4dd2",
           "mms_throttle_percent", None),
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

# Where to find the Streamlit app this catalogue mirrors.
#
# It is NOT in this folder — the Streamlit dashboard lives in ../THE RACE and
# that is where it is run from. Without a target the drift guard would be
# silently inert, which is worse than not having one, so it looks next door.
# SOLARRACE_PIT_DASHBOARD overrides the path; if nothing is found the guard
# stays quiet, because it genuinely cannot check.
def _find_pit_dashboard():
    import os
    override = os.environ.get("SOLARRACE_PIT_DASHBOARD")
    if override:
        return override if os.path.isfile(override) else None
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (
        os.path.join(here, "pit_dashboard.py"),
        os.path.join(os.path.dirname(os.path.dirname(here)),
                     "THE RACE", "Pit_Dashboard", "pit_dashboard.py"),
    ):
        if os.path.isfile(path):
            return path
    return None



def _verify():
    """Fail at import if this copy has drifted from pit_dashboard.py.

    Parses the HISTORY_CHARTS literal out of pit_dashboard.py as TEXT — no
    import, so no Streamlit — and compares the (key, label, unit, colour)
    tuples. This is the price of keeping two copies: the drift is caught here,
    at start-up, instead of on the pit wall when a trace changes colour between
    the two dashboards.

    Silent on a missing or unreadable pit_dashboard.py: the backend must still
    start if the Streamlit app is not deployed beside it.
    """
    path = _find_pit_dashboard()
    if path is None:
        return
    try:
        src = open(path, encoding="utf-8").read()
    except OSError:
        return
    m = re.search(r"^HISTORY_CHARTS = \[(.*?)^\]", src, re.S | re.M)
    if not m:
        return
    theirs = re.findall(
        r'\(\s*"([^"]+)",\s*"([^"]+)",\s*"([^"]*)",\s*"(#[0-9A-Fa-f]{6})"\s*\)',
        m.group(1))
    ours = [(x.key, x.label, x.unit, x.color) for x in HISTORY_CHARTS]
    if theirs and theirs != ours:
        only_theirs = [t for t in theirs if t not in ours]
        only_ours = [t for t in ours if t not in theirs]
        raise RuntimeError(
            "metrics.py has drifted from pit_dashboard.py's HISTORY_CHARTS.\n"
            "  in pit_dashboard.py only: %r\n"
            "  in metrics.py only:       %r\n"
            "Update this file to match, then restart." % (only_theirs, only_ours))


_verify()


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
