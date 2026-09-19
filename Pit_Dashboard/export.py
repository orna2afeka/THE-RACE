"""
export.py — export stored telemetry history to CSV or Excel
===========================================================
Reads from the local SQLite store (never Firebase). Filter by device, by which
metrics (columns) to include, and by time range.

The dashboard's download button produces a clean, readable **Excel workbook**
(`to_xlsx_bytes`): a formatted Data sheet, a Laps sheet (one row per lap), a
Charts sheet of history graphs, and a Faults sheet. The raw CSV writer
(`to_csv_bytes`) is kept for machine use / the CLI.

As a library:
    from export import to_xlsx_bytes, to_csv_bytes
    data, n = to_xlsx_bytes(start_ts=..., end_ts=..., metrics=["bms_soc_percent"])

As a CLI (format is chosen by the --out extension: .xlsx -> workbook, else CSV):
    python export.py --out race.xlsx
    python export.py --out race.csv
    python export.py --out soc.csv --metric bms_soc_percent --metric mms_temperature_C
    python export.py --out window.xlsx --start 2026-06-18T09:00 --end 2026-06-18T11:00
    python export.py --list-metrics
"""

import argparse
import csv
import io
import math
import sys
from datetime import datetime, timezone

import db
# No drivetrain constants are needed here any more: the Speed column is the
# controller's own CAN field, not a formula applied to RPM. Importing
# speed_kmh() again would be the first step back to two disagreeing speeds.
import pit_config          # export_local(): the Tel Aviv -> Brussels switch
from pit_config import DEVICE_ID

# Fixed identity/time columns that always lead the raw CSV.
_BASE_COLUMNS = ["rtdb_key", "device_id", "device_ts_epoch", "device_ts_iso", "ingested_ts_epoch"]

# Subsystem groups — what the dashboard offers as BMS / MMS / Temperature / GPS
# toggles, so users filter by system instead of remembering raw column names.
METRIC_GROUPS = {
    "BMS (battery)": ["bms_soc_percent", "bms_voltage_V", "bms_current_A",
                      "bms2_soc_percent", "bms2_voltage_V", "bms2_current_A"],
    "MMS (motor)": ["mms_rpm", "mms_power_W", "mms_temperature_C",
                    "mms_motor_temp_C",
                    "mms_motor_map", "mms_motor_map_raw",
                    # The controller's own measurements — see db.METRIC_COLUMNS.
                    # mms_vehicle_speed_kmh is the SPEED source now that its
                    # decode is fixed; it drives the derived "Speed (km/h)"
                    # column rather than appearing under its raw name.
                    # mms_estimated_soc_percent stays excluded; see _XLSX_COLS.
                    "mms_vehicle_speed_kmh",
                    "mms_measured_voltage_V", "mms_current_A", "mms_trip_m",
                    # Throttle: the percentage, the raw mV it was derived from,
                    # and the zone the driver was shown. All three, because the
                    # percentage alone cannot be re-derived once the placeholder
                    # calibration in efficiency.py is replaced by a measured one.
                    "mms_throttle_percent", "mms_throttle_mv",
                    "mms_throttle_zone"],
    "Temperature": ["battery_temp_C",
                    "bms_temp_1_C", "bms_temp_2_C", "bms_temp_3_C",
                    "bms2_temp_1_C", "bms2_temp_2_C", "bms2_temp_3_C"],
    "Motion / GPS": ["odometer_m", "calculated_lap", "lap_distance_m",
                     "lat", "lon", "target_speed_kmh"],
    # The last_lap_* columns feed the Laps sheet (one row per lap), not the
    # Data sheet: per row they only repeat the previous lap's figure.
    "Laps / Energy": ["total_race_energy", "last_lap_energy", "last_lap_time_s",
                      "regen_energy", "last_lap_regen_energy",
                      "last_lap_distance_m", "active_strategy",
                      "stint_energy", "stint_regen_energy"],
    "Errors / Faults": ["bms_has_error", "bms_error_code", "bms_protections",
                        "mms_has_error", "mms_error_code", "mms_alerts"],
}


def metrics_for_groups(groups):
    """Expand a list of group names into their underlying columns,
    preserving EXPORT_COLUMNS order and dropping duplicates."""
    wanted = set()
    for g in groups or []:
        wanted.update(METRIC_GROUPS.get(g, []))
    return [m for m in db.EXPORT_COLUMNS if m in wanted]


def _iso(ts):
    """Local time, ISO-8601, WITH the offset: 2026-08-20T15:12:25+03:00.

    Still ISO and still machine-readable, but in the timezone the team was in
    when the sample was recorded (see pit_config.export_local). The offset is
    what keeps it unambiguous across the mid-season Tel Aviv -> Brussels switch,
    so a reader never has to know which side of the switch a row came from.

    The CSV also carries device_ts_epoch beside this, which stays a plain UTC
    epoch - the anchor for anything that needs to compute rather than read.
    """
    if ts is None:
        return ""
    return pit_config.export_local(ts).isoformat()


def _excel_dt(ts):
    """`ts` as a NAIVE local datetime, for a real Excel date cell.

    Naive because openpyxl refuses a timezone-aware datetime outright - Excel
    has no timezone type, so the workbook carries no zone of its own: every
    time cell is pit-local for the moment it was recorded (pit_config
    .export_local), which is what the team reads a race log in. The CSV export
    is the one that keeps the offset per row (see `_iso`) for a reader that has
    to be sure across the mid-season Tel Aviv -> Brussels switch.

    A real datetime rather than a string so Excel sorts, filters and charts it
    as a time instead of as text, which the old UTC ISO string could not do.
    """
    local = pit_config.export_local(ts)
    return None if local is None else local.replace(tzinfo=None)


