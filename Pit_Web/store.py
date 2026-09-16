# Pit_Web/store.py — small persistent state the WEB dashboard owns.
#
# WHY THIS IS NOT IN db.py. This is the web backend's own state layer: only
# Pit_Web reads or writes what is here, so it lives beside api.py rather than in
# the Pit_Dashboard/ modules the collector and the tools share. These two
# helpers used to live in db.py.
#
# They touch app_state ONLY — the tiny key/value table beside the race clock,
# never the telemetry table. The "no new SQL outside db.py" rule exists so the
# programs reading telemetry.db cannot grow divergent readings of the CAR's
# data; a key/value row that only the web UI writes is not that. The telemetry
# path still goes through db.py's helpers exclusively.
#
# Also home to the driver-stint rule, which was in pit_config.py and moved here
# for the same reason.

import json
import sqlite3


# --- Driver stints ---------------------------------------------------------- #
# The regulations cap how long one driver may stay in the car. Missing a change
# is a penalty, so the countdown is a first-class readout on the pit wall.
#
# Stint time accumulates only while the RACE clock runs — see driver_stint() in
# api.py. Wall time would drain through setup and show a false OVERDUE before
# the race had started.
DRIVER_STINT_LIMIT_S = 2 * 3600      # 2 hours

# Amber when this much is left, red when this much is left. Tuned so amber
# arrives while there is still time to get the next driver suited and to the
# wall, and red means "they should be moving now".
DRIVER_STINT_WARN_S = 15 * 60
DRIVER_STINT_CRIT_S = 5 * 60

# How long "Undo last change" stays available. A mis-click during a pit stop
# restarts a two-hour countdown, which is an expensive thing to not take back.
DRIVER_STINT_UNDO_S = 120

# How long "Undo race reset" stays available. Resetting the race clock throws
# away a start time that cannot be reconstructed, so the mistake has to be
# reversible for long enough to notice it.
RACE_UNDO_S = 120


def save_app_state(conn: sqlite3.Connection, key: str, value) -> None:
    """Persist one JSON-able value in app_state under `key`.

    For state that must outlive a browser refresh AND read the same on every
    device watching — the driver-stint clock, for instance. Anything that only
    matters to one browser belongs in localStorage, not here.
    """
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()


def load_app_state(conn: sqlite3.Connection, key: str, default=None):
    """The value stored under `key`, or `default` if unset or unparseable."""
    row = conn.execute(
        "SELECT value FROM app_state WHERE key = ?", (key,)
    ).fetchone()
    if not row or not row["value"]:
        return default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return default
