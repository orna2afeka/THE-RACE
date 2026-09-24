#!/usr/bin/env python3
"""
check_main_bms.py - "the battery" is one pack, the same one on the car and in the pit
====================================================================================
    python tools/check_main_bms.py

The car has two packs and two BMSs. Wherever ONE answer is needed -- the SoC
the strategy plans from, the driver's gauge, the current that says "charging"
-- both sides read the pack named by MAIN_BMS. This exists because pack A's
BMS froze mid-race (2026-09-20, ~01:50): 77 %, 53.7 V, +35.9 A, repeated for
over an hour. The pit planned on 18 points of charge the car did not have, and
"+35.9 A" is a CHARGE to the detector the moment the car stands still, which
the pit counts against the three the regulations allow.

  SAME     the car's setting and the pit's name the same pack.
  PIT      state["soc"/"voltage"/"current"] are that pack's columns; each pack
           is still readable under its own letter; the pit wall follows the
           setting; History stays on its indexed columns and NAMES the pack.
  CAR      the HUD gauge and the charge detector read the main pack's keys, and
           nothing in that block still reads pack A by name.
  GUARD    a car that says "charging" while the main pack's current is flowing
           OUT does not start the charge clock, and does not cost a charge. A
           car that says it while current flows IN still does, and so does one
           whose current is simply not reported.
"""
import os
import re
import sys
import tempfile
import time
import warnings

warnings.filterwarnings("ignore")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_ROOT, os.path.join(_ROOT, "Pit_Dashboard"), os.path.join(_ROOT, "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

FAILED = []


def check(label, ok, detail=""):
    print("  %-62s %s" % (label, "OK" if ok else "FAIL"))
    if detail:
        print("       " + detail)
    if not ok:
        FAILED.append(label)


def main():
    store = os.path.join(tempfile.mkdtemp(prefix="mainbms_"), "t.db")
    os.environ["SOLARRACE_DB_PATH"] = store
    import constants as C
    import db
    conn = db.get_conn(store)
    db.init_db(conn)
    db.save_race_state(conn, True, time.time() - 3600)
    from Pit_Web import api
    from contextlib import closing

    print("SAME")
    car_cfg = open(os.path.join(_ROOT, "SolarRace_OS", "config.py"), encoding="utf-8").read()
    car = re.search(r'^MAIN_BMS = "([AB])"', car_cfg, re.M)
    check("the car has the setting", car is not None)
    check("and it names the pack the pit names",
          car is not None and car.group(1) == C.MAIN_BMS,
          "car %s, pit %s" % (car and car.group(1), C.MAIN_BMS))

    print("PIT")
    soc_col, v_col, a_col = C.BMS_COLUMNS[C.MAIN_BMS]
    check("state soc / voltage / current are the main pack's columns",
          (api._STATE_COLUMNS["soc"], api._STATE_COLUMNS["voltage"],
           api._STATE_COLUMNS["current"]) == (soc_col, v_col, a_col))
    check("each pack is still there under its own letter",
          api._STATE_COLUMNS["soc_a"] == "bms_soc_percent"
          and api._STATE_COLUMNS["soc_b"] == "bms2_soc_percent"
          and api._STATE_COLUMNS["current_a"] == "bms_current_A"
          and api._STATE_COLUMNS["current_b"] == "bms2_current_A")
    import live_metrics
    fields = {e["label"]: e.get("field") for _g, es in live_metrics.LIVE_METRIC_GROUPS for e in es}
    check('the "Battery A" tiles read pack A, the "Battery B" tiles pack B',
          fields["Battery A SoC"] == "state.soc_a" and fields["Battery B SoC"] == "state.soc_b"
          and fields["Battery A Current"] == "state.current_a")
    import metrics
    named = [m for v in vars(metrics).values() if isinstance(v, (list, tuple))
             for m in v if isinstance(m, metrics.Metric)]
    by_key = {m.key: m for m in named}
    # History cannot follow the setting until idx_telemetry_chart carries pack
    # B's columns (see metrics.py). What it must NOT do meanwhile is call pack
    # A "the battery": whichever pack it charts, the label names it.
    for key in ("SoC", "Current"):
        m = by_key.get(key)
        pack = "B" if m is not None and m.source.startswith("bms2_") else "A"
        check("History's %s chart names the pack it draws (%s)" % (key, pack),
              m is not None and ("Battery %s" % pack) in m.label,
              "%r from %s" % (m and m.label, m and m.source))
    check("and every charted column is on the fast index",
          all(m.source in db.CHART_COLUMNS for m in named),
          str([m.source for m in named if m.source not in db.CHART_COLUMNS]))
    import pit_wall
    check("the pit wall's battery keys read the main pack's columns",
          pit_wall.FIELD_SOURCE == {"bms_soc_percent": soc_col, "bms_voltage_V": v_col,
                                    "bms_current_A": a_col})

    print("CAR")
    car_main = open(os.path.join(_ROOT, "SolarRace_OS", "main.py"), encoding="utf-8").read()
    i = car_main.index('_main = "bms2_" if MAIN_BMS == "B" else "bms_"')
    block = car_main[i:car_main.index("# Faults are tracked PER PACK", i)]
    check("the HUD gauge and the charge detector read the main pack's keys",
          'bms_data[_main + "soc_percent"]' in block
          and 'self._last_bms_current_A = float(bms_data[_main + "current_A"])' in block)
    check("and nothing there still reads pack A by name",
          'bms_data["bms_soc_percent"]' not in block and 'bms_data["bms_current_A"]' not in block)

    print("GUARD")
    k = [0]

    def sample(charging, main_amps, other_amps=35.9, speed=0.0):
        k[0] += 1
        ts = time.time() - 0.5
        a, b = (other_amps, main_amps) if C.MAIN_BMS == "B" else (main_amps, other_amps)
        conn.execute(
            "INSERT INTO telemetry (device_id, rtdb_key, device_ts, ingested_ts, "
            " is_charging, mms_vehicle_speed_kmh, calculated_lap, bms_current_A, "
            " bms2_current_A, bms_soc_percent, bms2_soc_percent) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (db.DEVICE_ID, "-K%012d%03d" % (int(ts * 1000), k[0]), ts, time.time(),
             charging, speed, 10, a, b, 77.0, 57.0))
        conn.commit()

    def clock():
        with closing(api.ro_conn()) as ro:
            return api.charge_clock(ro)

    # The failure itself: the frozen pack says +35.9 A, the car is stopped for
    # a driver change and reports "charging", the main pack is idle or draining.
    sample(charging=1, main_amps=-0.4)
    api.charge_watch_once()
    check("frozen pack says charging, main pack is draining: no clock, no count",
          clock()["active"] is False and clock()["count"] == 0,
          "active %s, count %d" % (clock()["active"], clock()["count"]))
    conn.execute("DELETE FROM telemetry"); conn.commit()
    sample(charging=1, main_amps=36.0)
    api.charge_watch_once()
    check("a real charge (current INTO the main pack) still starts and counts",
          clock()["active"] is True and clock()["count"] == 1)
    from fastapi.testclient import TestClient
    TestClient(api.app).post("/api/charge", json={"action": "discard"})
    conn.execute("DELETE FROM telemetry"); conn.commit()
    sample(charging=1, main_amps=None)
    api.charge_watch_once()
    check("a current that is simply not reported is not a 'no'",
          clock()["active"] is True)
    conn.close()

    if FAILED:
        print("\n%d FAILED: %s" % (len(FAILED), FAILED))
        return 1
    print("\nAll main-BMS checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
