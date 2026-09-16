"""Edge SDK public API and background sync pipeline.

What a developer using the SDK calls:

    init(server_url, api_key, device_id)
    track(metric, value, ts=None)      -- one scalar reading
    track_many([(metric, value), ...]) -- many readings, one disk write
    track_snapshot(dict, ts=None)      -- a whole JSON object as one point
    force_flush()                      -- drain everything now (e.g. on shutdown)

What happens after track():

    1. The point is written to a durable SQLite queue (edge_sync/queue.py).
    2. A background batcher pulls unsent points (oldest-first by default) and
       groups them.
    3. The sender delivers the batch -- POST to the REST server, or any
       function passed as `sender`.
    4. Points are deleted from the queue only after the sender returns. Failed
       sends retry with backoff, so a dropped connection just grows the
       backlog, which drains once the network returns.

track() never raises and never touches the network. The only work it does in
the caller's thread is the local SQLite insert, so a slow or dead link can
never stall the code that produces the data.
"""

from __future__ import annotations

import logging
import threading
import time

from . import sync_policy
from .queue import Queue

log = logging.getLogger("edge_sync")


class RejectedError(Exception):
    """Raise this from a sender when the server refused a batch PERMANENTLY.

    For example: malformed data, or a record too large to accept. Retrying
    will never succeed, and without this the refused point would sit at the
    head of the queue and block everything recorded after it. The SDK retries
    the batch one point at a time and moves only the refused points to the
    queue's `dead_letter` table, where they stay on disk.

    Any other exception is treated as temporary (network down, timeout, server
    error) and the batch is retried after a backoff.
    """


