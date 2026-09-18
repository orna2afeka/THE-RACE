# live_metrics.py — the Live Metrics catalogue for the React/FastAPI dashboard.
#
# THE SINGLE SOURCE for the Live Metrics tab, in declarative form (a field or a
# named derivation per tile, never a lambda) so it can cross JSON to the
# frontend.
#
# Generated 2026-09-14 from the earlier Streamlit dashboard's
# LIVE_METRIC_GROUPS by parsing its AST, and checked against it at import until
# that app was removed (2026-09-16). The two solar tiles were dropped the same
# day: the car runs without an MPPT.
#
# 2026-09-16: trimmed to what the always-visible top strip does not already
# show (Energy stays whole: its tiles read as a race/stint/lap table), the
# always-zero controller SoC dropped, Trip and Odometer labels swapped to their
# usual meaning, and every note rewritten to say where the value comes from.
#
# FIELD SOURCES
#   "state.<k>"  — a key of the live-state dict the backend builds
#   derived      — a computation neither dict holds; resolve() implements each
#                  by name
#
# LIMITS are named as STRINGS so this module stays import-free and a FastAPI
# worker can load it in microseconds. Consumers resolve them against limits.py.

LIVE_METRICS_PER_ROW = 4

# Every limit name used below must exist in limits.py.
_KNOWN_LIMITS = {"SOC", "PACK_VOLTAGE", "BATT_CURRENT", "MOTOR_CURRENT"}

# Derived values resolve() computes by name.
_KNOWN_DERIVED = {
    "delta_to_target", "relative_regen_total",
    "relative_regen_stint", "relative_regen_lap", "active_lap",
    "lap_source", "controller_odometer_km", "throttle_zone_label",
}

LIVE_METRIC_GROUPS = [
    ("Motion", [
        dict(label="Target Speed", unit="km/h", spec=".1f", field="state.target_speed_kmh",
             note="the car's active speed profile, at the car's lap distance"),
        dict(label="Delta to Target", unit="km/h", spec="+.1f", derived="delta_to_target",
             note="actual speed minus target"),
        dict(label="Motor RPM", unit="rpm", spec=".0f", field="state.rpm",
             note="controller (CAN 0x610), corrected x2 on the car"),
    ]),
    ("Motor", [
        dict(label="Motor Current", unit="A", spec=".1f", limit="MOTOR_CURRENT", field="state.motor_current", mag=True,
             note="motor phase current from the controller, not battery current - amber only"),
    ]),
    # Two batteries, each with its own JBD BMS: A on can0, B on can1. One row
    # per battery, then the controller's reading of the voltage both feed.
    # The top strip's SoC is battery A's.
    ("Driver Input", [
        dict(label="Throttle", unit="%", spec=".0f", field="state.throttle_pct",
             note="pedal position from the ESC's GPIO0 (CAN 0x150)"),
        dict(label="Efficiency Zone", unit="", spec=".0f", derived="throttle_zone_label", text=True,
             note="eco / normal / power from efficiency.py"),
        dict(label="Throttle Raw", unit="mV", spec=".0f", field="state.throttle_mv",
             note="calibrate efficiency.py from this: pedal released, then floored"),
    ]),
    ("Battery", [
        dict(label="Battery A SoC", unit="%", spec=".0f", limit="SOC", field="state.soc",
             note="battery A's BMS (can0) coulomb count"),
        dict(label="Battery A Voltage", unit="V", spec=".2f", field="state.voltage",
             note="measured by battery A's BMS (CAN 0x100)"),
        dict(label="Battery A Current", unit="A", spec=".1f", limit="BATT_CURRENT", field="state.current", mag=True,
             note="battery A's BMS - negative = discharge"),
        dict(label="Pack Voltage", unit="V", spec=".2f", limit="PACK_VOLTAGE", field="state.pack_voltage",
             note="measured by the motor controller (CAN 0x618)"),
        dict(label="Battery B SoC", unit="%", spec=".0f", limit="SOC", field="state.soc_b",
             note="battery B's BMS (can1) coulomb count"),
        dict(label="Battery B Voltage", unit="V", spec=".2f", field="state.voltage_b",
             note="measured by battery B's BMS (CAN 0x100 on can1)"),
        dict(label="Battery B Current", unit="A", spec=".1f", limit="BATT_CURRENT", field="state.current_b", mag=True,
             note="battery B's BMS - negative = discharge"),
    ]),
    ("Energy", [
        dict(label="Total Race Energy", unit="Wh", spec=".0f", field="state.total_race_energy",
             note="car integrates controller power, net of regen - motor side, no auxiliary loads"),
        dict(label="Total Regen Energy", unit="Wh", spec=".0f", field="state.regen_energy",
             note="recovered while power was negative, whole race"),
        dict(label="Total Relative Regen", unit="%", spec=".1f", derived="relative_regen_total",
             note="share of drive energy recovered, whole race"),
        dict(label="Current Stint Energy", unit="Wh", spec=".1f", field="state.stint_energy",
             note="since the last charging stop the car detected - race total until the first"),
        dict(label="Current Stint Regen Energy", unit="Wh", spec=".1f", field="state.stint_regen_energy",
             note="stop = stopped + battery A charging over 1 A for 5 s"),
        dict(label="Current Stint Relative Regen", unit="%", spec=".1f", derived="relative_regen_stint",
             note="share of drive energy recovered, this stint"),
        dict(label="Last Lap Energy", unit="Wh", spec=".1f", field="state.last_lap_energy",
             note="taken at the last lap cut, held through the next lap"),
        dict(label="Last Lap Regen Energy", unit="Wh", spec=".1f", field="state.last_lap_regen_energy",
             note="taken at the last lap cut, held through the next lap"),
        dict(label="Last Lap Relative Regen", unit="%", spec=".1f", derived="relative_regen_lap",
             note="share of drive energy recovered, last lap"),
    ]),
    ("Lap & Distance", [
        dict(label="Lap", unit="", spec=".0f", derived="active_lap",
             note="the car's lap count, or the pit's manual override when one is set"),
        # The names follow the usual meaning: a TRIP resets, an ODOMETER does
        # not. state.odometer_km is the Pi's race distance, which the pit's
        # "Reset trip" zeroes; state.trip_m is the controller's own counter,
        # which the pit cannot reset. The state keys keep their car-side names.
        dict(label="Trip", unit="km", spec=".2f", field="state.odometer_km",
             note="race distance built on the Pi - Reset trip zeroes it, a Pi reboot does not"),
        dict(label="Odometer", unit="km", spec=".1f", derived="controller_odometer_km",
             note="motor controller's own counter (CAN 0x620) - not resettable from the pit"),
        dict(label="Lap Source", unit="", spec=".0f", derived="lap_source", text=True,
             note="how the last lap was cut - gps = finish line, gps_no_can, "
                  "odometer = distance fallback, manual = pit cut. Survives a "
                  "Pi reboot with the lap count; - until the first lap"),
    ]),
]


