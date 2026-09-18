# firebase_client.py

import json
import os
import threading
import time
import firebase_admin
from firebase_admin import credentials
from firebase_admin import db
from firebase_admin import exceptions as firebase_exceptions

from edge_sync import Client as OutboxClient, RefetchBatch, RejectedError
from edge_sync.queue import clean_json

# ==============================================================================
# FIREBASE CONFIGURATION
# ==============================================================================

# How often to send data to the cloud (in seconds)
# 0.5s gives the pit smoother, more real-time data (at ~2x the write volume).
UPDATE_INTERVAL_SECONDS = 0.5

# Append-only history node read by the pit-side SQLite collector.
# This is ADDITIVE: live_telemetry keeps being overwritten exactly as before;
# we ALSO write each throttled sample here so the pit can stream new samples
# incrementally (orderBy="$key") and backfill gaps after its own dropouts.
# Every sample's key is chronological and unique (it becomes the pit's primary
# key), so unlike live_telemetry, nothing here is ever overwritten. The keys
# come from the outbox below, NOT from push(): see THE OUTBOX.
HISTORY_PATH = 'telemetry_history'
LIVE_PATH = 'live_telemetry'

# Seconds any single Firebase request may take before it is abandoned. See the
# note in initialize_firebase: the alternative is the library's 120 s default,
# on the thread that reads the CAN bus.
UPLOAD_HTTP_TIMEOUT_S = 8.0

# State variable to track the last time we pinged the server
_last_update_time = 0


# ==============================================================================
# UPLOAD HEALTH — what the driver HUD's PIT badge reads
# ==============================================================================
# The HUD's NET badge proves the radio link is up. It cannot prove the pit is
# receiving anything, and the two come apart exactly when it matters: an expired
# service-account key, a blocked port, a Firebase outage, a full quota. Every
# one of those leaves 8.8.8.8 perfectly reachable while the pit wall goes blind.
#
# So this records whether the last push actually LANDED, and the HUD shows it
# next to the link light. NET green + PIT red is the specific, common failure
# that used to be invisible from the driver's seat.
#
# An attempt counts as a success only when BOTH writes went through. The live
# node feeds the pit's "now"; telemetry_history feeds the pit's SQLite, which is
# what the strategy screens and the exports are actually built from. A live-only
# success means the pit's RECORD has a hole in it even though the dial moved,
# and "the pit can see me" should not be true while that is happening.
#
# Read from the HUD's GUI thread, written from the CAN worker thread, hence the
# lock. Every field is a plain scalar copied out under it — get_upload_status()
# does no I/O and cannot block the caller.

STATUS_UNKNOWN = "unknown"   # nothing has ever been attempted (Firebase unused)
STATUS_IDLE = "idle"         # nothing to send lately — quiet bus, no GPS fix
STATUS_UP = "up"             # a push landed recently
STATUS_DOWN = "down"         # attempts are being made and they are failing

# How long a success stays "current". Ten throttled intervals: long enough that
# an ordinary gap between CAN frames never blinks the badge, short enough that a
# car which stopped uploading stops claiming it is uploading.
UPLOAD_STALE_AFTER_S = 5.0

# Consecutive failures before the badge goes red. Same asymmetry as the link
# probe: one failed write on a cellular link is weather, not an outage, and a
# badge that reacts to weather is a badge the driver stops reading.
FAILURES_BEFORE_DOWN = 2

_health_lock = threading.Lock()
_upload_ok_time = 0.0        # time.time() of the last fully successful push
_upload_attempt_time = 0.0   # time.time() of the last push that actually ran
_upload_failures = 0         # consecutive failed attempts
_upload_error = None         # text of the most recent failure
_upload_ok_count = 0         # proves pushes are really happening


def _record_upload(ok, error=None):
    """Fold one push attempt into the health snapshot. Never raises.

    Only called for attempts that actually ran — a call skipped by the
    UPDATE_INTERVAL_SECONDS throttle is not evidence of anything and must not
    be mistaken for either a success or a failure.
    """
    global _upload_ok_time, _upload_attempt_time
    global _upload_failures, _upload_error, _upload_ok_count
    now = time.time()
    with _health_lock:
        _upload_attempt_time = now
        if ok:
            _upload_ok_time = now
            _upload_failures = 0
            _upload_error = None
            _upload_ok_count += 1
        else:
            _upload_failures += 1
            _upload_error = str(error) if error is not None else "unknown"


