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
# FIELD SOURCES
#   "state.<k>"  — a key of the live-state dict the backend builds
#   derived      — a computation neither dict holds; resolve() implements each
#                  by name
#
# LIMITS are named as STRINGS so this module stays import-free and a FastAPI
# worker can load it in microseconds. Consumers resolve them against limits.py.

LIVE_METRICS_PER_ROW = 4

# Every limit name used below must exist in limits.py.
_KNOWN_LIMITS = {
    "MOTOR_TEMP", "CTRL_TEMP", "CELL_TEMP", "SOC", "PACK_VOLTAGE",
    "BATT_CURRENT", "MOTOR_CURRENT", "POWER", "SPEED",
    "CELL_VOLTAGE",
}

# Derived values resolve() computes by name.
_KNOWN_DERIVED = {
    "delta_to_target", "relative_regen_total",
    "relative_regen_stint", "relative_regen_lap", "active_lap",
    "lap_distance_m", "lap_source", "last_lap_time_text",
    "throttle_zone_label",
}

LIVE_METRIC_GROUPS = [
    ("Motion", [
        dict(label="Speed", unit="km/h", spec=".1f", limit="SPEED", field="state.speed_kmh"),
        dict(label="Target Speed", unit="km/h", spec=".1f", field="state.target_speed_kmh",
             note="from the active velocity profile"),
        dict(label="Delta to Target", unit="km/h", spec="+.1f", derived="delta_to_target",
             note="actual minus target"),
        dict(label="Motor RPM", unit="rpm", spec=".0f", field="state.rpm"),
    ]),
    ("Motor", [
        dict(label="Motor Power", unit="W", spec=".0f", limit="POWER", field="state.power_w",
             note="negative = regen"),
        dict(label="Motor Temp", unit="°C", spec=".1f", limit="MOTOR_TEMP", field="state.motor_temp"),
        dict(label="Motor Sensor", unit="Ω", spec=".1f", field="state.motor_ohms",
             note="raw PT1000; the temp is derived from this"),
        dict(label="Motor Current", unit="A", spec=".1f", limit="MOTOR_CURRENT", field="state.motor_current", mag=True,
             note="amber only; high current is normal"),
        dict(label="Power Map", unit="", spec=".0f", field="state.motor_map", text=True),
    ]),
    ("Driver Input", [
        dict(label="Throttle", unit="%", spec=".0f", field="state.throttle_pct",
             note="pedal position - see zone below"),
        dict(label="Efficiency Zone", unit="", spec=".0f", derived="throttle_zone_label", text=True,
             note="what the driver's HUD bar is showing"),
        dict(label="Throttle Raw", unit="mV", spec=".0f", field="state.throttle_mv",
             note="calibrate efficiency.py from this"),
    ]),
    ("Controller", [
        dict(label="Controller Temp", unit="°C", spec=".1f", limit="CTRL_TEMP", field="state.temp"),
    ]),
    ("Battery", [
        dict(label="Battery SoC", unit="%", spec=".0f", limit="SOC", field="state.soc",
             note="BMS coulomb count"),
        dict(label="Pack Voltage", unit="V", spec=".2f", limit="PACK_VOLTAGE", field="state.pack_voltage",
             note="controller measurement - the one to trust"),
        dict(label="Pack Voltage (BMS)", unit="V", spec=".2f", field="state.voltage",
             note="agrees since 2026-08-20 12:12; older history reads 2.25x high"),
        dict(label="Battery Current", unit="A", spec=".1f", limit="BATT_CURRENT", field="state.current", mag=True,
             note="negative = discharge"),
        dict(label="Battery Temp", unit="°C", spec=".1f", limit="CELL_TEMP", field="state.batt_temp",
             note="hottest cell in the pack"),
        dict(label="SoC (controller est.)", unit="%", spec=".0f", field="state.soc_ctrl",
             note="unimplemented on this controller - always 0"),
    ]),
    ("Energy", [
        dict(label="Total Race Energy", unit="Wh", spec=".0f", field="state.total_race_energy",
             note="integrated on the car, net of regen"),
        dict(label="Total Regen Energy", unit="Wh", spec=".0f", field="state.regen_energy",
             note="recovered under braking, whole race"),
        dict(label="Total Relative Regen", unit="%", spec=".1f", derived="relative_regen_total",
             note="regen / (regen + total), whole race"),
        dict(label="Current Stint Energy", unit="Wh", spec=".1f", field="state.stint_energy",
             note="since the last detected charging stop"),
        dict(label="Current Stint Regen Energy", unit="Wh", spec=".1f", field="state.stint_regen_energy",
             note="since the last detected charging stop"),
        dict(label="Current Stint Relative Regen", unit="%", spec=".1f", derived="relative_regen_stint",
             note="regen / (regen + total), this stint"),
        dict(label="Last Lap Energy", unit="Wh", spec=".1f", field="state.last_lap_energy"),
        dict(label="Last Lap Regen Energy", unit="Wh", spec=".1f", field="state.last_lap_regen_energy"),
        dict(label="Last Lap Relative Regen", unit="%", spec=".1f", derived="relative_regen_lap",
             note="regen / (regen + total), last lap"),
    ]),
    ("Lap & Distance", [
        dict(label="Lap", unit="", spec=".0f", derived="active_lap"),
        dict(label="Lap Distance", unit="m", spec=".0f", derived="lap_distance_m"),
        dict(label="Last Lap Time", unit="", spec=".0f", derived="last_lap_time_text", text=True),
        dict(label="Odometer", unit="km", spec=".2f", field="state.odometer_km"),
        dict(label="Trip", unit="m", spec=".0f", field="state.trip_m", note="controller trip counter"),
        dict(label="Lap Source", unit="", spec=".0f", derived="lap_source", text=True,
             note="what triggered the last lap"),
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
        if d == "lap_distance_m":
            return ctx.get("current_lap_dist_m")
        if d == "lap_source":
            return state.get("lap_source")
        if d == "last_lap_time_text":
            return state.get("last_lap_time_s")
        if d == "throttle_zone_label":
            # efficiency.py at the repo root is import-free, and its labels are
            # what the driver HUD shows — never retyped here.
            import efficiency
            z = state.get("throttle_zone")
            return None if z is None else efficiency.ZONE_LABELS.get(z, z)
        return None
    src, _, key = entry["field"].partition(".")
    return (state if src == "state" else ctx).get(key)
