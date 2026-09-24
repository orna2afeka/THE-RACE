"""
race_totals.py — what the car's running totals LOST when the car forgot them
=============================================================================
total_race_energy, regen_energy and odometer_m are counted on the car and kept
across a Pi restart by lap_checkpoint.json. When that checkpoint is not there
the car starts again from zero and says nothing: every number it publishes
afterwards is a true count -- of the race since the restart.

Zolder, 2026-09-19 21:44:41, the car parked at the start of a charge: the Pi
app restarted and came up with no checkpoint. 10153.8 Wh, 1010.2 Wh of regen
and 348.8 km went out of the totals and the pit under-read all three for the
remaining fourteen hours. (The other fourteen restarts of that race resumed
to the watt-hour.) What was lost was never in doubt: the pit had stored the
totals up to the second they vanished, and integrating mms_power_W from the
pit's own rows gives 21325 Wh against 21328 Wh with the loss added back.

So the pit adds it back, and works out what to add FROM THE STORE, not from a
number somebody typed: every fall in a total since the green flag that is too
big to be regen is a total the car forgot, and its size is what is owed.
Nothing is written. The stored rows stay what the car said; anything that
subtracts one stored total from another (this lap's energy, a lap's cost) must
keep doing so on the raw values, where the loss cancels.

tools/repair_race_totals.py writes the same correction INTO the stored rows,
for the History charts and the workbook. The two agree by construction: once
the rows are corrected there is no fall left to find, and if the car goes on
publishing its short count afterwards, the fall is at the seam and is found.
"""
import threading
import time

# A fall bigger than this between two consecutive samples is a reset, not the
# car. Energy is NET of regen and does go down, by a few Wh over a braking
# zone; the odometer never goes down at all, but a re-sent sample can be a few
# metres behind the one before it.
RESET_FALL = {"total_race_energy": 50.0, "odometer_m": 500.0}
# regen_energy is zeroed by exactly what zeroes total_race_energy (a lost
# checkpoint, reset_energy, new_race) and is NOT in idx_telemetry_chart, so it
# is not scanned: it is read at the instants the energy total fell.
FOLLOWS = {"regen_energy": "total_race_energy"}
COLUMNS = tuple(RESET_FALL) + tuple(FOLLOWS)

# How long an answer is served before the store is read again. The scan is
# index-only (both scanned columns ride in idx_telemetry_chart) and costs a
# few hundred ms for a whole race; build_live runs every 2 s for every viewer.
CACHE_S = 60.0

_cache = {}                              # (device_id, race_start) -> (at, offsets)
_lock = threading.Lock()


def trace(values, fall):
    """(falls, segments, in_force) -- every fall in `values`, and where each applies.

    `values` is one total in time order, None where a row did not carry it.
    falls:     [(index_before, index_after, amount)], every fall seen
    segments:  [(index_from, (fall numbers in force from there on))]
    in_force:  the fall numbers still in force at the end: the real resets

    A RESET THAT IS TAKEN BACK IS NOT A RESET. When the Pi app restarts, the
    outbox is still delivering the old process's last rows while the new one
    publishes from zero, and for some seconds the store alternates: 10153, 0,
    10153, 0 ... Each step down is matched by a step straight back up to the
    value it fell from, and only the last fall is never answered. So a rise
    that lands back on the pre-fall value takes that fall out of force; any
    other rise is the car being driven while the pit was not listening, and
    is kept.

    The segments are what the repair tool needs and the live offset does not:
    inside the alternation the new process's rows are owed the amount too, or
    the repaired History trace keeps forty seconds of spikes to zero.
    """
    falls, in_force, segments = [], [], []
    prev = prev_i = None
    for i, v in enumerate(values):
        if v is None:
            continue
        if prev is not None:
            if prev - v > fall:
                falls.append((prev_i, i, prev - v))
                in_force.append(len(falls) - 1)
                segments.append((i, tuple(in_force)))
            elif in_force and v - prev > fall:
                before = values[falls[in_force[-1]][0]]
                if abs(v - before) <= fall:
                    in_force.pop()
                    segments.append((i, tuple(in_force)))
        prev, prev_i = v, i
    return falls, segments, in_force


