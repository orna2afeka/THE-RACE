"""
collector.py — the ONE Firebase subscription for the pit wall
=============================================================
Run this as its own process, from the REPO ROOT ("THE RACE"):

    python Pit_Dashboard/collector.py

(Or `python collector.py` from inside Pit_Dashboard — the launch directory
does not matter, all paths resolve relative to this package.)

It is the only thing that talks to Firebase. It streams the append-only
`telemetry_history` node over the RTDB REST Server-Sent-Events API and stores
every sample into local SQLite. The dashboard and export read from SQLite only.

Why REST SSE and not firebase-admin .listen()?
----------------------------------------------
firebase-admin's Reference.listen() re-downloads the WHOLE node on every
(re)connect and cannot be combined with orderBy/startAt (listen() exists only on
Reference, not Query). The REST stream CAN take orderBy="$key"&startAt="<key>",
so after the first backfill we only ever stream the tail — incremental live feed
and reconnect catch-up in a single mechanism.

Resilience model
-----------------
* startAt is INCLUSIVE, so the last stored key always re-arrives on reconnect;
  INSERT OR IGNORE (see db.py) makes that a harmless no-op.
* On any disconnect we re-read the last stored key from SQLite, refresh the
  access token if needed, and reconnect with exponential backoff. Samples the
  car pushed while we were down are picked up by the startAt query — that is the
  whole point of the car writing to an append-only node.
"""

import json
import time

import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest

import db
from pit_config import (
    DB_URL,
    TELEMETRY_PATH,
    SERVICE_ACCOUNT_PATH,
    SQLITE_PATH,
    DEVICE_ID,
    OAUTH_SCOPES,
    RECONNECT_BACKOFF_START,
    RECONNECT_BACKOFF_MAX,
    INITIAL_BACKFILL_LIMIT,
    DATA_SILENCE_TIMEOUT,
)

STREAM_URL = f"{DB_URL}/{TELEMETRY_PATH}.json"

# Read timeout for the streaming socket. RTDB sends a keep-alive every ~30-45s;
# if we see NO BYTES AT ALL for longer than this, the connection is treated as
# dead and we reconnect.
#
# ⚠️ This alone is NOT enough to detect a stalled feed, because a keep-alive is
# bytes: a stream that has stopped delivering telemetry but is still being
# pinged resets this timer forever and never trips. That failure has been
# observed on this pit wall (see DATA_SILENCE_TIMEOUT in pit_config.py, which
# is the actual guard against it). Keep both: this one catches a socket that
# has gone completely silent, that one catches a socket that is chatty but no
# longer carrying samples.
STREAM_READ_TIMEOUT = 70.0


class StreamStalled(RuntimeError):
    """Raised when the socket is healthy but no telemetry has arrived for
    DATA_SILENCE_TIMEOUT. Subclass of RuntimeError so the supervisor's existing
    `except Exception` reconnect path handles it with no special casing."""

# How much we pull off the socket per read.
#
# This was 64 KiB, and that made the live feed arrive in BURSTS instead of in
# real time. Measured on the pit wall: exactly 37 samples landed at once every
# ~20 seconds and nothing in between — 37 samples x ~1.7 KB is almost exactly
# 64 KiB. The read was waiting to FILL this buffer before yielding anything, so
# a car pushing twice a second reached the dashboard in one 20-second clump.
# The tiles sat there ageing past DATA_STALE_AFTER_S (10 s) and flipping to
# "Stale" between clumps, which is what "the Pi is live but the pit is not"
# actually looked like.
#
# 4 KiB is a couple of samples' worth, so latency is well under a second.
#
# The original 64 KiB was chosen to keep a big reconnect catch-up to a few
# large reads. That reasoning does not apply any more: the O(n^2) blow-up it
# was guarding against lived in requests' own iter_lines, and _iter_sse_lines
# below replaced it with a joiner that is O(n) at ANY chunk size. A smaller
# chunk here costs only more (cheap, constant-work) loop iterations, and the
# per-byte cost of the catch-up path is unchanged.
STREAM_CHUNK_BYTES = 4096


def _log(msg: str) -> None:
    print(f"[collector] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def load_credentials():
    """Service-account credentials scoped for RTDB REST access."""
    return service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_PATH, scopes=OAUTH_SCOPES
    )


def fresh_token(creds) -> str:
    """Return a valid OAuth2 access token, refreshing it if expired."""
    if not creds.valid:
        creds.refresh(GoogleAuthRequest())
    return creds.token