def _parse_time(value):
    """Accept a unix epoch (e.g. 1718700000) or an ISO-8601 string."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _resolve_metrics(metrics):
    """Validate the requested columns, defaulting to all of them."""
    if not metrics:
        return list(db.EXPORT_COLUMNS)
    unknown = [m for m in metrics if m not in db.EXPORT_COLUMNS]
    if unknown:
        raise ValueError(
            f"unknown metric(s): {', '.join(unknown)}. "
            f"Valid metrics: {', '.join(db.EXPORT_COLUMNS)}"
        )
    return list(metrics)


def write_csv(fileobj, start_ts=None, end_ts=None, metrics=None,
              device_id=DEVICE_ID, conn=None) -> int:
    """Write filtered rows to an open text file object. Returns the row count."""
    metrics = _resolve_metrics(metrics)
    own_conn = conn is None
    if own_conn:
        conn = db.get_conn()
    try:
        rows = db.fetch_samples(conn, start_ts=start_ts, end_ts=end_ts, device_id=device_id)
        header = _BASE_COLUMNS + metrics
        writer = csv.writer(fileobj)
        writer.writerow(header)
        for r in rows:
            writer.writerow(
                [r["rtdb_key"], r["device_id"], r["device_ts"], _iso(r["device_ts"]), r["ingested_ts"]]
                + [r[m] for m in metrics]
            )
        return len(rows)
    finally:
        if own_conn:
            conn.close()


def to_csv_bytes(start_ts=None, end_ts=None, metrics=None, device_id=DEVICE_ID) -> bytes:
    """Return the CSV as UTF-8 bytes — convenient for a caller that wants it in memory."""
    buf = io.StringIO()
    write_csv(buf, start_ts=start_ts, end_ts=end_ts, metrics=metrics, device_id=device_id)
    return buf.getvalue().encode("utf-8")


# ============================================================================
# HISTORY-TAB CSV — one range, the metrics you charted, nothing else
# ============================================================================
# These build from the DataFrame the History chart actually drew, NOT from a
# fresh query. `to_csv_bytes` above re-reads SQLite, which by now can hold rows
# the chart never showed — so a file built that way could quietly disagree with
# the picture it came from. Same frame in, same numbers out.

def _csv_num(v, places=3):
    """Format one metric cell.

    A missing reading (None/NaN) becomes an EMPTY CELL. This matters: the pit
    used to coalesce absent readings to 0, so a telemetry dropout exported as a
    real "0 °C" and got averaged in. An empty cell is the honest version, and
    both Excel and pandas treat it as missing rather than as a value."""
    if v is None or v != v:          # NaN is the only value not equal to itself
        return ""
    try:
        return f"{float(v):.{places}f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return _safe(str(v))


def _finite(df, column):
    """Present, non-NaN values of a column — [] if the column isn't there."""
    if column not in df:
        return []
    return [v for v in df[column] if v is not None and v == v]


def _history_report_lines(df, charts, session, device_id):
    """The `#` header block, so the file still explains itself months later.

    Kept `#`-prefixed and ahead of the single header row, which keeps it
    readable with pandas' `read_csv(..., comment="#")`."""
    t0, t1 = df["Time"].iloc[0], df["Time"].iloc[-1]
    now = datetime.now().astimezone()
    lines = ["Solar Race - Pit Telemetry Export (History tab)"]
    if session:
        lines.append(f"Session:   {_safe(str(session))}")
    lines += [
        f"Device:    {device_id}",
        f"Exported:  {now:%Y-%m-%d %H:%M:%S %z}",
        f"Range:     {t0:%Y-%m-%d %H:%M:%S} -> {t1:%Y-%m-%d %H:%M:%S}",
        f"Timezone:  pit-local, UTC{now:%z}",
        f"Samples:   {len(df):,} (full resolution, not downsampled)",
    ]
    # Race context for the window, derived from columns already in the frame.
    # Written whichever metrics were charted, because it describes the RANGE.
    laps = _finite(df, "Lap")
    if laps:
        lines.append(f"Laps:      {int(min(laps))} -> {int(max(laps))}")
    dist = _finite(df, "Distance")
    if len(dist) > 1:
        lines.append(f"Distance:  {max(dist) - min(dist):.2f} km in this range")
    energy = _finite(df, "Energy")
    if len(energy) > 1:
        lines.append(f"Energy:    {max(energy) - min(energy):.0f} Wh in this range")
    lines.append("Metrics:   " + ", ".join(f"{lbl} ({unit})"
                                           for _c, lbl, unit, _k in charts))
    lines += [
        "Note:      an empty cell means the car never reported that reading.",
        "           It is NOT a zero — skip those rows when averaging.",
    ]
    return lines


