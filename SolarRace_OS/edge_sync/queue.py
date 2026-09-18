"""Durable local queue for the edge SDK.

Every tracked point is written to SQLite *before* anything tries to send it.
That is the whole durability story: if the network is down or the device
reboots mid-flight, the data is still on disk and gets drained later.

The queue is the only shared state between the caller's thread (which calls
`enqueue` via `track()`) and the background batcher thread (which calls
`fetch_unsent` / `mark_sent`), so every operation is guarded by a lock and uses
a single connection opened with check_same_thread=False.

WHY IT STAYS FAST WITH A LARGE BACKLOG
Every query here is an index lookup, so the cost of one insert or one batch
does not grow with the number of rows waiting:

  * `seq` is the INTEGER PRIMARY KEY, so SQLite assigns it for free — there is
    no `SELECT MAX(seq)` scan on insert.
  * acknowledged rows are DELETED, not flagged, so the table only ever holds
    what is still waiting to be sent, and never fills the disk.
  * batches are read in `seq` order, which is the primary key itself.

WHY ORDER IS BY `seq` AND NOT BY TIMESTAMP
A Raspberry Pi has no battery-backed clock. It boots believing it is whenever
it last shut down, and the first NTP sync steps the wall clock, possibly by
years. Ordering a backlog by wall-clock time would scramble it. `seq` is the
order the points were actually recorded in, and never goes backwards.

POINT IDS
Each point gets a 20-character id in the same format as a Firebase push key:
8 characters of milliseconds, then 12 characters that increase within the same
millisecond. The ids are generated here, inside the insert transaction, and the
last one is stored in the database, so they are strictly increasing across
restarts and across the wall clock stepping backwards. That lets a sender use
them directly as keys a server can order by (see docs/implementation.md).
"""

from __future__ import annotations

import json
import math
import secrets
import sqlite3
import threading
import time

PUSH_CHARS = "-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz"


def _encode_time(ms: int) -> str:
    chars = []
    for _ in range(8):
        chars.append(PUSH_CHARS[ms % 64])
        ms //= 64
    return "".join(reversed(chars))


def _increment(suffix: str) -> str | None:
    """The next suffix in PUSH_CHARS order, or None if it would overflow."""
    chars = list(suffix)
    for i in range(len(chars) - 1, -1, -1):
        idx = PUSH_CHARS.index(chars[i])
        if idx < 63:
            chars[i] = PUSH_CHARS[idx + 1]
            return "".join(chars)
        chars[i] = PUSH_CHARS[0]
    return None


def _random_suffix() -> str:
    # Keep the first character low so the suffix has room to increment many
    # times within one millisecond before it could overflow.
    return PUSH_CHARS[0] + "".join(secrets.choice(PUSH_CHARS) for _ in range(11))


