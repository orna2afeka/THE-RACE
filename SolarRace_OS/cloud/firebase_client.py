# firebase_client.py

import threading
import time
import firebase_admin
from firebase_admin import credentials
from firebase_admin import db

# ==============================================================================
# FIREBASE CONFIGURATION
# ==============================================================================

# How often to send data to the cloud (in seconds)
# 0.5s gives the pit smoother, more real-time data (at ~2x the write volume).
UPDATE_INTERVAL_SECONDS = 0.5

# Append-only history node read by the pit-side SQLite collector.
# This is ADDITIVE: live_telemetry keeps being overwritten exactly as before;
# we ALSO push() each throttled sample here so the pit can stream new samples
# incrementally (orderBy="$key") and backfill gaps after its own dropouts.
# push() generates a chronological, unique key per sample (that key becomes the
# pit's primary key), so unlike live_telemetry, nothing here is ever overwritten.
HISTORY_PATH = 'telemetry_history'

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
            'databaseURL': database_url
        })
        print("Firebase connection established successfully.")
    except Exception as e:
        print(f"CRITICAL: Failed to initialize Firebase: {e}")

def push_telemetry_to_cloud(vehicle_state):
    """
    Pushes the current vehicle state and strategy to the cloud.
    Includes a throttle mechanism to prevent network flooding.
    """
    global _last_update_time
    current_time = time.time()

    # Check if enough time has passed since the last update
    if (current_time - _last_update_time) >= UPDATE_INTERVAL_SECONDS:
        try:
            # We use a specific node in the database called 'live_telemetry'
            ref = db.reference('live_telemetry')
            
            # Construct the payload
            payload = {
                "timestamp": current_time,
                "car_data": vehicle_state
            }
            
            # .set() OVERWRITES the current data at this node.
            # This is perfect for a live dashboard (we only care about the NOW).
            ref.set(payload)

            # Update our timer
            _last_update_time = current_time

            # ADDITIVE: also append an immutable, keyed copy to the history node so
            # the pit wall can store full history locally (.push() never overwrites).
            # Wrapped separately so a history hiccup never affects the live snapshot.
            try:
                db.reference(HISTORY_PATH).push(payload)
                _record_upload(True)
            except Exception as hist_err:
                print(f"[Network Error] Failed to append telemetry history: {hist_err}")
                # The live snapshot DID land, and the separate try/except above
                # keeps it that way — but the pit's stored record now has a hole,
                # so this is not a healthy upload. See the header note on why the
                # badge reports the AND of both writes.
                _record_upload(False, f"history: {hist_err}")

            # The spectator feed rides along AFTER both pit writes, so it can
            # never delay or displace them, and its own failure is caught
            # inside push_public_snapshot rather than here.
            push_public_snapshot(vehicle_state)

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
#   2. NO POSITION. lat/lon are deliberately absent: the page places the car
#      from lap_distance_m along the baked centreline, which puts it in the
#      right corner without broadcasting where the car actually is. The
#      spectator map is a schematic and does not need better.
#   3. Size. This is read by every viewer's browser every time it changes, and
#      RTDB egress is metered. The full payload is a few hundred fields of
#      cell voltages and thermistors; this is eight numbers.
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
    solar = vehicle_state.get("solar") or {}
    return {
        # Server-independent: the page compares this against its own clock to
        # decide whether the feed is live, so it must be the moment the car
        # sampled, not the moment anything received it.
        "ts": time.time(),
        "lap": motor.get("calculated_lap"),
        "lap_distance_m": motor.get("lap_distance_m"),
        "odometer_m": motor.get("odometer_m"),
        "speed_kmh": motor.get("mms_vehicle_speed_kmh"),
        "last_lap_time_s": motor.get("last_lap_time_s"),
        "soc_percent": battery.get("bms_soc_percent"),
        "solar_current_A": solar.get("solar_current_A"),
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