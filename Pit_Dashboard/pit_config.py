"""
pit_config.py — Central configuration for the pit-side telemetry stack
======================================================================
The pit wall NEVER writes to Firebase. One process (collector.py) is the only
thing that talks to Firebase; everything else (dashboard, export) reads from the
local SQLite file, which is the pit's source of truth.

    Firebase RTDB  ──(collector.py, SSE stream)──►  telemetry.db (SQLite)
                                                          │
                                          ┌───────────────┼────────────────┐
                                          ▼               ▼                ▼
                                   pit_dashboard.py    export.py     (anything else)
"""

import os

# --- Where this file lives (so paths work regardless of the launch dir) ----- #
_HERE = os.path.dirname(os.path.abspath(__file__))

# --- Firebase Realtime Database ---------------------------------------------- #
# Same project the car writes to (see SolarRace_OS/cloud/firebase_client.py).
DB_URL = "https://solar-race-telemetry-default-rtdb.europe-west1.firebasedatabase.app"

# Append-only history node the car pushes each sample to (matches HISTORY_PATH
# in SolarRace_OS/cloud/firebase_client.py). The collector streams this node;
# it NEVER reads live_telemetry (that single node has no history to catch up on).
TELEMETRY_PATH = "telemetry_history"

# Service-account JSON (Firebase Admin key). Gitignored — keep it out of git.
# Used to mint an OAuth2 access token for the REST streaming endpoint.
SERVICE_ACCOUNT_PATH = os.path.join(_HERE, "serviceAccountKey.json")

# --- Local storage ----------------------------------------------------------- #
# The pit's source of truth. Lives next to this file by default.
SQLITE_PATH = os.path.join(_HERE, "telemetry.db")

# Logical device id stored with every row. There is one car today, but keeping a
# column means a second car (or a replay session) can coexist without a schema
# change, and export can filter by it.
DEVICE_ID = "solarcar"

# ── Export timezone: Tel Aviv before the team travels, Belgium after ─────── #
# Exports used to be UTC, which is unambiguous and useless: nobody comparing a
# stint to a radio call wants to do arithmetic first. So the workbook is written
# in the timezone the team was actually IN when the sample was recorded.
#
# It changes mid-season, which is why this is a pair and not one setting: the
# car runs in Israel now and the race is at Zolder in Belgium. A single zone
# would misdate one half of the history by an hour or two.
#
# THE SWITCH INSTANT is read in EXPORT_TZ_BEFORE, so "2026-09-14T00:00:00"
# means: everything up to midnight at the end of 13 September, Tel Aviv time,
# is Tel Aviv; from that moment on it is Brussels. Change this one string if the
# travel date moves - nothing else needs touching.
#
# Both zones are IANA names, not fixed offsets, deliberately: they carry their
# own daylight-saving rules. Israel and Belgium both leave summer time in late
# October, on different dates, and a hard-coded +3/+2 would silently rot then.
EXPORT_TZ_BEFORE = "Asia/Jerusalem"
EXPORT_TZ_AFTER = "Europe/Brussels"
EXPORT_TZ_SWITCH_LOCAL = "2026-09-14T00:00:00"


def _switch_epoch():
    """The switch instant as a unix timestamp, resolved once at import."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    naive = datetime.fromisoformat(EXPORT_TZ_SWITCH_LOCAL)
    return naive.replace(tzinfo=ZoneInfo(EXPORT_TZ_BEFORE)).timestamp()


EXPORT_TZ_SWITCH_EPOCH = _switch_epoch()


def export_zone(ts):
    """The ZoneInfo a sample recorded at unix time `ts` should be shown in."""
    from zoneinfo import ZoneInfo
    if ts is None or ts >= EXPORT_TZ_SWITCH_EPOCH:
        return ZoneInfo(EXPORT_TZ_AFTER)
    return ZoneInfo(EXPORT_TZ_BEFORE)


def export_local(ts):
    """`ts` as a timezone-AWARE datetime in the right zone for that instant."""
    from datetime import datetime
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=export_zone(ts))

# --- Collector tuning -------------------------------------------------------- #
# Exponential backoff bounds for reconnects (seconds).
RECONNECT_BACKOFF_START = 1.0
RECONNECT_BACKOFF_MAX = 30.0

# First-connect backfill cap. On a brand-new machine (empty DB, no cursor) the
# collector would otherwise stream the ENTIRE telemetry_history node as one
# initial event. That node grows without bound, so a fresh laptop can stall on a
# multi-MB first payload and never reach the live tail.
#
# A LIVE view needs none of that history — only the latest sample. So a fresh
# machine grabs just the most recent N samples (orderBy=$key + limitToLast): live
# is up in ~1s, and the trend charts have a little context instead of being blank.
# Reconnects afterwards resume incrementally from the stored cursor, so nothing is
# missed and history keeps growing from here on. Set to 1 for pure live-now (no
# history), or 0/None for the old unbounded full backfill.
INITIAL_BACKFILL_LIMIT = 200

# OAuth2 scopes required for RTDB REST access with a service account.
OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/firebase.database",
    "https://www.googleapis.com/auth/userinfo.email",
]
