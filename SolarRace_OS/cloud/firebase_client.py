# firebase_client.py

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
            except Exception as hist_err:
                print(f"[Network Error] Failed to append telemetry history: {hist_err}")

        except Exception as e:
            # We don't want a network drop to crash the whole car system
            print(f"[Network Error] Failed to update Firebase: {e}")


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