def get_upload_status():
    """Snapshot of whether telemetry is reaching the pit. Never blocks or raises.

    Always a dict:

        upload_status     STATUS_* — the value a status light should show
        upload_ok         True / False / None, None meaning "not uploading"
        upload_age_s      seconds since the last landed push, None if never
        upload_failures   consecutive failures right now
        upload_error      text of the last failure, or None
        upload_backlog    samples saved on the Pi, not yet in Firebase
                          (None when the outbox is not running)
    """
    now = time.time()
    with _health_lock:
        ok_time = _upload_ok_time
        attempt_time = _upload_attempt_time
        failures = _upload_failures
        error = _upload_error
        count = _upload_ok_count

    if not attempt_time:
        # Never attempted. This is the standalone-HUD and bench case, and it is
        # NOT "down" — reporting a failure for a subsystem nobody asked to run
        # is how a warning light teaches the driver to ignore it.
        status = STATUS_UNKNOWN
    elif failures >= FAILURES_BEFORE_DOWN:
        status = STATUS_DOWN
    elif ok_time and (now - ok_time) <= UPLOAD_STALE_AFTER_S:
        status = STATUS_UP
    else:
        # Attempts have happened, but nothing landed lately and nothing is
        # failing either: the car simply has nothing to send (quiet CAN bus, no
        # GPS fix — see the guard in main.py's GPS publish path).
        status = STATUS_IDLE

    return {
        "upload_status": status,
        "upload_ok": (True if status == STATUS_UP else
                      False if status == STATUS_DOWN else None),
        "upload_age_s": (now - ok_time) if ok_time else None,
        "upload_failures": failures,
        "upload_error": error,
        "upload_ok_count": count,
        "upload_backlog": (_outbox.stats()["pending"]
                           if _outbox is not None else None),
    }

def initialize_firebase(credential_file_path, database_url):
    """
    Initializes the Firebase Realtime Database connection.
    Call this ONCE at the start of main.py.
    """
    try:
        print("Connecting to Firebase Pit Wall...")
        cred = credentials.Certificate(credential_file_path)
        firebase_admin.initialize_app(cred, {
            'databaseURL': database_url,
            # firebase-admin defaults to 120 SECONDS per request, and these
            # writes are synchronous on the CAN worker thread -- the same thread
            # that drains the bus. One hung socket at the default would stop
            # frame decoding for two minutes, overflow the SocketCAN receive
            # buffer and silently lose everything in it.
            #
            # 8 s is far longer than a healthy write (measured ~40 ms at the
            # bench) and long enough for a bad cellular link to still get a
            # payload through, while capping what a single dead socket can cost.
            # Note firebase-admin retries inside one call, so this bounds each
            # ATTEMPT rather than the whole call.
            'httpTimeout': UPLOAD_HTTP_TIMEOUT_S,
        })
        print("Firebase connection established successfully.")
    except Exception as e:
        print(f"CRITICAL: Failed to initialize Firebase: {e}")

# ==============================================================================
# THE OUTBOX — a bad link delays telemetry, it never loses it
# ==============================================================================
# Every throttled sample is first saved to a SQLite outbox on the Pi, then a
# background thread uploads it (edge_sync, vendored in SolarRace_OS/edge_sync/).
# Two things this fixes, both measured on the car's own data:
#
#   1. A sample taken while the link was down used to be gone for good: the
#      write failed and nothing kept it. Now it waits on disk (across reboots
#      too) and is uploaded when the link returns.
#   2. The upload used to run ON THE CAN WORKER THREAD. A slow cellular write
#      stalled frame decoding, and LapTracker drops any energy interval longer
#      than 2 s — the car's Wh read 5-20% low exactly on the drives with link
#      trouble. The CAN thread now only does a local disk write.
#
# ORDER MATTERS FOR THE PIT. collector.py resumes from the newest key it has
# (orderBy $key, startAt). A backlog uploaded newest-first, or under keys that
# sort below what the pit already holds, would be skipped forever. So:
#   * the outbox drains OLDEST-first, one set() per sample, in order;
#   * keys are the outbox's own ids — push-key format, strictly increasing
#     across reboots and wall-clock jumps;
#   * before the first upload, the outbox is reconciled against the newest key
#     already in Firebase. A Pi with no battery-backed clock can boot an hour
#     behind after a hard power-off, and its keys would otherwise sort low.
#
# If the outbox cannot be opened at all (disk error), samples go straight to
# Firebase the old way rather than nowhere.
OUTBOX_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "telemetry_outbox.db")

