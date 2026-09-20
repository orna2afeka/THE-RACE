#!/usr/bin/env python3
"""
check_charge_curve.py - the charging curve, re-measured from the car's own charges
==================================================================================
    python tools/check_charge_curve.py
    python tools/check_charge_curve.py --db path\\to\\telemetry.db
    python tools/check_charge_curve.py --spell 2        # just that one, in detail

strategy_engine.CHARGING_CURVE decides how long a charge takes, and a charge
that takes longer than planned is laps the strategy promised and the car
cannot drive. The curve is no longer a model: it was measured from the first
race charge (2026-09-19, 16% -> 81% in 55.3 min). THIS re-derives it from
whatever charges are in the pit store and holds the curve to them.

WHAT IT DOES
  1. Finds every charging spell in the store: a run of is_charging = 1 with no
     gap longer than the pit's own CHARGE_SPELL_GAP_S, long enough and far
     enough to say anything (over 5 min and over 5 SoC points).
  2. Integrates V*I over BOTH packs across the spell -- the energy the charger
     actually delivered -- and divides by the SoC gained, which is a
     measurement of the pack as well as of the charger.
  3. Times it against the curve. The curve must reproduce a real charge to
     within TOLERANCE_PCT, or it is describing a different car.
  4. Prints the measured power band by band beside the curve, so a charge that
     tapers somewhere new is visible before it costs a plan.

WHAT "MODEL-SIDE kW" MEANS, because the two columns differ on purpose
The charger's kW is V*I at the terminals. The engine's kW is what moves the
SoC on a BATTERY_FULL_WH pack: one SoC point is 90 Wh of a 9000 Wh pack, so
the model-side figure is 90 Wh over the minutes that point took. The gap
between them is charging loss (~3% on the first charge). CHARGING_CURVE holds
MODEL-SIDE kW, because what a plan needs to predict is the clock.

READ-ONLY. It opens the store read-only and writes nothing, so it is safe to
run against the live pit store mid-race.
"""

import argparse
import datetime
import os
import sqlite3
import statistics
import sys