class Client:
    def __init__(
        self,
        server_url: str,
        api_key: str,
        device_id: str,
        *,
        db_path: str = "sdk_outbox.db",
        metadata: dict | None = None,
        batch_size: int = 50,
        flush_interval: float = 2.0,
        max_backoff: float = 30.0,
        network: str | None = None,
        battery: float | None = None,
        drain: str = "oldest",
        synchronous: str = "FULL",
        sender=None,
    ):
        if drain not in ("oldest", "newest"):
            raise ValueError("drain must be 'oldest' or 'newest'")
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.device_id = device_id
        self.metadata = metadata or {}
        self.max_backoff = max_backoff
        # "oldest": the backlog drains in the order it was recorded. Required
        # when the receiver resumes from the last id it saw.
        # "newest": the most recent points go first after an outage, so a live
        # view is current at once; the backlog fills in behind.
        self.drain = drain

        # Link conditions drive the network-aware sync policy. When `network` is
        # set, batch_size / flush_interval are derived from it dynamically;
        # otherwise the fixed values passed in are used.
        self.network = network
        self.battery = battery
        self._base_batch_size = batch_size
        self._base_flush_interval = flush_interval
        self.batch_size = batch_size          # current effective values
        self.flush_interval = flush_interval
        self._apply_policy()

        self.queue = Queue(db_path, synchronous=synchronous)

        # The sender is injectable so tests can simulate a flaky/offline network,
        # and so the SDK can deliver somewhere other than its own REST server.
        # It takes a batch dict and must raise on failure, return on success.
        self._sender = sender or self._http_send
        self._http = None
        if sender is None:
            # Imported here, not at the top: a device that delivers through its
            # own sender does not need httpx installed at all.
            import httpx
            self._http = httpx.Client(timeout=10.0)
        self._close_deadline = None

        # Counters kept in memory so track() never has to count the queue.
        self._stats_lock = threading.Lock()
        self._pending = self.queue.unsent_count()
        self._sent = 0
        self._dropped = 0
        self._dead = self.queue.dead_count()
        self._failures = 0
        self._last_ok_ts = None
        self._last_error = None

        self._backoff = 1.0
        self._stop = threading.Event()
        self._wake = threading.Event()   # nudges the batcher to flush now
        self._worker = threading.Thread(target=self._run, daemon=True,
                                        name="edge-sync")
        self._worker.start()

    # --- public API ---------------------------------------------------------

    def track(self, metric: str, value: float, ts: int | None = None) -> str | None:
        """Record one scalar point durably. Returns its id, or None if it could
        not be stored (see stats()["dropped"])."""
        return self._store(lambda: [self.queue.enqueue(metric, value, ts)])

    def track_many(self, points) -> list[str] | None:
        """Record many (metric, value[, ts]) points in one disk write."""
        return self._store(lambda: self.queue.enqueue_many(points), many=True)

    def track_snapshot(self, data: dict, ts: int | None = None) -> str | None:
        """Record a whole JSON object as ONE point, exactly as given.

        Use this when a device produces many values at once (a full vehicle
        state, say). One row per snapshot instead of one per value keeps the
        queue small and the disk writes few, and nothing is flattened away.
        """
        return self._store(lambda: [self.queue.enqueue_record(data, ts)])

    def force_flush(self, timeout: float = 15.0) -> bool:
        """Block until the queue is fully drained or `timeout` elapses.

        Returns True if everything was acknowledged, False on timeout.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._pending_count() == 0:
                return True
            self._wake.set()
            time.sleep(0.05)
        return self._pending_count() == 0

    def stats(self) -> dict:
        """Health of the pipeline, cheap enough to poll every frame.

        pending      points on disk waiting to be sent
        sent         points acknowledged since this Client started
        dropped      points that could NOT be stored (disk error) -- real loss
        dead         points the server permanently refused, kept in dead_letter
        failures     consecutive failed send attempts (0 when healthy)
        last_ok_ts   wall-clock time of the last acknowledged batch, or None
        last_error   text of the last send or storage error, or None
        """
        with self._stats_lock:
            return {
                "pending": self._pending, "sent": self._sent,
                "dropped": self._dropped, "dead": self._dead,
                "failures": self._failures, "last_ok_ts": self._last_ok_ts,
                "last_error": self._last_error,
            }

    def reconcile(self, receiver_newest_id: str) -> tuple[int, int]:
        """Line the queue up with a receiver's newest id before sending.

        For receivers that resume from the last id they stored (use with
        drain="oldest"). Removes points the receiver already has and renumbers
        any that would sort below it. Returns (acknowledged, renumbered). Safe
        to call from inside a sender; see Queue.reconcile.
        """
        acked, renumbered = self.queue.reconcile(receiver_newest_id)
        if acked:
            with self._stats_lock:
                self._pending -= acked
                self._sent += acked
        return acked, renumbered

    def close(self, flush_timeout: float = 5.0) -> None:
        """Stop the background worker and release resources.

        Spends at most `flush_timeout` seconds sending what is still queued.
        Whatever is left stays on disk and is sent by the next Client that
        opens the same db_path, so a big backlog cannot hold up a shutdown.
        """
        self._close_deadline = time.monotonic() + max(0.0, flush_timeout)
        self._stop.set()
        self._wake.set()
        self._worker.join(timeout=flush_timeout + 1.0)
        if self._http is not None:
            self._http.close()
        self.queue.close()

    # --- storing ------------------------------------------------------------

    def _store(self, write, many: bool = False):
        try:
            ids = write()
        except Exception as exc:
            with self._stats_lock:
                self._dropped += 1
                self._last_error = f"storage: {exc}"
                dropped = self._dropped
            # First failure and then every 100th, so a full disk cannot flood
            # the log at the caller's data rate.
            if dropped == 1 or dropped % 100 == 0:
                log.error("edge_sync could not store a point (%d so far): %s",
                          dropped, exc)
            return None
        with self._stats_lock:
            before = self._pending
            self._pending += len(ids)
            # Nudge only when a batch has just FILLED. Nudging on every point
            # while a backlog sits above batch_size would cut every backoff
            # short and retry a dead link at the caller's data rate.
            full = before < self.batch_size <= self._pending
        if full:
            self._wake.set()
        return ids if many else ids[0]

    def _pending_count(self) -> int:
        with self._stats_lock:
            return self._pending

    # --- network-aware sync policy ------------------------------------------

    def set_link(self, network: str | None = None, battery: float | None = None) -> None:
        """Update the device's link conditions; the batcher adapts immediately."""
        if network is not None:
            self.network = network
        if battery is not None:
            self.battery = battery
        self._apply_policy()
        self._wake.set()   # re-evaluate the loop now

    def _apply_policy(self) -> None:
        """Recompute effective batch size / flush interval from link state."""
        if self.network is None:
            self.batch_size = self._base_batch_size
            self.flush_interval = self._base_flush_interval
            self._allow_send = True
        else:
            self.batch_size, self.flush_interval, self._allow_send = sync_policy.plan(
                self.network, self.battery
            )

    def _send_metadata(self) -> dict:
        """Static metadata plus current link state, sent once per batch."""
        meta = dict(self.metadata)
        if self.network is not None:
            meta["network"] = self.network
        if self.battery is not None:
            meta["battery"] = self.battery
        return meta

    # --- background batcher + sender ----------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            # Flush on a timer or when nudged (full batch / force_flush / close).
            self._wake.wait(timeout=self.flush_interval)
            self._wake.clear()

            # Network-aware policy: when the device knows it's offline, don't
            # attempt to send -- just keep buffering durably (saves the radio).
            if not self._allow_send:
                continue
            try:
                if not self._drain():
                    self._sleep_backoff(self._backoff)
                    self._backoff = min(self._backoff * 2, self.max_backoff)
            except Exception as exc:
                # The worker thread must survive anything, or syncing stops
                # silently while track() keeps filling the disk.
                log.exception("edge_sync batcher error: %s", exc)
                self._sleep_backoff(self._backoff)

        # On close, make a best-effort final drain of whatever is queued.
        try:
            self._drain(final=True)
        except Exception:
            pass  # best effort; unsent data remains durably on disk

    def _drain(self, final: bool = False) -> bool:
        """Send batches until the queue is empty. False if a send failed."""
        while final or not self._stop.is_set():
            if final and self._close_deadline is not None                     and time.monotonic() >= self._close_deadline:
                return False
            points = self.queue.fetch_unsent(self.batch_size,
                                             newest_first=self.drain == "newest")
            if not points:
                self._backoff = 1.0
                return True
            try:
                self._send_batch(points)
            except RejectedError:
                if not self._isolate_rejected(points):
                    return False
                continue
            except Exception as exc:
                self._record_failure(exc)
                return False
            self._acknowledge(points)
        return True

    def _isolate_rejected(self, points: list[dict]) -> bool:
        """Resend a refused batch one point at a time; dead-letter the refusers.

        False if the network failed part-way (the rest stay queued).
        """
        for p in points:
            try:
                self._send_batch([p])
            except RejectedError as exc:
                self.queue.dead_letter(p["id"], str(exc))
                with self._stats_lock:
                    self._pending -= 1
                    self._dead += 1
                    self._last_error = f"rejected: {exc}"
                log.error("edge_sync: point %s refused by the server, kept in "
                          "dead_letter: %s", p["id"], exc)
                continue
            except Exception as exc:
                self._record_failure(exc)
                return False
            self._acknowledge([p])
        return True

    def _acknowledge(self, points: list[dict]) -> None:
        self.queue.mark_sent([p["id"] for p in points])
        self._backoff = 1.0
        with self._stats_lock:
            self._pending -= len(points)
            self._sent += len(points)
            self._failures = 0
            self._last_ok_ts = time.time()

    def _record_failure(self, exc: Exception) -> None:
        with self._stats_lock:
            self._failures += 1
            self._last_error = f"send: {exc}"

    def _send_batch(self, points: list[dict]) -> None:
        batch = {
            "device_id": self.device_id,
            "metadata": self._send_metadata(),
            "points": points,
        }
        self._sender(batch)

    def _http_send(self, batch: dict) -> None:
        resp = self._http.post(
            f"{self.server_url}/api/v1/telemetry",
            json=batch,
            headers={"X-API-Key": self.api_key},
        )
        # 400/413/422 mean "this data will never be accepted". 401/403 are NOT
        # treated that way: a wrong key is a configuration problem, and
        # dead-lettering every point because of it would throw the data aside.
        if resp.status_code in (400, 413, 422):
            raise RejectedError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()

    def _sleep_backoff(self, seconds: float) -> None:
        # Interruptible sleep so close()/force_flush() stay responsive.
        self._wake.wait(timeout=seconds)
        self._wake.clear()