# Samples per batch. Each is its own request (see _send_batch_to_firebase), so
# at ~100 ms a write on cellular a backlog drains at ~10 samples a second, five
# times faster than the car produces them: a 10-minute outage (1,200 samples)
# is caught up in about 2.5 minutes. Kept small so the live node, written once
# per batch, never falls more than a couple of seconds behind while it drains.
OUTBOX_BATCH_SIZE = 10
OUTBOX_FLUSH_INTERVAL_S = UPDATE_INTERVAL_SECONDS
# Longest wait between upload retries while the link is down. edge_sync's own
# default is 30 s, which would leave the pit blind for up to half a minute
# after the link comes back. One small request every 5 s costs nothing.
OUTBOX_MAX_BACKOFF_S = 5.0
OUTBOX_RETRY_OPEN_S = 30.0
OUTBOX_CLOSE_TIMEOUT_S = 3.0

_outbox = None
_outbox_lock = threading.Lock()
_outbox_failed_at = 0.0
_outbox_reconciled = False

# The newest sample, for live_telemetry and the spectator node. Those two are
# "latest wins", so they are written from memory, never from the backlog.
_latest_payload = None
_latest_lock = threading.Lock()


class _OutboxReconciled(RefetchBatch):
    """Raised by the sender after renumbering or clearing queued samples, so
    edge_sync fetches the batch again with the corrected keys."""


def _get_outbox():
    """The running outbox, started on first use. None if it cannot open."""
    global _outbox, _outbox_failed_at
    if _outbox is not None:
        return _outbox
    if _outbox_failed_at and time.monotonic() - _outbox_failed_at < OUTBOX_RETRY_OPEN_S:
        return None
    with _outbox_lock:
        if _outbox is None:
            try:
                _outbox = OutboxClient(
                    "firebase", "", "solar-car",
                    db_path=OUTBOX_PATH,
                    batch_size=OUTBOX_BATCH_SIZE,
                    flush_interval=OUTBOX_FLUSH_INTERVAL_S,
                    max_backoff=OUTBOX_MAX_BACKOFF_S,
                    drain="oldest",
                    sender=_send_batch_to_firebase,
                )
                waiting = _outbox.stats()["pending"]
                print(f"📦 Telemetry outbox open: {OUTBOX_PATH}"
                      + (f" — {waiting} sample(s) from before still to upload"
                         if waiting else ""))
            except Exception as exc:
                _outbox_failed_at = time.monotonic()
                print(f"CRITICAL: telemetry outbox unavailable, uploading "
                      f"directly (no buffering): {exc}")
    return _outbox


def _reconcile_with_firebase():
    """Once per run: line the outbox up with the newest key in Firebase."""
    global _outbox_reconciled
    newest = db.reference(HISTORY_PATH).order_by_key().limit_to_last(1).get()
    if newest:
        key = next(iter(newest))
        try:
            acked, renumbered = _outbox.reconcile(key)
        except ValueError:
            # Not a push-format key, so it cannot be compared; nothing to do.
            acked = renumbered = 0
        _outbox_reconciled = True
        if acked or renumbered:
            print(f"📦 Outbox reconciled with Firebase: {acked} sample(s) were "
                  f"already uploaded, {renumbered} renumbered to sort after {key}")
            raise _OutboxReconciled("outbox reconciled; refetching batch")
    _outbox_reconciled = True


