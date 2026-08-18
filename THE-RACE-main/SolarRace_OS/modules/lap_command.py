"""
lap_command.py — receive "cut a lap now" from the pit, safely
=============================================================
The pit writes a small command to Firebase; this is the car's side of that
channel. It exists as its own module because the interesting part is not the
Firebase call, it is the two problems that come with it.

PROBLEM 1: THREADS
The firebase-admin listener fires its callback on a BACKGROUND thread, while
the CAN worker thread is mutating vehicle_state and serialising it into the
0.5 s telemetry push. Touching the lap tracker from the callback would be a
straightforward data race.

The fix is confinement rather than locking: the callback validates the command
and puts it on a queue.Queue, and the CAN worker thread drains that queue and
applies it on its own thread, alongside everything else it owns. LapTracker and
vehicle_state stay single-threaded and need no locks at all.

Locking would have been worse than it looks. The only place a lock could go is
around vehicle_state, which is serialised *inside* push_telemetry_to_cloud —
so the CAN thread would end up holding a lock across a network write to
Firebase, stalling frame decoding for however long the RTDB round trip takes.

PROBLEM 2: THE NODE IS RETAINED
Firebase replays the current value of a watched node to every new listener. So
the same command re-fires on every reconnect, and again the next time the car
boots — which without care means a phantom lap every time the network hiccups,
and a phantom lap at the start of the next race weekend from a command sent
days earlier.

Two independent gates handle that:

  ID gate   ignore any id <= the last one seen. Kills reconnect replays.
  Age gate  a command older than MAX_COMMAND_AGE_S is ADOPTED (its id is
            recorded so later duplicates are also suppressed) but NOT executed.
            This is what stops a stale command firing at boot.

The age gate needs the wall clock, which on a Pi with no RTC may be badly wrong
until NTP syncs. If the clock is obviously unset we skip the age gate rather
than reject everything — see _clock_is_plausible.
"""

import queue
import time

# Commands older than this are recorded but not executed. Long enough for a slow
# network and a pit engineer's finger, far shorter than a pit stop.
MAX_COMMAND_AGE_S = 30.0

# Unix time in 2023. Below this the Pi's clock has clearly not been set yet
# (no RTC, no NTP), so any age comparison would be meaningless.
_PLAUSIBLE_EPOCH = 1_700_000_000

VALID_ACTIONS = ("cut_lap", "set_lap", "reset_energy")

# The strategy selector uses the same machinery on its own node — see
# CommandInbox below and firebase_client.STRATEGY_COMMAND_PATH.
STRATEGY_ACTIONS = ("set_strategy",)


def _clock_is_plausible():
    return time.time() > _PLAUSIBLE_EPOCH


class CommandInbox:
    """Thread-safe hand-off of pit commands to the CAN worker thread.

    Parameterised by node and action list so a second command type (the strategy
    selector) reuses this rather than growing a third near-identical copy of the
    listener, the queue hand-off, and the two idempotency gates. Those gates are
    subtle enough that having them in one place is worth more than the
    indirection.
    """

    def __init__(self, listen_fn, valid_actions, label="command",
                 max_age_s=MAX_COMMAND_AGE_S):
        self._listen_fn = listen_fn
        self._valid_actions = tuple(valid_actions)
        self._label = label
        self._queue = queue.Queue()
        self._max_age_s = max_age_s
        self._registration = None
        # Touched ONLY by the listener thread (single writer, single reader on
        # the same thread), so it needs no synchronisation of its own.
        self._last_id = None
        self.received = 0
        self.ignored = 0

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #
    def start(self):
        """Subscribe. Raises if Firebase is unavailable — the caller decides
        whether that is fatal (on the car it is not: no network still races)."""
        self._registration = self._listen_fn(self._on_event)
        return self

    def stop(self):
        if self._registration is not None:
            try:
                self._registration.close()
            except Exception:
                pass
            self._registration = None

    # ------------------------------------------------------------------ #
    # Listener thread                                                     #
    # ------------------------------------------------------------------ #
    def _on_event(self, event):
        """Runs on a firebase-admin BACKGROUND thread. Never mutates car state.

        Wrapped whole in try/except: an exception escaping a listener callback
        can tear the stream down, and losing the lap channel because of one
        malformed payload would be a poor trade.
        """
        try:
            data = getattr(event, "data", None)
            if not isinstance(data, dict):
                return          # node cleared, or a partial/child update

            cmd_id = data.get("id")
            action = data.get("action")
            if cmd_id is None or action not in self._valid_actions:
                return

            try:
                cmd_id = int(cmd_id)
            except (TypeError, ValueError):
                return

            # ID gate — the reconnect-replay killer.
            if self._last_id is not None and cmd_id <= self._last_id:
                self.ignored += 1
                return

            # Age gate — the stale-command-at-boot killer. Adopt the id either
            # way, so a repeat of a stale command is silently dropped too.
            if _clock_is_plausible():
                ts = data.get("ts")
                try:
                    age = abs(time.time() - float(ts))
                except (TypeError, ValueError):
                    age = 0.0   # no usable timestamp: trust the ID gate alone
                if age > self._max_age_s:
                    self._last_id = cmd_id
                    self.ignored += 1
                    print(f"🏁 ignoring stale {self._label} {cmd_id} "
                          f"({age:.0f}s old, limit {self._max_age_s:.0f}s)")
                    return

            self._last_id = cmd_id
            self.received += 1
            self._queue.put({"id": cmd_id, "action": action,
                             "value": data.get("value")})
        except Exception as exc:
            print(f"⚠️ {self._label} listener error (ignored): {exc}")

    # ------------------------------------------------------------------ #
    # CAN worker thread                                                   #
    # ------------------------------------------------------------------ #
    def drain(self):
        """Yield every queued command. Call from the CAN worker thread only."""
        while True:
            try:
                yield self._queue.get_nowait()
            except queue.Empty:
                return


class LapCommandInbox(CommandInbox):
    """Lap commands (`cut_lap`, `set_lap`, `reset_energy`) on /lap_command."""

    def __init__(self, max_age_s=MAX_COMMAND_AGE_S):
        from cloud.firebase_client import listen_lap_command
        super().__init__(listen_lap_command, VALID_ACTIONS,
                         label="lap command", max_age_s=max_age_s)


class StrategyCommandInbox(CommandInbox):
    """Strategy selection (`set_strategy`) on its own /strategy_command node.

    A separate node from /driver_command on purpose: that one is latest-wins for
    driver text and is DELETED to clear the HUD banner, so sharing it would mean
    picking a strategy wipes whatever the driver was being told — and clearing a
    driver message would reach this handler as a null event.
    """

    def __init__(self, max_age_s=MAX_COMMAND_AGE_S):
        from cloud.firebase_client import listen_strategy_command
        super().__init__(listen_strategy_command, STRATEGY_ACTIONS,
                         label="strategy command", max_age_s=max_age_s)