# --------------------------------------------------------------------------- #
# SSE parsing + ingest
# --------------------------------------------------------------------------- #
def _iter_sse_lines(resp):
    """Yield SSE lines (str) from a streaming response, in LINEAR time.

    Why not resp.iter_lines()? RTDB delivers a reconnect catch-up as a SINGLE
    'data:' line holding every missed record — tens of MB with no newline
    anywhere in it. requests' iter_lines accumulates that with `pending + chunk`
    at a 512-byte chunk size and re-runs splitlines() over the whole growing
    buffer on every chunk, which is O(n^2). A 20 MB catch-up became ~40k passes
    over an ever-growing string, on the order of a terabyte of copying: the
    collector pegged a core for the better part of an hour with nothing at all
    reaching the pit wall.

    That made the cost of a dropout quadratic in its length — a two-minute blip
    was invisible, an eleven-hour one never finished. Exactly backwards from
    what a race needs, where the long outage is the one you have to survive.

    So: hold unfinished chunks in a list, never recopying what we already have;
    join only once a chunk actually contains a newline; cut the joined buffer
    with find(). Every byte is copied a constant number of times. Decoding is
    per line, so a multi-byte character straddling a chunk boundary is
    reassembled before it is decoded rather than mangled.
    """
    pending = []  # chunks seen so far with no newline in them
    for chunk in resp.iter_content(chunk_size=STREAM_CHUNK_BYTES):
        if not chunk:
            continue  # urllib3 yields b"" as a keep-alive tick
        if b"\n" not in chunk:
            pending.append(chunk)
            continue

        buf = b"".join(pending) + chunk if pending else chunk
        pending = []
        start = 0
        while True:
            nl = buf.find(b"\n", start)
            if nl < 0:
                break
            yield buf[start:nl].decode("utf-8", "replace")
            start = nl + 1
        if start < len(buf):
            pending.append(buf[start:])

    if pending:  # server closed without a trailing newline
        yield b"".join(pending).decode("utf-8", "replace")


def _store(conn, key, record) -> int:
    """Upsert one (key, record). Returns rows actually inserted (0 or 1)."""
    if not isinstance(record, dict):
        return 0
    return db.upsert_many(conn, {key: record}, ingested_ts=time.time(), device_id=DEVICE_ID)


def _handle_put(conn, payload: dict) -> int:
    """A 'put' event. payload = {"path": ..., "data": ...}.

    * path "/"        → data is {key: record, ...} (initial backfill / catch-up
                        batch) or None (empty node). Upsert all.
    * path "/<key>"   → data is a single new record. Upsert it.
    Deeper paths would be partial mutations of a record; our records are
    immutable (pushed once, never edited), so we don't expect them.
    """
    path = payload.get("path", "/")
    data = payload.get("data")

    if path == "/":
        if not data:
            return 0
        if isinstance(data, dict):
            return db.upsert_many(conn, data, ingested_ts=time.time(), device_id=DEVICE_ID)
        _log(f"unexpected root data type {type(data).__name__}; ignoring")
        return 0

    # path like "/-Nabc123"
    key = path.strip("/").split("/")[0]
    if "/" in path.strip("/"):
        _log(f"ignoring partial update at {path} (records are immutable)")
        return 0
    return _store(conn, key, data)