def _send_batch_to_firebase(batch):
    """edge_sync sender: one set() per sample, in order, then the live node.

    NOT one multi-path update(), although that would be a single request: the
    pit's stream delivers an update() as a 'patch' event, which collector.py
    ignores while still counting it as traffic — the pit would silently stop
    receiving data. A set() on each key arrives as a 'put', exactly like the
    push() it replaces. Rewriting a key on retry is harmless: same key, same
    record, and the collector's upsert ignores the repeat.

    Samples go in recorded order, so if the power dies part-way the newest key
    in Firebase is the last one that landed, which is what
    _reconcile_with_firebase relies on at the next start.

    Runs on the outbox's own thread, never the CAN worker. Return = delivered;
    RejectedError = Firebase will never accept this data (it is kept aside on
    disk and stops blocking the queue); anything else = retry later.
    """
    try:
        if not _outbox_reconciled:
            _reconcile_with_firebase()
        for p in batch["points"]:
            db.reference(f"{HISTORY_PATH}/{p['id']}").set(p["data"])
        with _latest_lock:
            latest = _latest_payload
        if latest is not None:
            db.reference(LIVE_PATH).set(latest)
    except _OutboxReconciled:
        raise
    except firebase_exceptions.InvalidArgumentError as exc:
        print(f"[Network Error] Firebase refused telemetry data: {exc}")
        _record_upload(False, f"refused: {exc}")
        raise RejectedError(str(exc)) from exc
    except Exception as exc:
        print(f"[Network Error] Failed to upload telemetry: {exc}")
        _record_upload(False, exc)
        raise
    _record_upload(True)

    # The spectator feed rides along AFTER the pit's write, so it can never
    # delay or displace it, and its own failure is caught inside
    # push_public_snapshot rather than here.
    if latest is not None:
        push_public_snapshot(latest.get("car_data") or {})


def stop_telemetry_uploader(flush_timeout=OUTBOX_CLOSE_TIMEOUT_S):
    """Upload what fits in `flush_timeout`, then close the outbox.

    Whatever is left stays in telemetry_outbox.db and uploads at next start,
    so a big backlog cannot hold up shutting the car down.
    """
    global _outbox
    with _outbox_lock:
        outbox, _outbox = _outbox, None
    if outbox is not None:
        left = outbox.stats()["pending"]
        outbox.close(flush_timeout=flush_timeout)
        if left:
            print(f"📦 Outbox closed; up to {left} sample(s) kept on disk for next start")


def push_telemetry_to_cloud(vehicle_state):
    """
    Records the current vehicle state for the pit, throttled to
    UPDATE_INTERVAL_SECONDS.

    Does NO network I/O: the sample is saved to the outbox and uploaded by its
    own thread (see THE OUTBOX above). The only cost on the calling thread is
    one small local disk write.
    """
    global _last_update_time, _latest_payload
    current_time = time.time()

    if (current_time - _last_update_time) < UPDATE_INTERVAL_SECONDS:
        return
    # Stamped before anything can fail, for the reason explained in
    # _push_directly: a failure must never defeat the throttle.
    _last_update_time = current_time
    payload = {"timestamp": current_time, "car_data": vehicle_state}

    outbox = _get_outbox()
    if outbox is None:
        _push_directly(payload)
        return

    try:
        # A deep copy, taken on the thread that owns vehicle_state, so the
        # uploader thread never reads a dict that is being changed under it.
        snapshot = json.loads(json.dumps(clean_json(payload), default=str))
    except Exception as exc:
        print(f"[Telemetry] could not serialise vehicle state: {exc}")
        return
    with _latest_lock:
        _latest_payload = snapshot
    if outbox.track_snapshot(snapshot, ts=int(current_time * 1000)) is None:
        # The outbox could not store it (disk error). Better sent without a
        # safety net than not sent at all.
        _push_directly(snapshot)


def _push_directly(payload):
    """The pre-outbox upload path, kept as the fallback. Blocks on the network.

    Only used when the outbox cannot store a sample. The caller has already
    stamped _last_update_time BEFORE calling this, and that ordering matters:
    when the stamp came after the write, a write that RAISED never reached it,
    the throttle stayed open, and every CAN frame -- hundreds a second --
    attempted a full blocking HTTPS write, exactly when the link was bad.
    """
    try:
        # .set() OVERWRITES the live node: the pit's "now".
        db.reference(LIVE_PATH).set(payload)

        # Also append an immutable copy to the history node the pit collector
        # stores. push() keys here, since there is no outbox to assign them.
        # Wrapped separately so a history hiccup never affects the live snapshot.
        try:
            db.reference(HISTORY_PATH).push(payload)
            _record_upload(True)
        except Exception as hist_err:
            print(f"[Network Error] Failed to append telemetry history: {hist_err}")
            # The live snapshot DID land, but the pit's stored record now has a
            # hole, so this is not a healthy upload. See the header note on why
            # the badge reports the AND of both writes.
            _record_upload(False, f"history: {hist_err}")

        push_public_snapshot(payload.get("car_data") or {})

    except Exception as e:
        # We don't want a network drop to crash the whole car system
        print(f"[Network Error] Failed to update Firebase: {e}")
        _record_upload(False, e)


