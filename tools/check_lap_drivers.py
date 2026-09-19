#!/usr/bin/env python3
"""
check_lap_drivers.py - who drove a lap: a name counts from now, and the pit can fix the past
============================================================================================
    python tools/check_lap_drivers.py

Drives the stint and lap-driver endpoints against a throwaway store, with the
write to Firebase stubbed. No car, no network.

THE MISTAKE THIS EXISTS FOR, replayed below press for press. 2026-09-19, 19:37:
Ido had driven three hours. The pit typed the incoming driver, "Amit", pressed
"Name current driver" and then "Driver changed". The first press overwrote the
one name Ido's stint had, so eighteen laps Ido drove were credited to Amit --
in the lap table, in the Laps sheet, on every tab of the per-lap workbook.

Two things changed, and each is checked:

  FROM NOW   naming a stint that already has a name does not rename the laps
             already driven. The old name keeps every lap that finished before
             the press; the new one takes the laps that finish after it.
  FIRST NAME a stint nobody has named yet still takes its first name for the
             WHOLE of it: that is driver one, started by the green flag, and
             there is nobody else those laps could belong to.
  UNDO       undoing a driver change brings the names back with the stint.
  BY HAND    the pit can set the driver of a lap, or a run of laps, from the
             team's list -- and clear the edit to go back to the stint log.
  LISTED     only names on constants.DRIVERS are accepted. A typed "ido" and a
             picked "Ido" are two drivers in a pivot table.
  EXPORT     the edit is the name in the Excel workbooks too, because they and
             the table read it through the same db.attach_lap_drivers.
  BY SEQ     an edit is filed by lap_seq, not lap number: the pit can set the
             number, and that race had two lap 61s.
  THIS RACE  an edit from one race does not name a lap of the next.
"""

import os
import sys
import tempfile
import time
import warnings

warnings.filterwarnings("ignore")

