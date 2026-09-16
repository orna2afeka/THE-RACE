#!/usr/bin/env python3
"""
check_live_cadence.py - the live socket's push rate, against a hostile client
============================================================================
    python tools/check_live_cadence.py

Drives Pit_Web.api.ws_live with a fake WebSocket -- no browser, no network, no
database (build_live and the connection are stubbed) -- and exits non-zero if
the server can be talked into pushing faster than it should.

THE BUG THIS EXISTS FOR. /ws/live was meant to push every FAST_TICK_S (2 s).
Measured in a real browser it pushed 30 payloads a second on Driver Telemetry
and 134 a second on History: 60x to 270x the intended rate, ~10 KB each, up to
1.4 MB/s per open device on the pit LAN. The handler sent, then waited UP TO
2 s for a client message; the client sent its parameters back on EVERY message
it received. So the wait always ended at once, and the pair ran as fast as the
laptop could go -- saturating the main thread (a 50 ms timer fired 30 ms late)
and skewing the race clock's reading of server time.

Fixing the client alone would not be enough on race day. A phone in the pit
still holding last week's cached bundle would keep echoing and keep the storm
going. So the SERVER must hold its own cadence whatever a client sends, and
these checks attack it with exactly that client.

  1. ECHO STORM       answers every push                 -> still ~one push per tick
  2. PROMPT OVERRIDE  one real manual-lap change          -> pushed at once, not a tick later
  3. RAPID CHANGES    changes the value on every push     -> bounded by the floor
  4. SILENT CLIENT    sends nothing at all                -> the regular tick
  5. DISCONNECT       the client goes away                -> handler exits promptly, cleanly

Run it against the pre-fix handler and checks 1 and 3 FAIL. That is the point:
a check that cannot fail on the bug proves nothing about the fix.
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Pit_Web import api                                          # noqa: E402

FAILURES = []
CLOSE = object()
ASYNC_ERRORS = []


def check(name, ok, detail=""):
    print("  %-40s %s  %s" % (name, "OK  " if ok else "FAIL", detail))
    if not ok:
        FAILURES.append("%s - %s" % (name, detail))


class _Conn:
    def close(self):
        pass


def _stub_build_live(conn, manual_lap=-1):
    """Instant, so the test measures the handler's cadence and nothing else."""
    return {"ts": time.time(), "manual": manual_lap}


class FakeWS:
    """Just enough of Starlette's WebSocket for ws_live."""

    def __init__(self, on_send=None):
        self.sent = []                  # (seconds since start, payload)
        self.inbox = asyncio.Queue()
        self.on_send = on_send
        self.closed = False
        self.t0 = time.monotonic()

    async def accept(self):
        pass

    async def send_json(self, data):
        if self.closed:
            raise api.WebSocketDisconnect()
        self.sent.append((time.monotonic() - self.t0, data))
        # Cap the storm so the pre-fix run cannot exhaust memory.
        if self.on_send and len(self.sent) < 50000:
            self.on_send(self, data)

    async def receive_json(self):
        msg = await self.inbox.get()
        if msg is CLOSE:
            raise api.WebSocketDisconnect()
        return msg

    def close(self):
        self.closed = True
        self.inbox.put_nowait(CLOSE)


async def run(seconds, fast, floor, on_send=None, driver=None):
    """ws_live against a FakeWS for `seconds`, then disconnect.

    Returns (ws, seconds the handler took to exit after the disconnect, or None
    if it never did).
    """
    api.FAST_TICK_S = fast
    api.LIVE_MIN_PUSH_S = floor          # unused by the pre-fix handler; harmless
    ws = FakeWS(on_send)
    task = asyncio.create_task(api.ws_live(ws))
    drv = asyncio.create_task(driver(ws)) if driver else None
    await asyncio.sleep(seconds)
    closed_at = time.monotonic()
    ws.close()
    try:
        await asyncio.wait_for(task, timeout=fast + 2.0)
        exited = time.monotonic() - closed_at
    except asyncio.TimeoutError:
        task.cancel()
        exited = None
    if drv:
        drv.cancel()
    await asyncio.sleep(0.05)            # let any orphaned task surface its error
    return ws, exited