# --- module-level convenience API ------------------------------------------
# Mirrors the architecture's public surface: init / track / force_flush.

_default: Client | None = None


def init(server_url: str, api_key: str, device_id: str, **kwargs) -> Client:
    global _default
    _default = Client(server_url, api_key, device_id, **kwargs)
    return _default


def auto_init(config_path: str | None = None) -> Client:
    """Initialize the SDK without hand-coding init() — read config from, in order:

      1. an explicit JSON file at `config_path`,
      2. a `telemetry.json` file in the current directory (if present),
      3. environment variables: TELEMETRY_SERVER_URL, TELEMETRY_API_KEY,
         TELEMETRY_DEVICE_ID, and optional TELEMETRY_NETWORK.

    A config file may set: server_url, api_key, device_id, network, and any other
    Client option (batch_size, flush_interval, db_path, metadata, drain, …). Handy
    on a device that's provisioned once with a key from the dashboard's Setup tab.
    """
    import json
    import os

    cfg: dict = {}
    path = config_path or ("telemetry.json" if os.path.exists("telemetry.json") else None)
    if path:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

    server_url = cfg.get("server_url") or os.environ.get("TELEMETRY_SERVER_URL")
    api_key = cfg.get("api_key") or os.environ.get("TELEMETRY_API_KEY")
    device_id = cfg.get("device_id") or os.environ.get("TELEMETRY_DEVICE_ID")
    network = cfg.get("network") or os.environ.get("TELEMETRY_NETWORK")

    missing = [n for n, v in
               (("server_url", server_url), ("api_key", api_key), ("device_id", device_id))
               if not v]
    if missing:
        raise RuntimeError(
            "auto_init() is missing: " + ", ".join(missing) +
            ". Provide them in a config file or TELEMETRY_* environment variables."
        )

    # Pass through any extra Client options from the config file.
    opts = {k: v for k, v in cfg.items()
            if k not in ("server_url", "api_key", "device_id")}
    if network and "network" not in opts:
        opts["network"] = network
    return init(server_url, api_key, device_id, **opts)


def _client() -> Client:
    if _default is None:
        raise RuntimeError("SDK not initialized; call init() first")
    return _default


def track(metric: str, value: float, ts: int | None = None) -> str | None:
    return _client().track(metric, value, ts)


def track_many(points) -> list[str] | None:
    return _client().track_many(points)


def track_snapshot(data: dict, ts: int | None = None) -> str | None:
    return _client().track_snapshot(data, ts)


def force_flush(timeout: float = 15.0) -> bool:
    return _client().force_flush(timeout)