_ROOT = os.environ.get("SOLARRACE_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
for p in (_ROOT, os.path.join(_ROOT, "Pit_Dashboard")):
    if p not in sys.path:
        sys.path.insert(0, p)

FAILED = []


def check(label, ok, detail=""):
    print("  %-54s %s" % (label, "OK  " if ok else "FAIL"))
    if detail:
        print("       " + detail)
    if not ok:
        FAILED.append("%s - %s" % (label, detail))
    return ok


def main():
    import db
    import constants as C
    store = os.path.join(tempfile.mkdtemp(prefix="lapdrivers_"), "t.db")
    os.environ["SOLARRACE_DB_PATH"] = store
    conn = db.get_conn(store)
    db.init_db(conn)

    from contextlib import closing
    from fastapi.testclient import TestClient
    from Pit_Web import api
    import driver_message
    driver_message.publish_driver_name = lambda *a, **k: None
    c = TestClient(api.app)

    # A clock the test owns: every server stamp comes from here.
    clock = [1789560000.0]
    real_time = time.time
    api.time.time = lambda: clock[0]

    def at(seconds):
        clock[0] = 1789560000.0 + seconds

    def stints():
        with closing(api.ro_conn()) as ro:
            return [(s["driver"], s["started_at"] - 1789560000.0,
                     None if s["ended_at"] is None else s["ended_at"] - 1789560000.0)
                    for s in db.load_driver_stints(ro)]

    def who(seconds):
        with closing(api.ro_conn()) as ro:
            return db.driver_at(db.load_driver_stints(ro), 1789560000.0 + seconds)

    print("\nwho drove a lap: a name counts from now, and the pit can fix the past\n")

    # ---- the green flag starts stint one, unnamed -----------------------------
    at(0)
    c.post("/api/race", json={"isRacing": True})
    at(600)
    c.post("/api/driver_stint/name", json={"driver": "Ido"})
    check("FIRST NAME: an unnamed stint takes its name for all of it",
          who(30) == "Ido" and who(599) == "Ido",
          "lap at +30 s -> %s (named at +600 s)" % who(30))

    # ---- 19:37, replayed: name the INCOMING driver, then change ---------------
    at(10800)                                           # three hours in
    c.post("/api/driver_stint/name", json={"driver": "Amit"})
    at(10805)
    c.post("/api/driver_stint", json={"driver": "Amit"})
    check("FROM NOW: Ido keeps the three hours he drove",
          who(3600) == "Ido" and who(10799) == "Ido",
          "lap at +1 h -> %s, lap at +2:59:59 -> %s" % (who(3600), who(10799)))
    check("          and Amit has the laps from the press on",
          who(10802) == "Amit" and who(12000) == "Amit",
          "lap at +3:00:02 -> %s" % who(10802))
    check("          the log shows both names on that stint",
          [s[0] for s in stints()] == ["Ido", "Amit", "Amit"],
          "%s" % [s[0] for s in stints()])

    # ---- undo brings the names back with the stint -----------------------------
    at(10900)
    c.post("/api/driver_stint/undo")
    check("UNDO: the restored stint still knows both names",
          who(3600) == "Ido" and who(10850) == "Amit",
          "+1 h -> %s, +3:00:50 -> %s" % (who(3600), who(10850)))
    at(10905)
    c.post("/api/driver_stint", json={"driver": "Amit"})   # and change again

    api.time.time = real_time

    # ---- laps, to edit ---------------------------------------------------------
    cols = ["device_id", "rtdb_key", "device_ts", "ingested_ts", "calculated_lap",
            "lap_seq", "last_lap_number", "last_lap_time_s", "last_lap_energy",
            "last_lap_distance_m", "last_lap_kind", "lap_source"]
    sql = "INSERT INTO telemetry (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols)))
    # seq 1..4; laps 3 and 4 BOTH carry the number 61 (a count correction).
    for seq, number, t in [(1, 59, 2000), (2, 60, 2300), (3, 61, 2600), (4, 61, 2900)]:
        ts = 1789560000.0 + t
        conn.execute(sql, ["solarcar", "-K%d" % seq, ts, ts, float(number), float(seq),
                           float(number), 280.0 + seq, 100.0 + seq, 4000.0, "flying", "gps"])
    conn.commit()

    def laps():
        return c.get("/api/laps").json()["laps"]

    ls = laps()
    check("the table starts from the stint log",
          [l["driver"] for l in ls] == ["Ido"] * 4 and not any(l["driverEdited"] for l in ls),
          "%s" % [(l["lap"], l["driver"]) for l in ls])

    key = {l["key"]: l for l in ls}
    k3 = [l["key"] for l in ls][2]
    r = c.post("/api/laps/driver", json={"keys": [k3], "driver": "Tal"})
    ls = laps()
    check("BY HAND: one lap can be given to another driver",
          r.status_code == 200 and ls[2]["driver"] == "Tal" and ls[2]["driverEdited"],
          "%s" % [(l["lap"], l["driver"]) for l in ls])
    check("BY SEQ:  the OTHER lap 61 is untouched",
          ls[3]["lap"] == 61 and ls[3]["driver"] == "Ido" and not ls[3]["driverEdited"],
          "two laps numbered 61: %s and %s" % (ls[2]["driver"], ls[3]["driver"]))

    r = c.post("/api/laps/driver", json={"keys": [l["key"] for l in ls[:2]], "driver": "Guy"})
    check("         a run of laps is one request",
          [l["driver"] for l in laps()] == ["Guy", "Guy", "Tal", "Ido"],
          "%s" % [l["driver"] for l in laps()])

    r = c.post("/api/laps/driver", json={"keys": [k3], "driver": "ido"})
    check("LISTED:  a name off the team's list is refused",
          r.status_code == 400 and laps()[2]["driver"] == "Tal",
          "%s - list is %s" % (r.status_code, C.DRIVERS))

    # EXPORT: the workbook reads the same answer.
    import export
    from openpyxl import load_workbook
    out = os.path.join(os.path.dirname(store), "laps.xlsx")
    with closing(api.ro_conn()) as ro:
        export.write_xlsx(out, conn=ro)
    sheet = load_workbook(out)["Laps"]
    names = [row[1].value for row in sheet.iter_rows(min_row=2, max_row=5)]
    check("EXPORT:  the Laps sheet carries the edits",
          names == ["Guy", "Guy", "Tal", "Ido"], "Driver column %s" % names)

    c.post("/api/laps/driver", json={"keys": [k3], "driver": ""})
    ls = laps()
    check("         clearing an edit goes back to the stint log",
          ls[2]["driver"] == "Ido" and not ls[2]["driverEdited"],
          "lap 61 -> %s" % ls[2]["driver"])

    db.save_race_state(conn, True, real_time())           # a new race
    with closing(api.ro_conn()) as ro:
        left = db.load_lap_driver_overrides(ro)
    check("THIS RACE: a new race starts with no edits",
          left == {}, "%d carried over" % len(left))
    conn.close()

    if FAILED:
        print("\n%d FAILED:" % len(FAILED))
        for f in FAILED:
            print("  - %s" % f)
        return 1
    print("\nAll lap-driver checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