def clean_json(obj):
    """A copy of `obj` that JSON (and a strict server) will accept.

    Non-finite floats become None: `NaN` is not valid JSON, and a record that
    a server refuses would otherwise sit at the head of the queue. Tuples and
    sets become lists; dict keys become strings.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): clean_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [clean_json(v) for v in obj]
    return obj


class Queue:
    def __init__(self, db_path: str, synchronous: str = "FULL"):
        """Open (or create) the outbox at `db_path`.

        `synchronous` is SQLite's durability setting. FULL (the default) waits
        for the disk on every commit, so a point that `enqueue` returned for
        survives a power cut. NORMAL is faster and survives a process crash but
        can lose the last moments before a power cut.
        """
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA synchronous={synchronous}")
        with self._lock:
            self._create_schema()
            self._last_ms, self._last_suffix = self._load_id_state()

    # --- schema -------------------------------------------------------------

    def _create_schema(self) -> None:
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("CREATE TABLE IF NOT EXISTS sdk_meta ("
                      "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            old_cols = {r["name"] for r in c.execute("PRAGMA table_info(outbox)")}
            if "sent" in old_cols:
                # Version 0.1 kept acknowledged rows with sent=1. Carry the
                # unsent ones over, in the order 0.1 would have sent them.
                c.execute("ALTER TABLE outbox RENAME TO outbox_v1")
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
                    seq    INTEGER PRIMARY KEY AUTOINCREMENT,  -- record order
                    id     TEXT NOT NULL UNIQUE,   -- client-assigned point id
                    metric TEXT,                   -- scalar point: name
                    value  REAL,                   -- scalar point: value
                    data   TEXT,                   -- snapshot point: JSON object
                    ts     INTEGER NOT NULL        -- device wall clock (epoch ms)
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS dead_letter (
                    seq       INTEGER PRIMARY KEY,
                    id        TEXT NOT NULL UNIQUE,
                    metric    TEXT,
                    value     REAL,
                    data      TEXT,
                    ts        INTEGER NOT NULL,
                    error     TEXT,
                    failed_ts INTEGER NOT NULL
                )
                """
            )
            if "sent" in old_cols:
                c.execute(
                    "INSERT OR IGNORE INTO outbox (id, metric, value, ts) "
                    "SELECT id, metric, value, ts FROM outbox_v1 "
                    "WHERE sent = 0 ORDER BY ts ASC, seq ASC"
                )
                c.execute("DROP TABLE outbox_v1")
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def _load_id_state(self) -> tuple[int, str]:
        rows = dict(self._conn.execute(
            "SELECT key, value FROM sdk_meta "
            "WHERE key IN ('last_id_ms', 'last_id_suffix')").fetchall())
        return int(rows.get("last_id_ms", 0)), rows.get("last_id_suffix", "")

    # --- ids ----------------------------------------------------------------

    def _next_id(self) -> tuple[str, int, str]:
        """(id, ms, suffix) strictly greater than every id issued before."""
        now_ms = int(time.time() * 1000)
        if now_ms > self._last_ms:
            ms, suffix = now_ms, _random_suffix()
        else:
            # Same millisecond, or the wall clock went backwards: stay on the
            # last millisecond and count up within it.
            ms, suffix = self._last_ms, _increment(self._last_suffix or _random_suffix())
            if suffix is None:
                ms, suffix = self._last_ms + 1, _random_suffix()
        return _encode_time(ms) + suffix, ms, suffix

    def rekey_after(self, floor_id: str) -> int:
        """Make every id issued from now on, and every queued id, sort after
        `floor_id`. Returns how many queued points were renumbered.

        For a receiver that resumes from the last id it stored: if this device's
        clock was behind when it recorded (a Pi without a battery-backed clock,
        booted after a hard power-off, can be an hour behind), its ids could sort
        BELOW ids the receiver already has, and the receiver would never ask for
        them. Call this with the receiver's newest id before sending.

        Only unsent points are renumbered, in their recorded order, so the
        order is kept. `floor_id` must be in the push-key format.
        """
        if len(floor_id) != 20 or any(ch not in PUSH_CHARS for ch in floor_id):
            raise ValueError(f"not a push-key format id: {floor_id!r}")
        floor_ms = 0
        for ch in floor_id[:8]:
            floor_ms = floor_ms * 64 + PUSH_CHARS.index(ch)

        with self._lock:
            c = self._conn
            last_ms, last_suffix = self._last_ms, self._last_suffix
            if _encode_time(last_ms) + last_suffix < floor_id:
                self._last_ms, self._last_suffix = floor_ms, floor_id[8:]
            (lowest,) = c.execute("SELECT MIN(id) FROM outbox").fetchone()
            if lowest is None or lowest > floor_id:
                self._save_id_state()
                return 0
            c.execute("BEGIN IMMEDIATE")
            try:
                seqs = [r[0] for r in c.execute("SELECT seq FROM outbox ORDER BY seq")]
                for seq in seqs:
                    pid, self._last_ms, self._last_suffix = self._next_id()
                    c.execute("UPDATE outbox SET id = ? WHERE seq = ?", (pid, seq))
                self._write_id_state()
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                self._last_ms, self._last_suffix = last_ms, last_suffix
                raise
        return len(seqs)

    def reconcile(self, receiver_newest_id: str) -> tuple[int, int]:
        """Line the queue up with a receiver that resumes from its newest id.

        Returns (acknowledged, renumbered).

        * If `receiver_newest_id` is one of OUR queued ids, a batch reached the
          receiver but the acknowledgement never came back (power cut mid-send).
          Batches go oldest-first and each is all-or-nothing, so every queued
          point up to and including that id was delivered: they are removed
          instead of being sent again under new ids.
        * Otherwise any queued point still sorting at or below it is renumbered
          above it (see rekey_after), so the receiver will ask for it.
        """
        with self._lock:
            hit = self._conn.execute("SELECT seq FROM outbox WHERE id = ?",
                                     (receiver_newest_id,)).fetchone()
            acked = 0
            if hit is not None:
                c = self._conn
                c.execute("BEGIN IMMEDIATE")
                try:
                    acked = c.execute("DELETE FROM outbox WHERE seq <= ?",
                                      (hit[0],)).rowcount
                    c.execute("COMMIT")
                except BaseException:
                    c.execute("ROLLBACK")
                    raise
        return acked, self.rekey_after(receiver_newest_id)

    def _write_id_state(self) -> None:
        self._conn.executemany(
            "INSERT INTO sdk_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [("last_id_ms", str(self._last_ms)),
             ("last_id_suffix", self._last_suffix)])

    def _save_id_state(self) -> None:
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            self._write_id_state()
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    # --- writes -------------------------------------------------------------

    def enqueue(self, metric: str, value: float | None, ts: int | None = None) -> str:
        """Persist one scalar point. Returns its id."""
        return self.enqueue_many([(metric, value, ts)])[0]

    def enqueue_record(self, data: dict, ts: int | None = None) -> str:
        """Persist one snapshot point: a whole JSON object. Returns its id."""
        return self._insert([(None, None, data, ts)])[0]

    def enqueue_many(self, points) -> list[str]:
        """Persist many scalar points in ONE transaction (one disk sync).

        `points` is an iterable of (metric, value) or (metric, value, ts).
        """
        rows = []
        for p in points:
            metric, value = p[0], p[1]
            ts = p[2] if len(p) > 2 else None
            rows.append((metric, value, None, ts))
        return self._insert(rows)

    def _insert(self, rows) -> list[str]:
        prepared = []
        for metric, value, data, ts in rows:
            if value is not None:
                value = float(value)
                if not math.isfinite(value):
                    value = None
            blob = (json.dumps(clean_json(data), separators=(",", ":"),
                               allow_nan=False, default=str)
                    if data is not None else None)
            prepared.append((metric, value, blob,
                             int(ts) if ts is not None else int(time.time() * 1000)))
        if not prepared:
            return []

        with self._lock:
            c = self._conn
            last_ms, last_suffix = self._last_ms, self._last_suffix
            c.execute("BEGIN IMMEDIATE")
            try:
                ids = []
                for metric, value, blob, ts in prepared:
                    pid, self._last_ms, self._last_suffix = self._next_id()
                    c.execute("INSERT INTO outbox (id, metric, value, data, ts) "
                              "VALUES (?, ?, ?, ?, ?)", (pid, metric, value, blob, ts))
                    ids.append(pid)
                self._write_id_state()
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                self._last_ms, self._last_suffix = last_ms, last_suffix
                raise
        return ids

    # --- reads / acknowledgement --------------------------------------------

    @staticmethod
    def _point(row) -> dict:
        p = {"id": row["id"], "ts": row["ts"]}
        if row["data"] is not None:
            p["data"] = json.loads(row["data"])
        else:
            p["metric"] = row["metric"]
            p["value"] = row["value"]
        return p

    def fetch_unsent(self, limit: int, newest_first: bool = False) -> list[dict]:
        """A batch of points not yet acknowledged, in the order they were recorded.

        Oldest-first by default, so a backlog drains in chronological order.
        With `newest_first`, the batch is the most RECENT `limit` points (still
        returned oldest-to-newest within the batch), so after an outage the
        live picture arrives first and the backlog fills in behind it.
        """
        order = "DESC" if newest_first else "ASC"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, metric, value, data, ts FROM outbox "
                f"ORDER BY seq {order} LIMIT ?", (limit,)).fetchall()
        if newest_first:
            rows.reverse()
        return [self._point(r) for r in rows]

    def mark_sent(self, ids: list[str]) -> None:
        """Delete acknowledged points -- only ever called after the server acks."""
        if not ids:
            return
        with self._lock:
            c = self._conn
            c.execute("BEGIN IMMEDIATE")
            try:
                c.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in ids])
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def dead_letter(self, point_id: str, error: str) -> None:
        """Move a point the server permanently refused out of the outbox.

        It is kept on disk in `dead_letter` (never deleted), so nothing is lost,
        but it no longer blocks every point recorded after it.
        """
        with self._lock:
            c = self._conn
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute(
                    "INSERT OR REPLACE INTO dead_letter "
                    "(seq, id, metric, value, data, ts, error, failed_ts) "
                    "SELECT seq, id, metric, value, data, ts, ?, ? FROM outbox WHERE id = ?",
                    (error[:1000], int(time.time() * 1000), point_id))
                c.execute("DELETE FROM outbox WHERE id = ?", (point_id,))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def unsent_count(self) -> int:
        with self._lock:
            (n,) = self._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()
        return n

    def dead_count(self) -> int:
        with self._lock:
            (n,) = self._conn.execute("SELECT COUNT(*) FROM dead_letter").fetchone()
        return n

    def close(self) -> None:
        with self._lock:
            self._conn.close()