# ==============================================================================
# THE PUBLIC NODE — the only thing the outside world can read
# ==============================================================================
# Everything else in this database is readable only with the service-account
# key. This one node is world-readable, because it feeds the spectator page
# that people at home watch the race on, and that page is a plain static file
# with no credentials of any kind — it cannot be given a key without giving the
# key to everyone who opens it.
#
# So the snapshot below is a WHITELIST, not a copy of vehicle_state. Three
# reasons it is written out field by field rather than filtered from the live
# payload:
#
#   1. A field is public because it is listed here. Nobody makes something
#      public by accident, and adding a metric to the car does not silently
#      publish it.
#   2. POSITION IS PUBLIC, BY DECISION. lat/lon used to be held back so the
#      page placed the car from lap_distance_m along the baked centreline --
#      right corner, no real coordinates. The team chose to publish the true
#      position instead, because lap_distance_m is a distance since a datum and
#      a datum that is stale (a Pi restarted mid-lap, a trip reset taken in the
#      garage) puts the public marker somewhere the car is not.
#
#      Know what this means: this node has no credentials, so the live racing
#      line, the pit stops and the exact speed through every corner are readable
#      by anyone with the URL, rival teams included, and stay readable once
#      copied. Removing the two lines below is all it takes to go back.
#   3. Size. This is read by every viewer's browser every time it changes, and
#      RTDB egress is metered. The full payload is a few hundred fields of
#      cell voltages and thermistors; this is nine numbers.
PUBLIC_PATH = 'public/live'

# Slower than the 0.5 s pit feed on purpose. The pit is making decisions off
# its data; a spectator watching a car go round a 4 km lap cannot see the
# difference between one update a second and two, and every viewer pays for
# every update in bandwidth.
PUBLIC_UPDATE_INTERVAL_SECONDS = 1.0

_last_public_update = 0


def _public_snapshot(vehicle_state):
    """The whitelist, resolved against one vehicle_state. Never raises.

    Missing readings stay MISSING — the keys come out None and firebase-admin
    drops them from the node, so the page renders "—" rather than a confident
    zero. A spectator page showing 0% state of charge because the BMS went
    quiet would read as a dead car to exactly the audience least able to tell
    the difference.
    """
    motor = vehicle_state.get("motor") or {}
    battery = vehicle_state.get("battery") or {}
    gps = vehicle_state.get("gps") or {}
    return {
        # Server-independent: the page compares this against its own clock to
        # decide whether the feed is live, so it must be the moment the car
        # sampled, not the moment anything received it.
        "ts": time.time(),
        "lap": motor.get("calculated_lap"),
        # Position, when there is a CURRENT fix. Two guards, both needed:
        #
        #   * gps is left EMPTY until the first real fix (main._refresh_gps), so
        #     before that these are None, firebase-admin drops them, and the
        #     page falls back to lap_distance_m instead of drawing the car at
        #     0,0 in the Atlantic.
        #   * once the receiver loses lock the car KEEPS serving the last known
        #     fix, flagged stale (gps_reader.FIX_STALE_AFTER_S) -- a frozen dot
        #     beats an empty map on the driver's screen. Publishing that to a
        #     page whose whole job is to show where the car is now would be a
        #     lie that looks exactly like the truth: seen on 2026-09-18, the
        #     marker sat still for 27 minutes while the car drove two laps.
        #     A stale position is simply not published, and the page falls back
        #     to lap_distance_m, which is live.
        "lat": None if gps.get("stale") else gps.get("lat"),
        "lon": None if gps.get("stale") else gps.get("lon"),
        # Sent even when the position is not, and that is the point: it is how
        # the page says "no GPS, last fix 57 minutes ago" instead of silently
        # falling back to lap_distance_m and looking like it never had one.
        "gps_age_s": gps.get("fix_age_s"),
        "lap_distance_m": motor.get("lap_distance_m"),
        "odometer_m": motor.get("odometer_m"),
        "speed_kmh": motor.get("mms_vehicle_speed_kmh"),
        "last_lap_time_s": motor.get("last_lap_time_s"),
        "soc_percent": battery.get("bms_soc_percent"),
    }