def _validate():
    """A label may appear once across the whole tab, and every limit/derived
    name must be one we know. Raises at import, not on race night.
    """
    seen = {}
    for group, entries in LIVE_METRIC_GROUPS:
        for m in entries:
            label = m["label"]
            if label in seen:
                raise ValueError("Live Metrics: %r appears in both %r and %r"
                                 % (label, seen[label], group))
            seen[label] = group
            if m.get("limit") and m["limit"] not in _KNOWN_LIMITS:
                raise ValueError("Live Metrics: %r has unknown limit %r"
                                 % (label, m["limit"]))
            if m.get("derived") and m["derived"] not in _KNOWN_DERIVED:
                raise ValueError("Live Metrics: %r has unknown derived %r"
                                 % (label, m["derived"]))
            if not m.get("field") and not m.get("derived"):
                raise ValueError("Live Metrics: %r has no field or derived" % label)

    return len(seen)


LIVE_METRIC_COUNT = _validate()


def _relative_regen(regen_wh, total_wh):
    """regen / (regen + total) as a PERCENTAGE — what fraction of gross forward
    energy at the motor came back as regen.

    None whenever either input is missing, and when the denominator is zero:
    a car that has not moved has no meaningful recovery ratio, and 0 % would
    read as "recovered nothing" rather than "nothing to recover from".
    """
    if regen_wh is None or total_wh is None:
        return None
    gross = (regen_wh or 0.0) + (total_wh or 0.0)
    if not gross:
        return None
    return (regen_wh / gross) * 100.0


def resolve(entry, state, ctx):
    """The raw value for one catalogue entry, given the two live dicts.

    Returns None for anything the car has not reported. Never 0 — an absent
    reading is not a measurement, and the whole project turns on that.
    """
    d = entry.get("derived")
    if d:
        if d == "delta_to_target":
            a, b = state.get("speed_kmh"), state.get("target_speed_kmh")
            return None if a is None or b is None else a - b
        if d == "relative_regen_total":
            return _relative_regen(state.get("regen_energy"),
                                   state.get("total_race_energy"))
        if d == "relative_regen_stint":
            return _relative_regen(state.get("stint_regen_energy"),
                                   state.get("stint_energy"))
        if d == "relative_regen_lap":
            return _relative_regen(state.get("last_lap_regen_energy"),
                                   state.get("last_lap_energy"))
        if d == "active_lap":
            return ctx.get("active_lap")
        if d == "lap_source":
            return state.get("lap_source")
        if d == "throttle_zone_label":
            # efficiency.py at the repo root is import-free, and its labels are
            # the ones the car uses — never retyped here.
            import efficiency
            z = state.get("throttle_zone")
            return None if z is None else efficiency.ZONE_LABELS.get(z, z)
        if d == "controller_odometer_km":
            m = state.get("trip_m")
            return None if m is None else m / 1000.0
        return None
    src, _, key = entry["field"].partition(".")
    return (state if src == "state" else ctx).get(key)
