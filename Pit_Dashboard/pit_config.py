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
                                   Pit_Web/api.py      export.py     (anything else)
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

# ── Spectator viewer count ───────────────────────────────────────────────── #
# Every open spectator page (docs/index.html) refreshes one key under
# VIEWERS_PATH with the SERVER's timestamp. The collector is what counts them:
# if each page counted for itself it would have to download every other page's
# key, which is quadratic in viewers and would eat the free tier by mid-race.
# So the browsers write, this process counts, and each page reads one integer.
VIEWERS_PATH = "public/viewers"        # one key per open page, value = ms epoch
VIEWERS_COUNT_PATH = "public/viewers_count"   # the single number pages read

# A key older than this is a page that was closed, slept or lost its network.
# Comfortably longer than the page's beat (20s) so one missed beat on bad wifi
# does not make somebody blink out of the count.
VIEWER_STALE_MS = 45_000

# How often the collector recounts. Also the delay before a closed tab that
# missed its farewell drops out.
VIEWER_SWEEP_S = 10.0

# A recount that hangs must never wedge the sweeper.
VIEWER_HTTP_TIMEOUT = 10.0

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

# Force a reconnect when no actual TELEMETRY event has arrived for this long,
# even though the socket is still delivering bytes.
#
# This exists because of a real, observed silent stall: the collector sat with
# an ESTABLISHED connection to RTDB for over an hour, using almost no CPU,
# logging nothing, and storing nothing — while the car was pushing a sample
# every half second and Firebase itself had data 0.7 s old. The pit wall showed
# "Stale" the whole time and there was no error anywhere to explain it.
#
# The cause is that STREAM_READ_TIMEOUT (collector.py) measures BYTES, not
# samples. RTDB sends a `keep-alive` event every ~30-45 s, which resets that
# read timeout forever, so a stream that has stopped delivering `put` events
# looks perfectly healthy to the socket layer and is never torn down. Liveness
# has to be measured in the thing we actually care about — samples — which is
# what this does.
#
# Comfortably longer than the keep-alive interval, so a merely idle-but-healthy
# stream is not churned. A reconnect is cheap and lossless (startAt resumes
# from the stored cursor), so erring toward reconnecting is the right trade:
# the worst case while the car is genuinely parked is one extra HTTPS request
# every couple of minutes, versus silently missing an entire race stint.
DATA_SILENCE_TIMEOUT = 120.0

# First-connect backfill cap. On a brand-new machine (empty DB, no cursor) the
# collector would otherwise stream the ENTIRE telemetry_history node as one
# initial event. That node grows without bound, so a fresh laptop can stall on a
# multi-MB first payload and never reach the live tail.
#
# A LIVE view needs none of that history — only the latest sample, so a fresh
# machine asks for the most recent N samples (orderBy=$key + limitToLast) and is
# live in ~1s. Reconnects afterwards resume incrementally from the stored cursor,
# so nothing is missed and history grows from here on.
#
# Set to 1 — pure live-now. An archived database (tools/archive_db.py, run before
# a practice day or the race) must come back holding that session and nothing
# else: 200 samples of carry-over would put the END of the previous session at
# the START of the new one, where it reads as this session's opening lap and
# poisons the first trend charts and any speed profile measured off them.
#
# DO NOT set this to 0 to mean "no backfill". 0 and None are falsy and select the
# OLD UNBOUNDED FULL BACKFILL — the entire telemetry_history node in one event,
# which is the very thing this cap exists to prevent. 1 is the floor.
#
# One sample still arrives on a fresh DB, and cannot not: RTDB's startAt/
# limitToLast boundary is inclusive, so the newest existing key is always
# delivered. It is a single row, and it is the car's current state rather than
# stale history, which is what a live view wants anyway.
INITIAL_BACKFILL_LIMIT = 1

# How many samples to pull per request while CATCHING UP after a gap.
#
# Restarting the collector days behind asks RTDB for the entire tail in one
# streamed event. Measured on 2026-09-08 against a 12-day gap that was a single
# 166.6 MB SSE event: requests' iter_lines() buffers it as ONE line, json.loads
# expands it, and only then is anything stored or logged — so the process sat
# silent for minutes on a multi-GB working set, looking hung. Nothing bounded
# it, because INITIAL_BACKFILL_LIMIT only covers the EMPTY-database case, not a
# resume. StreamStalled does not catch it either: bytes are arriving the whole
# time, so the feed is not stalled, just enormous.
#
# Catch-up is now paged through plain REST GETs of this size before the live
# stream opens. 5,000 samples is roughly 1.7 MB per request — quick, loggable,
# and few enough round trips to close a long gap sensibly.
CATCHUP_PAGE_SIZE = 5000

# OAuth2 scopes required for RTDB REST access with a service account.
OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/firebase.database",
    "https://www.googleapis.com/auth/userinfo.email",
]
