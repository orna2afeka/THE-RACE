"""
cell_extremes.py — rule 3.5.6: the pack's cell extremes over the last 2 hours
=============================================================================
The regulations make the team report, every 2 hours during the 24 h race:

    highest cell temperature · lowest cell temperature
    highest cell voltage     · lowest cell voltage

This module keeps those four over a ROLLING 2-hour window, with which cell set
each one and when. It is shared by the car (the HUD's report screen) and the
pit, the same way limits.py is, so both apply the identical rules about which
readings count.

    python cell_extremes.py        # self-check on a fake clock

WHY NOT JUST KEEP A RUNNING MAX
Comparing each new reading against the current maximum is cheap, but a running
maximum cannot FORGET. When the hottest reading turns 2 h 01 min old it has to
leave the window, and at that moment nothing knows what the second-hottest was.
So some history is unavoidable; the trick is to keep very little of it.

HOW: ONE BUCKET PER MINUTE
    * 120 buckets cover 2 hours. Each holds the four extremes seen in its minute.
    * A new reading is compared against the CURRENT minute's bucket only —
      a couple of comparisons, however long the race has been running.
    * When the minute rolls over, the oldest bucket is dropped and the extremes
      of the closed buckets are recomputed once: ~120 x 4 comparisons, once a
      minute.
    * result() merges that cached answer with the current bucket: 4 comparisons.

Memory is 120 small records forever. The window edge is accurate to one bucket:
a value up to 2 h 01 min old can still be counted, never one that is younger
than 2 h and missed — the safe direction for a safety report.

TIME
Buckets are keyed by a MONOTONIC clock. The Pi has no battery-backed clock and
its wall time can jump by years when NTP first syncs; ageing the window on that
clock would silently empty or freeze the report. Wall time is only recorded for
display ("at 14:32") and for the save file.

READINGS THAT MUST NEVER BE REPORTED
    * A failed thermistor reads a nonsense negative (limits.plausible_cell_temp).
    * A BMS voltage tap beyond the wired cell count can decode to a literal
      0.000 V. Reported, that is "lowest cell voltage 0.00 V" to the officials.
Both are dropped before they reach a bucket.
"""

import json
import os
import time

import limits

WINDOW_S = 2 * 3600
BUCKET_S = 60

# A reading outside this is a bad frame or an unwired tap, not a cell. Wide on
# purpose: an over-discharged cell at 2.6 V is exactly what the report exists
# to catch, so only values no Li-ion cell can physically hold are dropped.
CELL_V_PLAUSIBLE_MIN = 2.0
CELL_V_PLAUSIBLE_MAX = 4.6

KEYS = ("temp_max", "temp_min", "volt_max", "volt_min")


def plausible_cell_voltage(volts, cell=None, string_count=None):
    """The reading, or None if it cannot be a real cell voltage."""
    if volts is None:
        return None
    try:
        volts = float(volts)
    except (TypeError, ValueError):
        return None
    if string_count is not None and cell is not None and int(cell) > int(string_count):
        return None
    if not (CELL_V_PLAUSIBLE_MIN <= volts <= CELL_V_PLAUSIBLE_MAX):
        return None
    return volts


def _better(key, new, old):
    """True if reading `new` should replace `old` for this extreme."""
    if old is None:
        return True
    return new[0] > old[0] if key.endswith("_max") else new[0] < old[0]