async def main():
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _l, ctx: ASYNC_ERRORS.append(ctx.get("message", str(ctx))))

    saved = {n: getattr(api, n) for n in ("ro_conn", "build_live", "FAST_TICK_S")}
    had_floor = hasattr(api, "LIVE_MIN_PUSH_S")
    saved_floor = getattr(api, "LIVE_MIN_PUSH_S", None)
    api.ro_conn = lambda: _Conn()
    api.build_live = _stub_build_live
    print("handler under test: %s  (LIVE_MIN_PUSH_S %s)\n"
          % (api.ws_live.__code__.co_filename.split(os.sep)[-1],
             "present" if had_floor else "ABSENT - this is the pre-fix handler"))
    try:
        # 1. The client that caused the storm: answers every push, value unchanged.
        ws, _ = await run(1.5, fast=0.3, floor=0.08,
                          on_send=lambda w, d: w.inbox.put_nowait({"manualLap": -1}))
        n = len(ws.sent)
        check("1. echo storm holds the tick", 3 <= n <= 10,
              "%d pushes in 1.5 s at a 0.3 s tick (%.0f/s; ~6 expected)" % (n, n / 1.5))

        # 2. One real change must not wait for the next tick.
        change = {}

        async def one_change(w):
            await asyncio.sleep(0.4)
            change["at"] = time.monotonic() - w.t0
            w.inbox.put_nowait({"manualLap": 42})

        ws, _ = await run(1.2, fast=1.5, floor=0.08, driver=one_change)
        hit = next((t for t, d in ws.sent if d["manual"] == 42), None)
        lag = None if (hit is None or "at" not in change) else hit - change["at"]
        check("2. a real change is pushed at once", lag is not None and lag < 0.3,
              "pushed %s after the change, with a 1.5 s tick" %
              ("never" if lag is None else "%.0f ms" % (lag * 1000)))

        # 3. A client changing the value on every push must still be bounded.
        box = {"v": 0}

        def changing(w, d):
            box["v"] += 1
            w.inbox.put_nowait({"manualLap": box["v"]})

        ws, _ = await run(1.2, fast=1.5, floor=0.08, on_send=changing)
        n = len(ws.sent)
        check("3. rapid changes bounded by the floor", n <= 22,
              "%d pushes in 1.2 s with a 0.08 s floor (<= ~18 expected)" % n)

        # 4. No client messages: just the tick.
        ws, _ = await run(1.5, fast=0.3, floor=0.08)
        n = len(ws.sent)
        check("4. silent client gets the tick", 3 <= n <= 10,
              "%d pushes in 1.5 s at a 0.3 s tick (~6 expected)" % n)

        # 5. Disconnect: out promptly, even mid-way through a long tick.
        ws, exited = await run(0.3, fast=1.5, floor=0.08)
        check("5. exits promptly on disconnect", exited is not None and exited < 0.6,
              "exited %s after the disconnect, inside a 1.5 s tick" %
              ("never" if exited is None else "%.0f ms" % (exited * 1000)))
    finally:
        for n, v in saved.items():
            setattr(api, n, v)
        if had_floor:
            api.LIVE_MIN_PUSH_S = saved_floor
        elif hasattr(api, "LIVE_MIN_PUSH_S"):
            delattr(api, "LIVE_MIN_PUSH_S")

    check("no unhandled errors in async tasks", not ASYNC_ERRORS,
          "; ".join(ASYNC_ERRORS[:3]) or "none")


asyncio.run(main())
print()
if FAILURES:
    print("FAILED (%d):" % len(FAILURES))
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("All live-cadence checks passed.")
