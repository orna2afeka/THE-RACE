"""
check_lap_checkpoint.py — one bad checkpoint file must not cost the race its totals
==================================================================================
Zolder, 2026-09-19 21:44: the Pi app restarted, found no usable
lap_checkpoint.json, and started laps, distance and energy from zero without a
word in the log. main.py now keeps the checkpoint before the newest as
lap_checkpoint.json.prev and falls back to it.

main.py cannot be imported off the car (PyQt, python-can, the modem), so this
lifts _load_lap_checkpoint and _save_lap_checkpoint out of its source and runs
THOSE, against a real LapTracker and a temp directory. It is the shipped code
that is tested, not a copy of it.

    python tools/check_lap_checkpoint.py
"""
import ast
import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAR = os.path.join(ROOT, "SolarRace_OS")
for _p in (ROOT, CAR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.lap_tracker import LapTracker  # noqa: E402

failures = []


def check(what, ok):
    print(("  ok    " if ok else "  FAIL  ") + what)
    if not ok:
        failures.append(what)


def lifted(tmpdir):
    """A class holding main.py's two checkpoint methods, pointed at tmpdir."""
    with open(os.path.join(CAR, "main.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    wanted = {"_load_lap_checkpoint", "_save_lap_checkpoint"}
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert {f.name for f in funcs} == wanted, "main.py no longer has both methods"
    path = os.path.join(tmpdir, "lap_checkpoint.json")
    scope = {"os": os, "json": json, "time": time,
             "LAP_CHECKPOINT_PATH": path,
             "LAP_CHECKPOINT_PREV_PATH": path + ".prev",
             "LAP_CHECKPOINT_INTERVAL_S": 15.0}
    exec(compile(ast.Module(body=funcs, type_ignores=[]), "main.py", "exec"), scope)

    class Car:
        _load_lap_checkpoint = scope["_load_lap_checkpoint"]
        _save_lap_checkpoint = scope["_save_lap_checkpoint"]

        def __init__(self):
            self.laps = LapTracker()
            self._last_checkpoint_save = 0.0
            self.log = io.StringIO()
            with redirect_stdout(self.log):
                self._load_lap_checkpoint()

    return Car, path


def drive(car, wh, metres):
    car.laps.total_energy_wh = wh
    car.laps.odometer_m = metres
    car._save_lap_checkpoint(force=True)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        Car, path = lifted(tmp)

        car = Car()
        check("no checkpoint at all: starts from zero and SAYS so",
              car.laps.total_energy_wh == 0.0 and "ZERO" in car.log.getvalue())
        drive(car, 10100.0, 348000.0)
        drive(car, 10153.764, 348812.0)
        check("the save before the newest is kept", os.path.exists(path + ".prev"))

        check("a normal restart resumes from the newest",
              Car().laps.total_energy_wh == 10153.764)

        with open(path, "w") as f:                 # a power cut's zero-length file
            pass
        car = Car()
        check("newest unreadable: resumes from the one before, not from zero",
              car.laps.total_energy_wh == 10100.0 and car.laps.odometer_m == 348000.0)
        check("...and the log names the file that failed",
              "unreadable" in car.log.getvalue())

        os.remove(path)
        check("newest missing: resumes from the one before",
              Car().laps.total_energy_wh == 10100.0)

        with open(path, "w") as f:
            f.write("[1, 2]")                      # parses, is not a checkpoint
        check("newest is JSON but not a checkpoint: falls back",
              Car().laps.total_energy_wh == 10100.0)

        # The green flag: the save before it is the WARM-UP and must not return.
        car = Car()
        car.laps.new_race()
        car._save_lap_checkpoint(force=True, keep_previous=False)
        check("new race drops the previous save", not os.path.exists(path + ".prev"))
        os.remove(path)
        check("...so a lost checkpoint after the flag cannot bring the warm-up back",
              Car().laps.total_energy_wh == 0.0)

    print("\n%s" % ("ALL OK" if not failures else "%d FAILED" % len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