def find_resets(values, fall):
    """[(index_before, index_after, amount)] for every real reset in `values`."""
    falls, _, in_force = trace(values, fall)
    return [falls[k] for k in in_force]


def _scan(conn, race_start, device_id):
    rows = conn.execute(
        "SELECT device_ts, total_race_energy, odometer_m "
        "FROM telemetry INDEXED BY idx_telemetry_chart "
        "WHERE device_id = ? AND device_ts >= ? ORDER BY device_ts",
        (device_id, race_start)).fetchall()
    ts = [r[0] for r in rows]
    offsets = {c: 0.0 for c in COLUMNS}
    events = {}
    for n, col in enumerate(RESET_FALL, start=1):
        events[col] = find_resets([r[n] for r in rows], RESET_FALL[col])
        offsets[col] = sum(e[2] for e in events[col])

    for col, leader in FOLLOWS.items():
        for before_i, after_i, _ in events[leader]:
            was = conn.execute(
                "SELECT %s FROM telemetry WHERE device_id = ? AND device_ts <= ? "
                "AND device_ts >= ? AND %s IS NOT NULL "
                "ORDER BY device_ts DESC LIMIT 1" % (col, col),
                (device_id, ts[before_i], race_start)).fetchone()
            now = conn.execute(
                "SELECT %s FROM telemetry WHERE device_id = ? AND device_ts >= ? "
                "AND %s IS NOT NULL ORDER BY device_ts LIMIT 1" % (col, col),
                (device_id, ts[after_i])).fetchone()
            if was is not None and now is not None and was[0] > now[0]:
                offsets[col] += was[0] - now[0]
    return offsets, {c: [(ts[a], ts[b], amt) for a, b, amt in ev]
                     for c, ev in events.items()}


def offsets(conn, race_start, device_id="solarcar", fresh=False):
    """{column: what to ADD to the car's figure} for the three running totals.

    All zeros when there is no race, when nothing was ever lost, and when the
    store cannot be read: an uncorrected total is what the pit showed before
    this existed, and is better than no dashboard.
    """
    if not race_start:
        return {c: 0.0 for c in COLUMNS}
    key = (device_id, float(race_start))
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and not fresh and now - hit[0] < CACHE_S:
            return dict(hit[1])
    try:
        found, _ = _scan(conn, race_start, device_id)
    except Exception:                                        # noqa: BLE001
        found = dict(hit[1]) if hit is not None else {c: 0.0 for c in COLUMNS}
    with _lock:
        _cache.clear()                   # one race at a time
        _cache[key] = (now, found)
    return dict(found)


def resets(conn, race_start, device_id="solarcar"):
    """(offsets, {column: [(ts_before, ts_after, amount)]}) -- uncached, for
    the repair tool and for anyone who wants to see WHEN."""
    return _scan(conn, race_start, device_id)


def corrected(value, offset):
    """value + offset, and None stays None: an unreported total is not zero."""
    return None if value is None else round(value + (offset or 0.0), 3)


if __name__ == "__main__":
    F = 50.0
    assert find_resets([1, 2, None, 3], F) == []
    assert find_resets([100, 95, 99], F) == [], "regen is not a reset"
    # the 2026-09-19 interleave: old and new process alternating, new one wins
    seq = [10153, 0, 10153, 0, 10153, 0, 0.5, 108]
    assert [(a, b) for a, b, _ in find_resets(seq, F)] == [(4, 5)]
    assert sum(e[2] for e in find_resets(seq, F)) == 10153
    # ...and if the OLD one's row is the last to arrive, nothing was lost
    assert find_resets([10153, 0, 10153, 10160], F) == []
    # driven while the pit was deaf: a rise that is not a return is kept
    assert len(find_resets([5000, 0, 900, 1000], F)) == 1
    # two losses add up
    assert sum(e[2] for e in find_resets([300, 0, 200, 0, 10], F)) == 500
    # the seam after the stored rows are repaired: corrected, then the car's own
    assert find_resets([21000, 21010, 10860, 10870], F)[0][2] == 10150
    # inside the alternation, the new process's rows are owed the amount too
    _, segs, _ = trace([10153, 0, 10153, 0, 5], F)
    assert segs == [(1, (0,)), (2, ()), (3, (1,))]
    assert corrected(None, 5.0) is None and corrected(1.0, None) == 1.0
    print("race_totals: ok")