def push_public_snapshot(vehicle_state):
    """Best-effort write of the spectator snapshot. NEVER affects the pit feed.

    Called from push_telemetry_to_cloud after the pit's own writes have gone
    out, inside its own try/except, and deliberately NOT folded into
    _record_upload(): the PIT badge on the driver's HUD answers one question —
    "can the pit see me" — and a spectator page failing to update is not an
    answer to it. Turning the badge red because a family page went stale would
    train the driver to ignore the one light that tells them the pit wall has
    gone blind.
    """
    global _last_public_update
    now = time.time()
    if (now - _last_public_update) < PUBLIC_UPDATE_INTERVAL_SECONDS:
        return
    _last_public_update = now
    try:
        db.reference(PUBLIC_PATH).set(_public_snapshot(vehicle_state))
    except Exception as exc:
        print(f"[Network Error] Failed to update the public snapshot: {exc}")


# Node the pit writes short driver instructions to (category + value). "Latest
# wins" — the pit overwrites the whole node, or deletes it to clear the HUD.
DRIVER_COMMAND_PATH = 'driver_command'


# Node the pit writes lap commands to (cut a lap, correct the lap number).
# DELIBERATELY SEPARATE from DRIVER_COMMAND_PATH: that node is "latest wins" for
# driver text and is DELETED to clear the HUD banner, so sharing it would mean a
# lap cut wipes the driver's message — and a "clear message" would arrive at the
# lap handler as a null event.
LAP_COMMAND_PATH = 'lap_command'
LAP_COMMAND_ACK_PATH = 'lap_command_ack'

# Which speed profile the car should follow. Its own node for the same reason
# lap commands got one: /driver_command is latest-wins driver text and is
# DELETED to clear the banner, so sharing it would make a strategy change wipe
# the driver's message.
STRATEGY_COMMAND_PATH = 'strategy_command'
STRATEGY_ACK_PATH = 'strategy_ack'


def listen_strategy_command(callback):
    """Subscribe to /strategy_command. Same background-thread contract as
    listen_lap_command — queue the command, never touch car state here."""
    return db.reference(STRATEGY_COMMAND_PATH).listen(callback)


def ack_strategy(cmd_id, strategy, applied, note=None):
    """Report back which profile the car is actually running.

    Worth the extra write: the pit is choosing how hard the car is driven for
    the next hour, and "the message left the pit" is not the same as "the car
    changed profile". Best-effort — a failed ack never disturbs the car.
    """
    try:
        db.reference(STRATEGY_ACK_PATH).set({
            "id": cmd_id, "strategy": strategy,
            "applied": bool(applied), "note": note, "ts": time.time(),
        })
    except Exception as exc:
        print(f"[Network Error] Failed to ack strategy {cmd_id}: {exc}")


def listen_lap_command(callback):
    """Subscribe to /lap_command. Same contract as listen_driver_command.

    IMPORTANT: `callback(event)` runs on a firebase-admin BACKGROUND thread. It
    must not touch the lap tracker or vehicle_state — both belong to the CAN
    worker thread. Queue the command and let that thread apply it.

    The node is retained, so the listener re-fires the current value on every
    reconnect and again at startup. The caller is responsible for ignoring
    repeats (see modules/lap_command.py).
    """
    return db.reference(LAP_COMMAND_PATH).listen(callback)


def ack_lap_command(cmd_id, action, applied, lap=None, note=None):
    """Tell the pit a lap command landed, so it can show "applied" not "sent".

    Best-effort: an ack that fails to send must never disturb the car, so all
    errors are swallowed with a log line.
    """
    try:
        db.reference(LAP_COMMAND_ACK_PATH).set({
            "id": cmd_id,
            "action": action,
            "applied": bool(applied),
            "lap": lap,
            "note": note,
            "ts": time.time(),
        })
    except Exception as exc:
        print(f"[Network Error] Failed to ack lap command {cmd_id}: {exc}")


def listen_driver_command(callback):
    """Subscribe to /driver_command with a push-based realtime stream.

    Returns a ListenerRegistration (keep a reference; call .close() to stop).
    This opens ONE long-lived connection and fires `callback` only when the pit
    changes the command — no polling, negligible bandwidth on top of the 0.5s
    telemetry push.

    IMPORTANT: `callback(event)` runs on a firebase-admin background thread, so
    it must NOT touch any GUI objects — only marshal the data to the GUI thread
    (e.g. emit a Qt signal). `event.data` is the node value (dict, or None when
    cleared); `event.path` is '/' for a whole-node write.
    """
    return db.reference(DRIVER_COMMAND_PATH).listen(callback)