def stream_once(conn, creds, start_after_key) -> str:
    """Open one streaming connection and process events until it drops.

    Returns the last successfully observed disposition so the caller can decide
    whether to reset backoff. Raises on connection/auth errors so the caller can
    reconnect. `start_after_key` is the cursor (inclusive startAt) or None for a
    full initial backfill on an empty DB.
    """
    params = {"orderBy": '"$key"'}
    if start_after_key:
        # startAt is inclusive — boundary key re-arrives and is de-duped by the
        # idempotent upsert. (startAfter exists on newer RTDB but startAt+IGNORE
        # is universally supported and equally correct here.)
        params["startAt"] = f'"{start_after_key}"'
        _log(f"streaming tail from key > {start_after_key}")
    elif INITIAL_BACKFILL_LIMIT:
        # Fresh machine, empty DB: don't pull the whole (ever-growing) node in one
        # giant initial event — that stalls a new laptop before it ever goes live.
        # Grab only the most recent N samples, then stream new ones incrementally.
        params["limitToLast"] = INITIAL_BACKFILL_LIMIT
        _log(f"no cursor yet — backfilling last {INITIAL_BACKFILL_LIMIT} sample(s), then live")
    else:
        _log("no cursor yet — initial full backfill of telemetry_history")

    headers = {
        "Authorization": f"Bearer {fresh_token(creds)}",
        "Accept": "text/event-stream",
    }

    with requests.get(
        STREAM_URL, params=params, headers=headers, stream=True,
        timeout=(10.0, STREAM_READ_TIMEOUT), allow_redirects=True,
    ) as resp:
        resp.raise_for_status()
        _log("stream connected")

        event_type = None
        data_buf = []
        # Watchdog clock. Deliberately reset ONLY by a real telemetry event
        # below, never by a keep-alive — see StreamStalled / the note on
        # STREAM_READ_TIMEOUT for the stall this exists to break out of.
        last_data_ts = time.time()

        for raw in _iter_sse_lines(resp):
            line = raw.rstrip("\r")

            # Checked here rather than only after an event, so a stream that is
            # delivering nothing but keep-alives still trips it. Keep-alives are
            # what give this loop control back on an otherwise idle socket.
            silent_for = time.time() - last_data_ts
            if silent_for > DATA_SILENCE_TIMEOUT:
                raise StreamStalled(
                    f"no telemetry for {silent_for:.0f}s despite a live socket "
                    f"(limit {DATA_SILENCE_TIMEOUT:.0f}s) — reconnecting")

            if line == "":
                # blank line terminates an event
                if event_type is not None:
                    if _dispatch(conn, event_type, "\n".join(data_buf)):
                        last_data_ts = time.time()
                event_type = None
                data_buf = []
                continue

            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_buf.append(line[len("data:"):].strip())
            # other SSE fields (id:, retry:, comments starting with ':') ignored

        # iterator ended cleanly == server closed the stream; treat as a drop
        _log("stream closed by server")
        return "closed"


def _dispatch(conn, event_type: str, data_str: str) -> bool:
    """Apply one fully-parsed SSE event.

    Returns True when this was a REAL telemetry event (as opposed to a
    keep-alive or an unparseable/ignored one), which is what resets the
    caller's stall watchdog. A keep-alive must return False — treating it as
    liveness is precisely the bug StreamStalled exists to catch.
    """
    if event_type in ("keep-alive",):
        return False
    if event_type == "auth_revoked":
        raise PermissionError("auth_revoked: access token rejected by RTDB")
    if event_type == "cancel":
        raise RuntimeError("cancel: stream cancelled by server (check DB rules)")

    if not data_str:
        return False
    try:
        payload = json.loads(data_str)
    except json.JSONDecodeError:
        _log(f"could not parse data for event '{event_type}'")
        return False

    if event_type == "put":
        n = _handle_put(conn, payload)
        if n:
            _log(f"+{n} new sample(s)  (total {db.count_samples(conn)})")
        # True even when n == 0: a duplicate/boundary key still proves the
        # stream is delivering telemetry, which is all the watchdog asks.
        return True
    if event_type == "patch":
        # records are immutable; a patch shouldn't occur. Log and skip.
        _log(f"ignoring 'patch' at {payload.get('path')}")
        return True
    _log(f"ignoring event '{event_type}'")
    return False


# --------------------------------------------------------------------------- #
# Main supervisor loop
# --------------------------------------------------------------------------- #
def run():
    _log(f"sqlite: {SQLITE_PATH}")
    _log(f"stream: {STREAM_URL}")
    conn = db.get_conn()
    db.init_db(conn)
    _log(f"resuming with {db.count_samples(conn)} sample(s) already stored")

    creds = load_credentials()
    backoff = RECONNECT_BACKOFF_START

    while True:
        # Fall back to the persisted cursor when the table is empty (e.g. just
        # after a pit-side history reset) so we resume from the tail instead of
        # re-backfilling everything that was cleared.
        last_key = db.get_last_key(conn) or db.get_cursor(conn)
        try:
            stream_once(conn, creds, last_key)
            # clean close — reconnect immediately (no penalty), reset backoff
            backoff = RECONNECT_BACKOFF_START
        except PermissionError as e:
            _log(f"{e} — forcing token refresh")
            try:
                creds.refresh(GoogleAuthRequest())
            except Exception as re:
                _log(f"token refresh failed: {re}")
            backoff = RECONNECT_BACKOFF_START
        except KeyboardInterrupt:
            _log("interrupted — shutting down")
            break
        except Exception as e:
            _log(f"stream error: {e.__class__.__name__}: {e}")
            _log(f"reconnecting in {backoff:.0f}s (catch-up via startAt on reconnect)")
            time.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)

    conn.close()


if __name__ == "__main__":
    run()