_ROOT = os.environ.get("SOLARRACE_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "Pit_Dashboard")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import strategy_engine as se                                     # noqa: E402

# A run of is_charging = 1 ends at a silence this long: the car was off or out
# of contact, and what came before may be a different plug-in. The same number
# the pit's own charge clock uses (api.CHARGE_SPELL_GAP_S), named here so this
# tool does not have to import the web app.
SPELL_GAP_S = 120.0
# Under this, a spell says nothing useful about a curve: the SoC is reported in
# whole percent, so a short charge is one or two quantised steps.
MIN_SPELL_MIN = 5.0
MIN_SPELL_PCT = 5.0
# How far the curve may be from a real charge before this fails. A charge is
# measured through an integer SoC readout at both ends, so a point of
# quantisation on a 30-point charge is already 3%.
TOLERANCE_PCT = 10.0
# Per-band, where a single band is a handful of samples and the quantisation
# bites harder. A band this far out is printed as a warning, not a failure --
# what fails is the whole charge's clock above.
BAND_WARN_PCT = 20.0

FAILED = []


def fail(msg):
    FAILED.append(msg)
    print("  ** FAIL ** " + msg)


def spells(conn):
    """Every charging spell in the store, newest last, as lists of samples."""
    rows = conn.execute(
        "SELECT device_ts, bms_soc_percent, bms_voltage_V, bms_current_A, "
        "       bms2_voltage_V, bms2_current_A "
        "FROM telemetry WHERE is_charging = 1 ORDER BY device_ts").fetchall()
    out, run, prev = [], [], None
    for r in rows:
        if prev is not None and r[0] - prev > SPELL_GAP_S:
            out.append(run)
            run = []
        run.append(r)
        prev = r[0]
    if run:
        out.append(run)
    return out


def analyse(run):
    """One spell as numbers: duration, SoC span, energy in, and per-point times.

    A sample missing its voltage or current is dropped from the ENERGY sum
    only -- a gap in the CAN report is not zero power, and treating it as zero
    would quietly under-read the charger. The clock is taken from the
    timestamps, which are always there.
    """
    soc = [(r[0], r[1]) for r in run if r[1] is not None]
    if len(soc) < 2:
        return None
    minutes = (soc[-1][0] - soc[0][0]) / 60.0
    lo, hi = soc[0][1], soc[-1][1]

    wh = 0.0
    usable = [r for r in run
              if None not in (r[2], r[3], r[4], r[5])]
    for a, b in zip(usable, usable[1:]):
        dt = b[0] - a[0]
        if dt <= 0 or dt > SPELL_GAP_S:
            continue
        pa = a[2] * a[3] + a[4] * a[5]
        pb = b[2] * b[3] + b[4] * b[5]
        wh += (pa + pb) / 2.0 * dt / 3600.0

    # When each whole-percent step was first seen, which is what times a point.
    first = {}
    for ts, s in soc:
        first.setdefault(int(s), ts)
    steps = sorted(first)
    per_point = {a: (first[b] - first[a]) / 60.0
                 for a, b in zip(steps, steps[1:])}
    # The first step is partial -- the spell started part way through it.
    per_point.pop(steps[0], None)

    return {"start": soc[0][0], "minutes": minutes, "from_pct": lo, "to_pct": hi,
            "charger_wh": wh, "per_point": per_point,
            "samples": len(run), "energy_samples": len(usable)}


def model_kw(minutes_per_point):
    """The kW that moves one SoC point of the modelled pack in that long."""
    wh = se.BATTERY_FULL_WH / 100.0
    return (wh / (minutes_per_point / 60.0)) / 1000.0 if minutes_per_point else None


def report(n, a, detail=False):
    when = datetime.datetime.fromtimestamp(a["start"]).strftime("%Y-%m-%d %H:%M")
    gained = a["to_pct"] - a["from_pct"]
    print("\nspell %d - %s: %.0f%% -> %.0f%% in %.1f min (%d samples)"
          % (n, when, a["from_pct"], a["to_pct"], a["minutes"], a["samples"]))
    if gained < MIN_SPELL_PCT or a["minutes"] < MIN_SPELL_MIN:
        print("    too short to say anything about the curve - skipped")
        return False

    avg_kw = a["charger_wh"] / (a["minutes"] / 60.0) / 1000.0 if a["minutes"] else 0.0
    implied = a["charger_wh"] / gained * 100.0
    print("    charger delivered %.0f Wh (%.2f kW average over the spell)"
          % (a["charger_wh"], avg_kw))
    print("    which implies a %.0f Wh pack at 100%%; BATTERY_FULL_WH is %.0f"
          % (implied, se.BATTERY_FULL_WH))
    # The charger always puts in MORE than the model counts: the difference is
    # loss. A pack constant ABOVE the measured energy would mean the model
    # thinks more energy is in there than the charger ever delivered.
    if not (0.85 * implied <= se.BATTERY_FULL_WH <= implied):
        fail("spell %d: a %.0f Wh pack against %.0f Wh measured in"
             % (n, se.BATTERY_FULL_WH, implied))

    modelled = se.charging_time_min(a["from_pct"], a["to_pct"])
    off = (modelled - a["minutes"]) / a["minutes"] * 100.0
    print("    the curve plans this charge in %.1f min, the car took %.1f (%+.1f%%)"
          % (modelled, a["minutes"], off))
    if abs(off) > TOLERANCE_PCT:
        fail("spell %d: the curve is %+.1f%% out over %.0f%%-%.0f%% - re-fit "
             "CHARGING_CURVE from this charge"
             % (n, off, a["from_pct"], a["to_pct"]))

    print("    measured against the curve, by band:")
    print("      band      minutes   measured kW   curve kW")
    bands = {}
    for s, m in a["per_point"].items():
        bands.setdefault(int(s // 5) * 5, []).append(m)
    for band in sorted(bands):
        mins = sum(bands[band])
        got = model_kw(statistics.mean(bands[band]))
        want = se.get_charging_power(band + 2.5)
        flag = ""
        if got and abs(got - want) / want * 100.0 > BAND_WARN_PCT:
            flag = "   <- %+.0f%% off the curve" % ((got - want) / want * 100.0)
        print("      %2d-%2d%%   %6.1f      %6.2f        %6.2f%s"
              % (band, band + 5, mins, got, want, flag))
        if detail:
            for s in sorted(x for x in a["per_point"] if int(x // 5) * 5 == band):
                print("          %3d%%  %5.2f min  %5.2f kW"
                      % (s, a["per_point"][s], model_kw(a["per_point"][s])))
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="store to read (default: the pit's)")
    ap.add_argument("--spell", type=int, default=None,
                    help="only this spell, per SoC point")
    args = ap.parse_args()

    path = args.db or str(se.os.environ.get("SOLARRACE_DB_PATH") or "")
    if not path:
        import db as pit_db
        path = str(pit_db.SQLITE_PATH)
    if not os.path.exists(path):
        print("no store at %s" % path)
        return 0
    print("store: %s" % path)
    print("curve: measured %g-%g%%, from %s"
          % (se.CHARGING_CURVE_MEASURED_PCT[0], se.CHARGING_CURVE_MEASURED_PCT[1],
             se.MEASURED_CHARGE["note"]))

    conn = sqlite3.connect("file:%s?mode=ro" % path.replace("?", "%3f"), uri=True)
    try:
        runs = spells(conn)
    finally:
        conn.close()

    if not runs:
        # NOT a failure. A store with no charge in it is the normal state
        # before the first stop, and a check that cries wolf then is a check
        # the crew learns to skip.
        print("\nno charging spell in this store yet - nothing to check the "
              "curve against. It stands on %s." % se.MEASURED_CHARGE["note"])
        return 0

    looked = 0
    for i, run in enumerate(runs, 1):
        a = analyse(run)
        if a is None:
            continue
        if args.spell and args.spell != i:
            continue
        looked += report(i, a, detail=bool(args.spell))

    if not looked:
        print("\nevery spell in the store is too short to say anything.")
        return 0
    if FAILED:
        print("\n%d FAILED:" % len(FAILED))
        for f in FAILED:
            print("  - %s" % f)
        return 1
    print("\nThe curve matches every charge in the store, to within %.0f%%."
          % TOLERANCE_PCT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