class RollingExtremes:
    """The four rule-3.5.6 extremes over a rolling window. Not thread-safe:
    on the car it lives on the CAN worker thread with everything it reads."""

    def __init__(self, window_s=WINDOW_S, bucket_s=BUCKET_S,
                 mono=time.monotonic, wall=time.time):
        self.window_s = float(window_s)
        self.bucket_s = float(bucket_s)
        self.n_buckets = int(round(self.window_s / self.bucket_s))
        self._mono = mono
        self._wall = wall
        self._closed = []            # [(key, {extreme: (value, cell, wall_ts)})]
        self._closed_best = {}       # cached extremes over self._closed
        self._cur_key = None
        self._cur = {}
        self._first_mono = None      # earliest moment the window has data for

    # ------------------------------------------------------------------ #
    def _key_now(self):
        return int(self._mono() // self.bucket_s)

    def _roll(self):
        """Close the current bucket if its minute is over, and age the window."""
        key = self._key_now()
        if self._cur_key is None:
            self._cur_key = key
            return
        if key == self._cur_key:
            return
        if self._cur:
            self._closed.append((self._cur_key, self._cur))
        self._cur_key, self._cur = key, {}
        # 120 CLOSED buckets plus the current one. With only 119 closed, a
        # reading 1 h 59 min 30 s old could already be gone at the top of a
        # minute; this way the window is never shorter than 2 h, and at most
        # one minute longer — the safe direction for a safety report.
        oldest = key - self.n_buckets
        self._closed = [(k, b) for k, b in self._closed if k >= oldest]
        best = {}
        for _k, bucket in self._closed:
            for name, reading in bucket.items():
                if _better(name, reading, best.get(name)):
                    best[name] = reading
        self._closed_best = best
        if self._first_mono is not None:
            self._first_mono = max(self._first_mono, oldest * self.bucket_s)

    def _offer(self, name, value, cell):
        self._roll()
        reading = (value, int(cell), self._wall())
        if _better(name, reading, self._cur.get(name)):
            self._cur[name] = reading
        if self._first_mono is None:
            self._first_mono = self._mono()

    # ------------------------------------------------------------------ #
    def add_temp(self, cell, deg_c):
        """One cell temperature. Implausible readings are ignored."""
        v = limits.plausible_cell_temp(deg_c)
        if v is None:
            return
        self._offer("temp_max", v, cell)
        self._offer("temp_min", v, cell)

    def add_volt(self, cell, volts, string_count=None):
        """One cell voltage. Unwired taps and impossible values are ignored."""
        v = plausible_cell_voltage(volts, cell, string_count)
        if v is None:
            return
        self._offer("volt_max", v, cell)
        self._offer("volt_min", v, cell)

    def result(self):
        """{extreme: (value, cell, wall_ts) or None, covers_s, window_s}."""
        self._roll()
        out = {}
        for name in KEYS:
            best = self._closed_best.get(name)
            cur = self._cur.get(name)
            if cur is not None and _better(name, cur, best):
                best = cur
            out[name] = best
        covers = 0.0
        if self._first_mono is not None:
            covers = min(self.window_s, max(0.0, self._mono() - self._first_mono))
        out["covers_s"] = covers
        out["window_s"] = self.window_s
        return out

    # ------------------------------------------------------------------ #
    def to_dict(self):
        """Save-file form. Buckets are stored by WALL time, because monotonic
        time restarts from zero with the process."""
        self._roll()
        mono_now, wall_now = self._mono(), self._wall()

        def wall_of(key):
            return wall_now - (mono_now - key * self.bucket_s)

        buckets = [(k, b) for k, b in self._closed]
        if self._cur:
            buckets.append((self._cur_key, self._cur))
        return {
            "saved_at": wall_now,
            "bucket_s": self.bucket_s,
            "first_wall": (None if self._first_mono is None
                           else wall_now - (mono_now - self._first_mono)),
            "buckets": [{"start_wall": wall_of(k),
                         **{n: list(r) for n, r in b.items()}}
                        for k, b in buckets],
        }

    def restore(self, data):
        """Reload a save file. Never raises; returns how many buckets survived.

        A bucket is kept only if its wall time is inside the window AND not in
        the future. The second test matters on a Pi that booted before NTP: its
        wall clock may read a date long past, every saved bucket then looks
        like it is from the future, and dropping them is the honest answer —
        a report that shows less coverage beats one built on a wrong clock.
        """
        try:
            if float(data.get("bucket_s") or 0) != self.bucket_s:
                return 0
            mono_now, wall_now = self._mono(), self._wall()
            kept = []
            for b in data.get("buckets") or []:
                start = float(b["start_wall"])
                age = wall_now - start
                # + one bucket: a bucket that STARTED just over 2 h ago still
                # holds readings younger than 2 h (see _roll's window edge).
                if age < 0 or age >= self.window_s + self.bucket_s:
                    continue
                key = int((mono_now - age) // self.bucket_s)
                readings = {}
                for name in KEYS:
                    if b.get(name) is not None:
                        v, cell, ts = b[name]
                        readings[name] = (float(v), int(cell), float(ts))
                if readings:
                    kept.append((key, readings))
            kept.sort(key=lambda kb: kb[0])
            cur_key = self._key_now()
            self._closed = [(k, b) for k, b in kept if k < cur_key]
            current = [b for k, b in kept if k == cur_key]
            self._cur_key, self._cur = cur_key, (current[0] if current else {})
            best = {}
            for _k, bucket in self._closed:
                for name, reading in bucket.items():
                    if _better(name, reading, best.get(name)):
                        best[name] = reading
            self._closed_best = best
            first = data.get("first_wall")
            if kept:
                # The oldest bucket's START can be up to a minute before its
                # first reading. Coverage must never claim time nothing was
                # seen in, so the saved first-reading time wins when it lies
                # inside the kept data -- max, not min.
                earliest = kept[0][0] * self.bucket_s
                if first is not None and 0 <= wall_now - float(first) < self.window_s:
                    earliest = max(earliest, mono_now - (wall_now - float(first)))
                self._first_mono = earliest
            return len(kept)
        except Exception:
            return 0

    def save(self, path):
        """Atomic write: a power cut mid-write cannot leave a torn file."""
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh)
        os.replace(tmp, path)

    def load(self, path):
        try:
            with open(path, encoding="utf-8") as fh:
                return self.restore(json.load(fh))
        except (OSError, ValueError):
            return 0


# --------------------------------------------------------------------------- #
# Self-check:  python cell_extremes.py
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import tempfile

    clock = {"mono": 1000.0, "wall": 1_790_000_000.0}

    def mono():
        return clock["mono"]

    def wall():
        return clock["wall"]

    def advance(s):
        clock["mono"] += s
        clock["wall"] += s

    ok = True

    def want(cond, msg):
        global ok
        print(("  ok   " if cond else "  FAIL ") + msg)
        ok &= bool(cond)

    r = RollingExtremes(mono=mono, wall=wall)

    # 1. basic extremes, and the cell that set them
    for cell, t in ((1, 30.0), (2, 41.5), (3, 28.0)):
        r.add_temp(cell, t)
    for cell, v in ((1, 3.91), (2, 3.62), (3, 4.05)):
        r.add_volt(cell, v, string_count=26)
    res = r.result()
    want(res["temp_max"][:2] == (41.5, 2) and res["temp_min"][:2] == (28.0, 3),
         "temperature extremes and their cells")
    want(res["volt_max"][:2] == (4.05, 3) and res["volt_min"][:2] == (3.62, 2),
         "voltage extremes and their cells")

    # 2. readings that must never be reported
    r.add_volt(27, 0.0, string_count=26)       # unwired tap
    r.add_volt(5, 0.0, string_count=26)        # impossible value
    r.add_temp(9, -41.0)                       # failed thermistor
    res = r.result()
    want(res["volt_min"][0] == 3.62, "0.000 V taps ignored, lowest voltage unchanged")
    want(res["temp_min"][0] == 28.0, "failed-thermistor negative ignored")

    # 3. the whole point: an old maximum leaves the window
    advance(30 * 60)
    r.add_temp(4, 35.0)
    advance(95 * 60)                           # the 41.5 C reading is now 2 h 05 min old
    r.add_temp(5, 33.0)
    res = r.result()
    want(res["temp_max"][0] == 35.0,
         f"41.5 C aged out after 2 h; highest is now {res['temp_max'][0]}")
    # Within one bucket of 2 h: the current minute is still partial, and saying
    # 1 h 59 min 40 s is the honest reading of that.
    want(WINDOW_S - BUCKET_S <= res["covers_s"] <= WINDOW_S, "coverage caps at 2 h")

    # 4. still counted just inside 2 h
    r2 = RollingExtremes(mono=mono, wall=wall)
    r2.add_temp(1, 50.0)
    advance(119 * 60)
    want(r2.result()["temp_max"] is not None and r2.result()["temp_max"][0] == 50.0,
         "a reading 1 h 59 min old is still reported")

    # 4b. the edge: a reading 1 s short of 2 h old, landing at the top of a minute
    r2b = RollingExtremes(mono=mono, wall=wall)
    advance((-clock["mono"]) % BUCKET_S)       # to the start of a minute
    r2b.add_temp(1, 50.0)
    advance(WINDOW_S - 1)
    want(r2b.result()["temp_max"] is not None,
         "a reading 1 h 59 min 59 s old is still reported")
    advance(BUCKET_S + 1)
    want(r2b.result()["temp_max"] is None,
         "and it is gone once it is more than a minute past 2 h")

    # 5. coverage is honest after a short run
    r3 = RollingExtremes(mono=mono, wall=wall)
    r3.add_volt(1, 3.9, 26)
    advance(47 * 60)
    want(abs(r3.result()["covers_s"] - 47 * 60) < 1, "coverage reads 47 min, not 2 h")

    # 6. cost: a comparison per reading, not a scan
    import time as _t
    r4 = RollingExtremes(mono=mono, wall=wall)
    t0 = _t.perf_counter()
    n = 0
    for s in range(3 * 3600):                  # 3 h at 1 Hz, 52 cells each second
        advance(1)
        for c in range(1, 27):
            r4.add_volt(c, 3.6 + (c % 5) * 0.01, 26)
            r4.add_temp(c, 30.0 + (c % 7))
            n += 2
        if s % 1 == 0:
            r4.result()
    el = _t.perf_counter() - t0
    want(len(r4._closed) <= 120, f"never more than 120 closed buckets held ({len(r4._closed)})")
    print(f"       {n:,} readings + {3 * 3600:,} results in {el:.2f} s "
          f"= {el / (3 * 3600) * 1e3:.3f} ms per second of race")

    # 7. survives a restart, and refuses a wrong clock
    path = os.path.join(tempfile.gettempdir(), "_cell_extremes_selfcheck.json")
    r5 = RollingExtremes(mono=mono, wall=wall)
    r5.add_temp(3, 44.0)
    advance(10 * 60)
    r5.add_volt(7, 3.1, 26)
    r5.save(path)
    clock["mono"] = 5.0                        # process restart: monotonic resets
    r6 = RollingExtremes(mono=mono, wall=wall)
    kept = r6.load(path)
    res = r6.result()
    want(kept == 2 and res["temp_max"][:2] == (44.0, 3) and res["volt_min"][:2] == (3.1, 7),
         f"restart restores the report ({kept} buckets)")
    want(abs(res["covers_s"] - 10 * 60) < 2, f"restored coverage is 10 min ({res['covers_s']:.0f} s)")
    clock["wall"] -= 3 * 365 * 86400           # booted before NTP: clock years behind
    r7 = RollingExtremes(mono=mono, wall=wall)
    want(r7.load(path) == 0 and r7.result()["temp_max"] is None,
         "a save file from the 'future' is refused, not trusted")
    os.remove(path)

    print("\nSELF-CHECK", "PASSED" if ok else "FAILED")
    raise SystemExit(0 if ok else 1)
