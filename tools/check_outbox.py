#!/usr/bin/env python3
"""
check_outbox.py - the car's telemetry outbox, with no network and no car
========================================================================
    python tools/check_outbox.py

Drives SolarRace_OS/cloud/firebase_client.py against a fake firebase_admin.db
and a throwaway outbox file, and exits non-zero if a sample could be lost,
reordered, or skipped by the pit. Takes about 20 seconds: part of what it
checks is how the upload retries behave while the link is down.

WHY THIS IS WORTH A TOOL. Before the outbox, a sample taken while the link was
down was simply gone, and the upload ran on the CAN worker thread, so a slow
link stalled frame decoding (the car's lap energy read 5-20% low on drives
with link trouble). The outbox fixes both, but it can fail in ways that look
fine at the bench and only bite on the circuit:

  * the pit collector resumes from the NEWEST key it has (orderBy $key,
    startAt). A backlog uploaded under keys that sort below that key is never
    fetched: no error anywhere, just a hole in the pit's record. Cases A, C
    and E are that test, including a Pi clock an hour behind after a hard
    power-off.
  * an upload that blocks the caller brings the CAN-thread stall back. Case B.
  * a backlog must survive the car being switched off. Case D.
"""

import os
import sys
import tempfile
import time
import types

_ROOT = os.environ.get("SOLARRACE_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.abspath(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "SolarRace_OS"))
print("checking: %s" % _ROOT)

FAILURES = []


def check(name, ok, detail=""):
    print("  %-58s %s" % (name, "OK" if ok else "FAIL"))
    if not ok:
        FAILURES.append(name + ((" - " + detail) if detail else ""))
    elif detail:
        print("      %s" % detail)


class FakeRTDB:
    """Flat {path: value} store. `up` and `delay` simulate the cellular link."""

    def __init__(self):
        self.data = {}
        self.up = True
        self.delay = 0.0

    def net(self):
        if self.delay:
            time.sleep(self.delay)
        if not self.up:
            raise ConnectionError("fake link down")


RTDB = FakeRTDB()


class FakeRef:
    """The three calls firebase_client makes: set, and the newest-key query."""

    def __init__(self, path):
        self.path, self.last = path, None

    def order_by_key(self):
        return self

    def limit_to_last(self, n):
        self.last = n
        return self

    def get(self):
        RTDB.net()
        prefix = self.path + "/"
        keys = sorted(k[len(prefix):] for k in RTDB.data if k.startswith(prefix))
        return {k: RTDB.data[prefix + k] for k in keys[-self.last:]}

    def set(self, value):
        RTDB.net()
        RTDB.data[self.path] = value

    def push(self, value):
        raise AssertionError("push() must not be used while the outbox works")


import firebase_admin                                            # noqa: E402

fake_db = types.ModuleType("firebase_admin.db")
fake_db.reference = lambda path="/": FakeRef(path.strip("/"))
firebase_admin.db = fake_db
sys.modules["firebase_admin.db"] = fake_db

import cloud.firebase_client as fc                               # noqa: E402
from edge_sync.queue import _encode_time                         # noqa: E402

fc.OUTBOX_PATH = os.path.join(tempfile.mkdtemp(), "telemetry_outbox.db")


def history_keys():
    return sorted(k.split("/", 1)[1] for k in RTDB.data
                  if k.startswith(fc.HISTORY_PATH + "/"))


def recorded_order(keys):
    return [RTDB.data["%s/%s" % (fc.HISTORY_PATH, k)]["car_data"]["motor"]["i"]
            for k in keys]


def wait_for(cond, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def sample(i):
    fc._last_update_time = 0          # step past the 0.5 s throttle
    fc.push_telemetry_to_cloud({"motor": {"i": i, "mms_power_W": float("nan")},
                                "battery": {"bms_soc_percent": 80}})


# Firebase already holds a key from an hour AHEAD of this machine's clock: the
# Pi booting behind after a hard power-off, seen from the other side.
future_key = _encode_time(int((time.time() + 3600) * 1000)) + "-" * 12
RTDB.data["%s/%s" % (fc.HISTORY_PATH, future_key)] = {"timestamp": 0, "car_data": {}}

print("A. link down for 40 samples, then back")
RTDB.up = False
for i in range(40):
    sample(i)
time.sleep(2.5)
status = fc.get_upload_status()
check("nothing uploaded while the link is down", len(history_keys()) == 1)
check("PIT badge goes down", status["upload_status"] == fc.STATUS_DOWN,
      status["upload_status"])
check("backlog reported to the HUD", status["upload_backlog"] == 40,
      str(status["upload_backlog"]))
time.sleep(6)                         # long enough for the retry wait to hit its cap
RTDB.up = True
t_up = time.time()
check("whole backlog uploads", wait_for(lambda: len(history_keys()) == 41),
      "%d keys" % len(history_keys()))
check("upload resumes within the retry cap",
      time.time() - t_up < fc.OUTBOX_MAX_BACKOFF_S + 1.5,
      "%.1f s after the link came back" % (time.time() - t_up))
ours = [k for k in history_keys() if k != future_key]
check("keys sort in the order the car recorded", recorded_order(ours) == list(range(40)))
check("NaN uploaded as null",
      RTDB.data["%s/%s" % (fc.HISTORY_PATH, ours[0])]["car_data"]["motor"]["mms_power_W"] is None)
check("live node holds the newest sample",
      RTDB.data[fc.LIVE_PATH]["car_data"]["motor"]["i"] == 39)
check("PIT badge back up", fc.get_upload_status()["upload_status"] == fc.STATUS_UP)

print("C. Pi clock an hour behind what Firebase already holds")
check("every new key sorts above the existing newest key",
      all(k > future_key for k in ours))

print("E. pit collector resuming from its newest key")
check("startAt that key covers all 40 samples",
      len([k for k in history_keys() if k > future_key]) == 40)

print("B. slow link (1.5 s a write)")
RTDB.delay = 1.5
t0 = time.perf_counter()
for i in range(40, 50):
    sample(i)
spent = time.perf_counter() - t0
check("recording 10 samples does not wait on the link", spent < 0.5,
      "%.0f ms" % (spent * 1000))
RTDB.delay = 0.0
check("slow-link samples upload", wait_for(lambda: len(history_keys()) == 51, 60),
      "%d keys" % len(history_keys()))

print("D. car switched off with a backlog, then on again")
RTDB.up = False
for i in range(50, 60):
    sample(i)
fc.stop_telemetry_uploader(flush_timeout=0.5)
fc._outbox_reconciled = False         # what a fresh process starts with
RTDB.up = True
sample(60)
check("backlog from before the restart uploads",
      wait_for(lambda: len(history_keys()) == 62, 30), "%d keys" % len(history_keys()))
ours = [k for k in history_keys() if k != future_key]
check("still in recorded order", recorded_order(ours) == list(range(61)))
fc.stop_telemetry_uploader()

print()
if FAILURES:
    print("FAILED (%d):" % len(FAILURES))
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("All outbox checks passed.")
