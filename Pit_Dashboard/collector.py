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
)

STREAM_URL = f"{DB_URL}/{TELEMETRY_PATH}.json"

# Read timeout for the streaming socket. RTDB sends a keep-alive every ~30-45s;
# if we see nothing for longer than this, the connection is treated as dead and
# we reconnect.
STREAM_READ_TIMEOUT = 70.0

# How much we pull off the socket per read. Deliberately large: a reconnect
# catch-up arrives as one enormous line, and we want a few big reads rather than
# thousands of tiny ones. See _iter_sse_lines for why that matters so much.
STREAM_CHUNK_BYTES = 1 << 16  # 64 KiB


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

        for raw in _iter_sse_lines(resp):
            line = raw.rstrip("\r")

            if line == "":
                # blank line terminates an event
                if event_type is not None:
                    _dispatch(conn, event_type, "\n".join(data_buf))
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


def _dispatch(conn, event_type: str, data_str: str):
    """Apply one fully-parsed SSE event. Returns None (kept simple)."""
    if event_type in ("keep-alive",):
        return None
    if event_type == "auth_revoked":
        raise PermissionError("auth_revoked: access token rejected by RTDB")
    if event_type == "cancel":
        raise RuntimeError("cancel: stream cancelled by server (check DB rules)")

    if not data_str:
        return None
    try:
        payload = json.loads(data_str)
    except json.JSONDecodeError:
        _log(f"could not parse data for event '{event_type}'")
        return None

    if event_type == "put":
        n = _handle_put(conn, payload)
        if n:
            _log(f"+{n} new sample(s)  (total {db.count_samples(conn)})")
    elif event_type == "patch":
        # records are immutable; a patch shouldn't occur. Log and skip.
        _log(f"ignoring 'patch' at {payload.get('path')}")
    else:
        _log(f"ignoring event '{event_type}'")
    return None


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