def history_csv_bytes(df, charts, style="data", session="", device_id=DEVICE_ID) -> bytes:
    """CSV for one History range.

    `charts` is the dashboard's (column, label, unit, color) list — only those
    metrics are written, in that order. `style` is "data" (bare table, opens
    straight into Excel/Sheets) or "report" (the same table behind a documented
    `#` header block).

    Columns are `Time`, `Elapsed (s)`, then one per metric with its unit in the
    header. Time is pit-local in a form Excel parses as a datetime; Elapsed lets
    you plot against time without doing any date maths.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    headers = ["Time", "Elapsed (s)"] + [f"{lbl} ({unit})"
                                         for _c, lbl, unit, _k in charts]
    if df is None or df.empty:
        writer.writerow(headers)
        return buf.getvalue().encode("utf-8-sig")

    if style == "report":
        for line in _history_report_lines(df, charts, session, device_id):
            buf.write(f"# {line}\n")
        buf.write("#\n")

    writer.writerow(headers)
    times = list(df["Time"])
    t0 = times[0]
    columns = [list(df[col]) for col, *_rest in charts]
    for i, when in enumerate(times):
        writer.writerow(
            [when.strftime("%Y-%m-%d %H:%M:%S"),
             f"{(when - t0).total_seconds():.1f}"]
            + [_csv_num(values[i]) for values in columns]
        )
    # utf-8-SIG, not plain utf-8: without the BOM Excel guesses the codepage and
    # mangles the unit symbols in the headers (°C, Ω) into mojibake. pandas and
    # Sheets both read the BOM form transparently.
    return buf.getvalue().encode("utf-8-sig")


# ============================================================================
# EXCEL (.xlsx) export — clean Data sheet + history-graph Charts sheet + Faults
# ============================================================================
# Clean, human-readable columns for the workbook. Maps an output key ->
# (header, number_format, unit, chart_color_hex). `device_ts_iso` is the time
# axis; `speed_kmh` and `distance_km` are derived (see _cell_value). Order here
# is the display order on the Data sheet.
_XLSX_COLS = {
    "device_ts_iso":     ("Time (local)",  "yyyy-mm-dd hh:mm:ss", None, None),
    # Time since the race start (Start race / corrected start time). Blank when
    # no race start is set or the row is before it.
    "race_time":         ("Race Time",           "[h]:mm:ss", None,  None),
    "speed_kmh":         ("Speed (km/h)",        "0.0",       "km/h", "00FFCC"),
    "mms_rpm":           ("Motor RPM",           "0",         "rpm",  "9B59B6"),
    "mms_power_W":       ("Motor Power (W)",     "0",         "W",    "00B3FF"),
    # This is the CONTROLLER's own temperature (byte 4 of the temp frame), not
    # the motor's — it was labelled "Motor Temp" before the motor actually had
    # a sensor, which would now collide with the real one two rows down.
    "mms_temperature_C": ("Controller Temp (°C)", "0.0",  "°C", "E74C3C"),
    "mms_motor_temp_C":  ("Motor Temp (°C)",      "0.0",  "°C", "FF5E5E"),
    # Map name is text (no chart colour); the raw value charts as a step trace.
    "mms_motor_map":     ("Power Map",            None,   None, None),
    "mms_motor_map_raw": ("Power Map (raw)",      "0",    None, "58D68D"),
    "bms_soc_percent":   ("Battery SoC (%)",     "0.0",       "%",    "F1C40F"),
    # BOTH voltage sources, each labelled with where it came from. They disagree
    # (~50 V from the controller vs ~113 V from the BMS for the same pack); the
    # controller matches the cell count and the JBD decode is a known open bug.
    # An export that quietly picked one would destroy the evidence.
    "mms_measured_voltage_V": ("Pack Voltage ctrl (V)", "0.00", "V", "16A085"),
    "bms_voltage_V":     ("Pack Voltage BMS (V)", "0.00",     "V",    "2ECC71"),
    "bms_current_A":     ("Battery Current BMS (A)", "0.00",  "A",    "E67E22"),
    "mms_current_A":     ("Motor Current ctrl (A)", "0.00",   "A",    "D35400"),
    # Battery B's BMS. The three bms_ columns above are battery A's. No chart
    # colour, like the BMS B probes below: the Data sheet holds them.
    "bms2_soc_percent":  ("Battery B SoC (%)",   "0.0",       "%",    None),
    "bms2_voltage_V":    ("Pack Voltage BMS B (V)", "0.00",   "V",    None),
    "bms2_current_A":    ("Battery Current BMS B (A)", "0.00", "A",   None),
    # mms_vehicle_speed_kmh is no longer excluded — its decode is fixed. It used
    # to peak at 6583 against an mms_rpm peak of 6352, with 2,569 rows over
    # 200 km/h, because the raw field was read as km/h when it is really
    # 0.1 km/h WITHOUT the gear reduction applied (mms_parser
    # .decode_vehicle_speed_kmh has the measurement). It now feeds the
    # "Speed (km/h)" column above and is not exported again under its raw name.
    #
    # ⚠️ Rows recorded before that fix still hold the raw value and will export
    # as ~50x too fast. Back-fill before exporting a race that spans the change.
    #
    # NOT exported, deliberately, though it is captured in telemetry.db:
    #   mms_estimated_soc_percent  — 0 in all 33,972 rows; the controller never
    #     populates it, so it is not the independent SoC cross-check it looked like.
    "target_speed_kmh":  ("Target Speed (km/h)", "0.0",       "km/h", "85C1E9"),
    "regen_energy":      ("Regen Energy (Wh)",   "0.0",       "Wh",   "A9DFBF"),
    "mms_trip_m":        ("Controller Trip (m)", "0",         "m",    None),
    "battery_temp_C":    ("Battery Temp (°C)", "0.0",    "°C", "FF9900"),
    # The BMS units' own NTC probes. No chart colour: six more charts would
    # slow every export for readings the Data sheet already holds.
    "bms_temp_1_C": ("BMS A T1 (°C)", "0.0", "°C", None),
    "bms_temp_2_C": ("BMS A T2 (°C)", "0.0", "°C", None),
    "bms_temp_3_C": ("BMS A T3 (°C)", "0.0", "°C", None),
    "bms2_temp_1_C": ("BMS B T1 (°C)", "0.0", "°C", None),
    "bms2_temp_2_C": ("BMS B T2 (°C)", "0.0", "°C", None),
    "bms2_temp_3_C": ("BMS B T3 (°C)", "0.0", "°C", None),
    "distance_km":       ("Distance (km)",       "0.000",     "km",   "1ABC9C"),
    "calculated_lap":    ("Lap",                 "0",         "#",    "7F8C9B"),
    # Distance into the lap being driven (resets at the line).
    "lap_distance_m":    ("Lap Distance (m)",    "0",         "m",    None),
    "total_race_energy": ("Total Energy (Wh)",   "0.0",       "Wh",   "58D68D"),
    # Stint pair — same green family as the other energy columns so they read
    # as one group. Per-lap energy/time live on the Laps sheet.
    # Since the last detected charging stop (charge_detector.py on the car),
    # NOT since the last lap — see LapTracker.mark_stint_start. Rows from
    # before this feature existed export blank, same as any other None.
    "stint_energy":      ("Current Stint Energy (Wh)", "0.0", "Wh", "27AE60"),
    "stint_regen_energy": ("Current Stint Regen Energy (Wh)", "0.0", "Wh", "76D7C4"),
    "active_strategy":   ("Speed Profile",       None,        None,   None),
    "lat":               ("Latitude",            "0.000000",  None,  None),
    "lon":               ("Longitude",           "0.000000",  None,  None),
}

# The six fault columns (the "Errors / Faults" group). If any are selected we
# emit a dedicated Faults sheet instead of six mostly-empty per-row columns.
_FAULT_COLUMNS = set(METRIC_GROUPS["Errors / Faults"])

# Cap the number of points a chart series references so the workbook stays
# snappy on a long race (the Data sheet still keeps every row).
_MAX_CHART_POINTS = 2000


# Data-sheet keys that are not a raw store column, and what gates them. Every
# other _XLSX_COLS key IS a raw column and is emitted when it was selected, so a
# column added to _XLSX_COLS can never again be silently left out of the file
# (five were, when this was a hand-kept list that had to be updated twice).
_DERIVED_SOURCE = {
    # Speed is the controller's field, NOT a function of RPM.
    "speed_kmh": "mms_vehicle_speed_kmh",
    "distance_km": "odometer_m",
}
_ALWAYS = ("device_ts_iso",)
# Lap is read out of the second column, where the Zone column used to sit: it
# is what the team looks for first when reading a row, so it leads the metrics
# instead of sitting among the Motion/GPS ones. Only when it was selected - an
# export without the lap metric gets no empty column.
_LEAD = "calculated_lap"


def _data_columns(metrics, race_start=None):
    """Ordered output keys for the Data sheet, derived from the selected raw
    metrics. Time always leads, then Lap when it was selected; Race Time
    follows whenever a race start is known."""
    m = set(metrics)
    cols = list(_ALWAYS)
    if _LEAD in m:
        cols.append(_LEAD)
    if race_start:
        cols.append("race_time")
    for k in _XLSX_COLS:
        if k in _ALWAYS or k in (_LEAD, "race_time"):
            continue
        if _DERIVED_SOURCE.get(k, k) in m:
            cols.append(k)
    return cols


def _race_duration(ts, race_start):
    """`ts` as an Excel duration (days) since the race start, or None."""
    if not race_start or ts is None or ts < race_start:
        return None
    return (ts - race_start) / 86400.0


def _cell_value(key, r, race_start=None):
    """Value for one Data-sheet cell from a telemetry row."""
    if key == "race_time":
        return _race_duration(r["device_ts"], race_start)
    if key == "device_ts_iso":
        return _excel_dt(r["device_ts"])
    if key == "speed_kmh":
        # Straight from the controller's decoded speed field. No `or 0`: a row
        # the car never reported speed for stays None -> an empty cell, not a
        # confident 0 km/h averaged into the stint statistics.
        return r["mms_vehicle_speed_kmh"]
    if key == "distance_km":
        # Missing stays missing: an empty cell, never a 0 km odometer.
        odo = r["odometer_m"]
        return None if odo is None else odo / 1000.0
    return r[key]


def _safe(v):
    """Neutralize CSV/Excel formula injection: a text cell that starts with
    = + - @ is prefixed with an apostrophe so Excel treats it as text."""
    if isinstance(v, str) and v and v[0] in ("=", "+", "-", "@"):
        return "'" + v
    return v


def _style_header(ws, ncols, nrows, header_row=1):
    """Paint, freeze and filter a header row.

    `header_row` is 1 everywhere except the per-lap sheets, whose data table
    starts below the lap's own summary block -- the freeze then keeps both the
    summary and the column names on screen while the samples scroll.
    """
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    fill = PatternFill("solid", fgColor="1F3A5F")
    font = Font(bold=True, color="FFFFFF")
    for c in range(1, ncols + 1):
        cell = ws.cell(row=header_row, column=c)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="center")
    ws.freeze_panes = "A%d" % (header_row + 1)
    if nrows:
        ws.auto_filter.ref = (f"A{header_row}:"
                              f"{get_column_letter(ncols)}{header_row + nrows}")


# Selecting any of the "Laps / Energy" columns adds the per-lap sheet.
_LAP_COLUMNS = set(METRIC_GROUPS["Laps / Energy"])


# NO LAP BUILDER LIVES HERE ANY MORE.
#
# There used to be a `_laps_from_rows` that rebuilt the lap list out of the
# exported rows, keyed on (calculated_lap, time, energy, distance). It agreed
# with the pit's per-lap charts right up until the pit corrected the lap count:
# set_lap moves the car's lap_count and deliberately leaves lap_seq alone, so
# the rows carrying one finished lap's figures split across two values of
# calculated_lap and the workbook listed that lap TWICE -- 26 rows against the
# chart's 25 bars, with the duplicate quietly dragged through the Average row.
#
# db.fetch_laps() groups on lap_seq, which the pit cannot set, and it is what
# /api/laps serves to the History charts. The workbook now asks the same
# function for the same laps, bounded to the export window. Two builders is
# how the two views came to disagree; one builder is the fix.


def _mean(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


# ONE DEFINITION OF A LAP'S SUMMARY ROW, used by the Laps sheet and by the
# header on each lap's own sheet in the per-lap workbook. They are the same
# thirteen facts about the same lap, and two copies of this list is how the
# index and the tab it points at come to disagree about lap 12's energy.
#
# Driver is the SECOND column, beside the lap number: "who drove lap 12" is the
# question this sheet gets asked after the race, and an answer eight columns to
# the right of the lap is one nobody reads. Empty when no stint covered that
# lap — an unlogged lap is not driven by nobody, it is a lap the pit never
# recorded a name for, and a blank cell says so.
_LAP_HEADERS = ["Lap", "Driver", "Finished (local)", "Race Time", "Lap Time",
                "Energy (Wh)", "Regen (Wh)", "Distance (m)", "Avg Speed (km/h)",
                "Kind", "Stood still (s)", "Cut by", "Flags"]
_LAP_FORMATS = [None, None, "yyyy-mm-dd hh:mm:ss", "[h]:mm:ss", "[m]:ss.000",
                "0.0", "0.0", "0", "0.0", None, "0", None, None]


def _as_duration(sec):
    """Seconds as an Excel duration (a fraction of a day)."""
    return None if sec is None else sec / 86400.0


def _lap_summary_row(lap, race_start):
    """One lap as the _LAP_HEADERS row describes it."""
    t, d = lap["lap_time_s"], lap["distance_m"]
    avg = (d / 1000.0) / (t / 3600.0) if t and d is not None else None
    ts = lap["finished_ts"]
    return [lap["lap"], lap.get("driver"), _excel_dt(ts),
            _race_duration(ts, race_start), _as_duration(t),
            lap["energy_wh"], lap["regen_wh"], d, avg,
            lap.get("kind"), lap.get("stopped_s"), lap.get("lap_source"),
            # fetch_laps hands back a list; the cell wants one string.
            ", ".join(lap.get("flags") or []) or None]


def _write_laps_sheet(ls, laps, race_start):
    """Laps sheet: one row per lap, then Best and Average rows.
    Missing figures stay empty cells and are left out of the averages."""
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    headers, formats = _LAP_HEADERS, _LAP_FORMATS
    ls.append(headers)
    as_duration = _as_duration

    for lap in laps:
        ls.append(_lap_summary_row(lap, race_start))
    if not laps:
        ls.append(["No lap completed in this window."] + [None] * (len(headers) - 1))
    else:
        # Best and Average describe RACING laps. Once the car tags its laps,
        # an in-lap (it holds the pit stop) or an out-lap must not drag them;
        # a window with no tags at all is from an older car and is averaged
        # whole, as it always was. Same rule as db.flying_laps().
        tagged = any(l.get("kind") for l in laps)
        pool = [l for l in laps if l.get("kind") == "flying"] if tagged else laps
        label = " (flying laps)" if tagged else ""
        times = [l["lap_time_s"] for l in pool if l["lap_time_s"]]
        ls.append([])
        ls.append(["Best" + label, None, None, None,
                   as_duration(min(times) if times else None),
                   None, None, None, None])
        ls.append(["Average" + label, None, None, None,
                   as_duration(_mean(l["lap_time_s"] for l in pool)),
                   _mean(l["energy_wh"] for l in pool),
                   _mean(l["regen_wh"] for l in pool),
                   _mean(l["distance_m"] for l in pool), None])
        for row in ls.iter_rows(min_row=ls.max_row - 1, max_row=ls.max_row):
            row[0].font = Font(bold=True)

    for row in ls.iter_rows(min_row=2, max_row=ls.max_row):
        for cell, nf in zip(row, formats):
            if nf:
                cell.number_format = nf
    for i, h in enumerate(headers, start=1):
        ls.column_dimensions[get_column_letter(i)].width = max(12, len(h) + 2)
    _style_header(ls, len(headers), max(1, len(laps)))


# --------------------------------------------------------------------------- #
# The per-lap workbook: the Laps sheet as an index, then a sheet per lap.
#
# A SEPARATE FUNCTION, NOT A MODE OF write_xlsx(). The workbook above is cut by
# TIME and answers "what was the car doing between 14:00 and 15:00"; this one is
# cut by LAP and answers "show me lap 87". They share the column definitions,
# the lap summary row and the header styling, and nothing else -- write_xlsx is
# untouched and keeps serving the button it always served.
# --------------------------------------------------------------------------- #

# How many lap sheets one workbook may be asked for. A 24 h race is 250-300
# laps, so this is "the whole race, and not a typo for the whole race twice":
# the caller is refused rather than silently handed a truncated export, because
# a workbook quietly missing laps 120 upward is worse than one that did not
# open at all. Excel has no sheet limit beyond memory; openpyxl's per-sheet
# overhead is what makes a thousand of them a bad idea.
MAX_LAP_SHEETS = 300

# How far past the lap's own measured time a window may stretch before it is
# refused and rebuilt from lap_time_s. See _lap_window.
_LAP_WINDOW_SLACK = 1.5


def _lap_window(laps, i):
    """(t0, t1) -- the instants lap `laps[i]` was driven between, or None.

    THIS IS THE OFF-BY-ONE, AND IT IS NOT OPTIONAL READING. db.fetch_laps
    defines `finished_ts` as the first row that CARRIED the finished lap's
    figures -- the car holds them constant through the whole of the next lap --
    so that row is the first row of the lap AFTER this one. Two things follow,
    and they are the whole of this function:

      t1  is this lap's finished_ts, and this lap's samples are the ones
          strictly BEFORE it.
      t0  is the PREVIOUS lap's finished_ts, which by the same rule is the
          first row of this lap.

    Confirmed against a store rather than argued from the comment: over the 28
    laps of demo_telemetry.db every window holds samples whose calculated_lap
    is exactly one below the lap number (`calculated_lap` counts laps
    COMPLETED, so the trace of lap L is tagged L-1), and a 212 s lap comes out
    at 419 samples against the 0.5 s push interval. tools/check_lap_export.py
    re-proves both at runtime instead of trusting this paragraph.

    THE FALLBACK. With no previous lap in the store, or with a gap between the
    two far longer than the lap actually took -- a second session, or a spell
    when the pit heard nothing -- the car's own measured lap_time_s is used
    instead. A window is never allowed to stretch across a hole and collect
    another evening's driving into lap 3.
    """
    lap = laps[i]
    t1 = lap.get("finished_ts")
    if t1 is None:
        return None
    measured = lap.get("lap_time_s")
    t0 = laps[i - 1].get("finished_ts") if i > 0 else None
    if t0 is None or (measured and t1 - t0 > measured * _LAP_WINDOW_SLACK):
        t0 = None if not measured else t1 - measured
    return None if t0 is None or t0 >= t1 else (t0, t1)


def _lap_sheet_title(wb, lap):
    """A unique sheet name for a lap: "Lap 42", or "Lap 42 (2)" if it repeats.

    Lap numbers CAN repeat in one store -- the pit can set the car's lap number,
    and a car whose checkpoint was wiped starts counting again. Excel refuses a
    duplicate sheet name outright, so a workbook that would be fine on 364 days
    of the year must not fail on the one race where somebody corrected the
    count.
    """
    base = "Lap %s" % ("?" if lap.get("lap") is None else lap["lap"])
    title, n = base, 1
    while title in wb.sheetnames:
        n += 1
        title = "%s (%d)" % (base, n)
    return title


def _write_lap_sheet(ws, lap, rows, cols, race_start):
    """One lap: its summary in a header block, then every sample it holds."""
    from openpyxl.utils import get_column_letter

    # The lap's own figures, as the SAME row the Laps sheet shows -- plus the
    # sample count, which is this sheet's own fact and belongs nowhere else.
    ws.append(_LAP_HEADERS + ["Samples"])
    ws.append(_lap_summary_row(lap, race_start) + [len(rows)])
    for cell, nf in zip(ws[2], _LAP_FORMATS):
        if nf:
            cell.number_format = nf
    ws.append([])

    header_row = 4
    # "Laps completed", NOT "Lap", and only on these sheets. The raw column is
    # calculated_lap, which counts laps FINISHED -- so every sample on the tab
    # headed "Lap 7" carries a 6, and the two sitting one row apart is an
    # invitation to conclude the export is off by one. It is not: 6 laps were
    # complete while lap 7 was being driven. The Data sheet of the time-ranged
    # workbook is not touched; there is no lap-numbered tab beside it to
    # contradict.
    ws.append(["Laps completed" if k == _LEAD else _XLSX_COLS[k][0]
               for k in cols])
    for r in rows:
        ws.append([_cell_value(k, r, race_start) for k in cols])
    if not rows:
        # Never a silently empty sheet. A lap with no samples is a lap the pit
        # did not hear, which is a fact about the race worth reading on the tab
        # that was opened to look for it.
        ws.append(["No samples stored for this lap -- the pit heard nothing "
                   "between its start and its finish."])

    numfmts = {i: _XLSX_COLS[k][1] for i, k in enumerate(cols, start=1)
               if _XLSX_COLS[k][1]}
    if numfmts and rows:
        for row_cells in ws.iter_rows(min_row=header_row + 1,
                                      max_row=header_row + len(rows)):
            for i, nf in numfmts.items():
                row_cells[i - 1].number_format = nf
    for i, k in enumerate(cols, start=1):
        head = len(_LAP_HEADERS[i - 1]) + 2 if i <= len(_LAP_HEADERS) else 0
        name = "Laps completed" if k == _LEAD else _XLSX_COLS[k][0]
        ws.column_dimensions[get_column_letter(i)].width = max(
            12, len(name) + 2, head)
    # Both header rows are painted; the freeze is on the data one, so the lap's
    # summary stays in view above its samples.
    _style_header(ws, len(_LAP_HEADERS) + 1, 0, header_row=1)
    _style_header(ws, len(cols), len(rows), header_row=header_row)


def write_laps_xlsx(fileobj_or_path, first_lap=None, last_lap=None,
                    metrics=None, device_id=DEVICE_ID, conn=None):
    """Workbook with a sheet per lap. Returns (lap count, total sample rows).

    `first_lap` / `last_lap` bound the LAP NUMBERS, inclusive; None means the
    end of the store on that side. The numbering and the summary figures are
    db.fetch_laps() -- the same call the Laps sheet, /api/laps and the History
    charts all make -- so a tab labelled "Lap 87" holds the lap the rest of the
    pit calls 87, and no second lap builder exists anywhere.

    Raises ValueError when the range names more sheets than MAX_LAP_SHEETS, or
    when it selects no lap at all.
    """
    from openpyxl import Workbook

    metrics = _resolve_metrics(metrics)
    own_conn = conn is None
    if own_conn:
        conn = db.get_conn()
    try:
        race_start = db.load_race_state(conn).get("race_start_time")
        # FROM THE GREEN FLAG, like /api/laps and for the same reason: a race
        # start zeroes the car's counter, so a warm-up lap 1 and a race lap 1
        # would be two tabs fighting over one name. The TIME-ranged workbook
        # above is deliberately not bounded this way -- it exports the window
        # it was asked for, warm-up and all.
        #
        # Unbounded WITHIN that, on purpose: the window of lap i is built from
        # lap i-1's finished_ts, so a list clipped to the requested lap range
        # would have no predecessor for its first lap and would fall back to
        # lap_time_s for exactly the lap most likely to be looked at. Fetch
        # every lap of the race, window them all, then select.
        laps = db.fetch_laps(conn, device_id=device_id, since_ts=race_start)
        db.attach_lap_drivers(laps, db.load_driver_stints(conn),
                              db.load_lap_driver_overrides(conn))

        wanted = [(i, l) for i, l in enumerate(laps)
                  if l.get("lap") is not None
                  and (first_lap is None or l["lap"] >= first_lap)
                  and (last_lap is None or l["lap"] <= last_lap)]
        if not wanted:
            raise ValueError("no completed lap in that range")
        if len(wanted) > MAX_LAP_SHEETS:
            raise ValueError("%d laps in that range; %d sheets is the limit. "
                             "Narrow the lap range."
                             % (len(wanted), MAX_LAP_SHEETS))

        cols = _data_columns(metrics, race_start)
        wb = Workbook()

        # The index first, listing ONLY the laps this workbook has a tab for: a
        # contents page naming laps that are not in the file sends someone
        # looking for a tab that was never written.
        index = wb.active
        index.title = "Laps"
        _write_laps_sheet(index, [l for _, l in wanted], race_start)

        total = 0
        for i, lap in wanted:
            win = _lap_window(laps, i)
            rows = []
            if win:
                t0, t1 = win
                # fetch_samples' end is INCLUSIVE and the window is half-open:
                # the row at t1 is the first row of the NEXT lap and belongs on
                # the next tab, not on this one.
                rows = db.fetch_samples(conn, start_ts=t0, end_ts=t1 - 1e-6,
                                        device_id=device_id)
            total += len(rows)
            _write_lap_sheet(wb.create_sheet(_lap_sheet_title(wb, lap)),
                             lap, rows, cols, race_start)
    finally:
        if own_conn:
            conn.close()

    wb.save(fileobj_or_path)
    return len(wanted), total


def lap_bounds(device_id=DEVICE_ID, conn=None):
    """(first lap, last lap, how many) for the export panel's two fields.

    Counts the same laps write_laps_xlsx will write -- from the green flag when
    a race has been started -- so the range the panel offers is the range it
    can deliver.
    """
    own_conn = conn is None
    if own_conn:
        conn = db.get_conn()
    try:
        race_start = db.load_race_state(conn).get("race_start_time")
        laps = [l["lap"] for l in db.fetch_laps(conn, device_id=device_id,
                                                since_ts=race_start)
                if l.get("lap") is not None]
    finally:
        if own_conn:
            conn.close()
    return (min(laps), max(laps), len(laps)) if laps else (None, None, 0)


def write_xlsx(fileobj_or_path, start_ts=None, end_ts=None, metrics=None,
               device_id=DEVICE_ID, conn=None) -> int:
    """Write a formatted Excel workbook (Data + Laps + Charts + Faults). Returns the
    Data row count. `fileobj_or_path` may be a path or a binary file object."""
    from openpyxl import Workbook
    from openpyxl.chart import LineChart, Reference
    from openpyxl.chart.shapes import GraphicalProperties
    from openpyxl.drawing.line import LineProperties
    from openpyxl.utils import get_column_letter

    metrics = _resolve_metrics(metrics)
    own_conn = conn is None
    if own_conn:
        conn = db.get_conn()
    try:
        rows = db.fetch_samples(conn, start_ts=start_ts, end_ts=end_ts, device_id=device_id)
        race_start = db.load_race_state(conn).get("race_start_time")
        include_faults = any(m in _FAULT_COLUMNS for m in metrics)
        include_laps = any(m in _LAP_COLUMNS for m in metrics)
        # The SAME call /api/laps makes, over this export's window, so the Laps
        # sheet and the History per-lap charts number their laps identically.
        laps = db.fetch_laps(conn, device_id=device_id,
                             since_ts=start_ts, until_ts=end_ts) if include_laps else []
        # Who drove each lap, from the stints the pit logged. Same two calls
        # /api/laps makes, so the Driver column and the dashboard's per-lap
        # table cannot credit one lap to two different people.
        if include_laps:
            db.attach_lap_drivers(laps, db.load_driver_stints(conn),
                              db.load_lap_driver_overrides(conn))
        fault_rows = []
        if include_faults:
            # limit=None: the default keeps only the newest 2000 fault rows,
            # which silently dropped the start of a long race's window.
            for r in db.fetch_faults(conn, limit=None, device_id=device_id):
                ts = r["device_ts"] or 0
                if (start_ts is None or ts >= start_ts) and (end_ts is None or ts <= end_ts):
                    fault_rows.append(r)
    finally:
        if own_conn:
            conn.close()

    nrows = len(rows)
    cols = _data_columns(metrics, race_start)

    wb = Workbook()

    # --- Data sheet ------------------------------------------------------- #
    ws = wb.active
    ws.title = "Data"
    ws.append([_XLSX_COLS[k][0] for k in cols])
    for r in rows:
        ws.append([_cell_value(k, r, race_start) for k in cols])
    # number formats + column widths
    numfmts = {i: _XLSX_COLS[k][1] for i, k in enumerate(cols, start=1) if _XLSX_COLS[k][1]}
    if numfmts:
        for row_cells in ws.iter_rows(min_row=2, max_row=nrows + 1):
            for i, nf in numfmts.items():
                row_cells[i - 1].number_format = nf
    for i, k in enumerate(cols, start=1):
        ws.column_dimensions[get_column_letter(i)].width = max(12, len(_XLSX_COLS[k][0]) + 2)
    _style_header(ws, len(cols), nrows)

    # --- Laps sheet --------------------------------------------------------- #
    if include_laps:
        _write_laps_sheet(wb.create_sheet("Laps"), laps, race_start)

    # --- Charts sheet (from a hidden, downsampled data block) ------------- #
    chart_keys = [k for k in cols
                  if k not in ("device_ts_iso", "lat", "lon")
                  and _XLSX_COLS[k][3]]
    if nrows and chart_keys:
        step = max(1, math.ceil(nrows / _MAX_CHART_POINTS))
        sampled = rows[::step]
        m = len(sampled)

        cd = wb.create_sheet("_chartdata")
        cd.sheet_state = "hidden"
        cd.append(["Time"] + [_XLSX_COLS[k][0] for k in chart_keys])
        for r in sampled:
            cd.append([_excel_dt(r["device_ts"])]
                      + [_cell_value(k, r, race_start) for k in chart_keys])

        charts = wb.create_sheet("Charts")
        cats = Reference(cd, min_col=1, min_row=2, max_row=m + 1)
        for idx, k in enumerate(chart_keys):
            chart = LineChart()
            chart.title = _XLSX_COLS[k][0]
            chart.y_axis.title = _XLSX_COLS[k][2] or ""
            chart.x_axis.title = "Time (local)"
            chart.x_axis.delete = False
            chart.y_axis.delete = False
            chart.height = 7.5
            chart.width = 16
            chart.legend = None
            chart.x_axis.tickLblSkip = max(1, m // 8)
            chart.x_axis.tickMarkSkip = max(1, m // 8)
            data_ref = Reference(cd, min_col=2 + idx, min_row=1, max_row=m + 1)
            chart.add_data(data_ref, titles_from_data=True)
            chart.set_categories(cats)
            series = chart.series[0]
            series.graphicalProperties = GraphicalProperties()
            series.graphicalProperties.line = LineProperties(solidFill=_XLSX_COLS[k][3], w=28000)
            series.smooth = False
            anchor = f"{'A' if idx % 2 == 0 else 'J'}{1 + (idx // 2) * 16}"
            charts.add_chart(chart, anchor)

    # --- Faults sheet ----------------------------------------------------- #
    if include_faults:
        fs = wb.create_sheet("Faults")
        fheaders = ["Time (local)", "BMS error code", "BMS protections",
                    "Motor error code", "Motor alerts"]
        fs.append(fheaders)
        if fault_rows:
            for r in fault_rows:
                fs.append([
                    _excel_dt(r["device_ts"]),
                    r["bms_error_code"],
                    _safe(r["bms_protections"]),
                    r["mms_error_code"],
                    _safe(r["mms_alerts"]),
                ])
        else:
            fs.append(["No faults recorded in this window.", None, None, None, None])
        for i, h in enumerate(fheaders, start=1):
            fs.column_dimensions[get_column_letter(i)].width = max(16, len(h) + 2)
        _style_header(fs, len(fheaders), max(1, len(fault_rows)))

    wb.save(fileobj_or_path)
    return nrows


def to_xlsx_bytes(start_ts=None, end_ts=None, metrics=None, device_id=DEVICE_ID):
    """Return (xlsx_bytes, row_count) — for a caller that wants the file in memory."""
    buf = io.BytesIO()
    n = write_xlsx(buf, start_ts=start_ts, end_ts=end_ts, metrics=metrics, device_id=device_id)
    return buf.getvalue(), n


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Export stored telemetry history. Output format follows the "
                    "--out extension: .xlsx -> formatted Excel workbook (Data + "
                    "Laps + Charts + Faults), anything else -> raw CSV (stdout if omitted).")
    p.add_argument("--out", "-o", help="output path; .xlsx for Excel, else CSV (default: stdout CSV)")
    p.add_argument("--device", default=DEVICE_ID, help=f"device id (default: {DEVICE_ID})")
    p.add_argument("--metric", "-m", action="append",
                   help="metric column to include (repeatable; default: all)")
    p.add_argument("--group", "-g", action="append",
                   help=f"subsystem group to include (repeatable). Choices: {', '.join(METRIC_GROUPS)}")
    p.add_argument("--start", help="start time: unix epoch or ISO-8601 (inclusive)")
    p.add_argument("--end", help="end time: unix epoch or ISO-8601 (inclusive)")
    p.add_argument("--list-metrics", action="store_true", help="list valid metric names and exit")
    args = p.parse_args(argv)

    if args.list_metrics:
        print("metrics:\n  " + "\n  ".join(db.EXPORT_COLUMNS))
        print("groups:\n  " + "\n  ".join(METRIC_GROUPS))
        return 0

    try:
        start_ts = _parse_time(args.start)
        end_ts = _parse_time(args.end)
        # Combine explicit metrics with any requested groups; empty == all.
        selected = list(args.metric or []) + metrics_for_groups(args.group)
        metrics = _resolve_metrics(selected or None)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.out:
        if args.out.lower().endswith(".xlsx"):
            n = write_xlsx(args.out, start_ts, end_ts, metrics, args.device)
        else:
            with open(args.out, "w", newline="", encoding="utf-8") as f:
                n = write_csv(f, start_ts, end_ts, metrics, args.device)
        print(f"wrote {n} row(s) to {args.out}")
    else:
        n = write_csv(sys.stdout, start_ts, end_ts, metrics, args.device)
        print(f"# {n} row(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
