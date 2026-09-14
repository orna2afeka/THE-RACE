"""
driver_message.py — send short instructions from the pit to the car HUD
=======================================================================
The pit wall is normally read-only (collector.py streams Firebase -> SQLite).
This is the ONE place the pit *writes*: a tiny `/driver_command` node the car
subscribes to with a realtime listener (see SolarRace_OS/cloud/firebase_client.py
`listen_driver_command`). "Latest wins" — each send overwrites the node; clearing
deletes it so the HUD banner disappears.

Reuses the same service-account credentials collector.py uses to read, minting a
short-lived OAuth token for a plain REST PUT/DELETE (no firebase-admin needed).
"""

import time

import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest

from pit_config import DB_URL, SERVICE_ACCOUNT_PATH, OAUTH_SCOPES

_URL = f"{DB_URL}/driver_command.json"

# SENDS get a generous timeout. A send is a deliberate click by an engineer who
# is watching for the result, and a send that gives up early is a command the
# pit believes it issued and did not.
_TIMEOUT = 5  # seconds

# ACK READS get a much tighter one, because they are a different kind of call.
# An ack is advisory — it upgrades "sent" to "applied" — so a slow one is worth
# nothing. And critically it happens on the SCRIPT-RUN THREAD, which Streamlit
# also uses to redraw every live tile on the wall: fragments are not concurrent,
# they queue. A five-second ack read therefore does not stall the Strategy tab,
# it stalls the speed, the SoC and the fault banner along with it.
_ACK_TIMEOUT = 2  # seconds

# Cache the credentials object; it refreshes its own token in place.
_creds = None


class _TimedAuthRequest(GoogleAuthRequest):
    """google-auth's transport, with a timeout we actually choose.

    Credentials.refresh() calls its transport with no timeout argument, and
    google.auth.transport.requests.Request defaults to 120 SECONDS. That call
    runs on the same thread that draws the pit wall, so a hung TLS session to
    oauth2.googleapis.com froze every tile on the screen for up to two minutes —
    once an hour as the token expired, and on the send path too.

    Ten seconds is already very generous for an OAuth POST. If it does expire,
    the send raises, the caller's existing except shows "send failed", and the
    engineer can press the button again — a visible, retryable failure, which is
    the correct one to have.
    """

    def __call__(self, url, method="GET", body=None, headers=None,
                 timeout=10, **kwargs):
        return super().__call__(url, method, body, headers, timeout, **kwargs)


def _token() -> str:
    """A valid OAuth2 access token for RTDB REST access (refreshed as needed)."""
    global _creds
    if _creds is None:
        _creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_PATH, scopes=OAUTH_SCOPES
        )
    if not _creds.valid:
        _creds.refresh(_TimedAuthRequest())
    return _creds.token


def send_driver_command(category: str, value) -> dict:
    """Write {category, value, ts} to /driver_command (overwrite). Returns the
    payload sent. Raises on HTTP/credential errors so the UI can show a toast."""
    payload = {"category": str(category or ""), "value": str(value), "ts": time.time()}
    resp = requests.put(_URL, params={"access_token": _token()},
                        json=payload, timeout=_TIMEOUT)
    resp.raise_for_status()
    return payload


# ---------------------------------------------------------------------------
# Lap commands — a SEPARATE node from /driver_command.
#
# /driver_command is "latest wins" for driver text and is DELETED to clear the
# HUD banner. Sharing it would mean cutting a lap wipes whatever the driver was
# being told, and clearing a driver message would reach the car's lap handler as
# a null event. Different lifecycle, different node.
#
# NOTE this is entirely independent of the pit's "Manual Lap (-1 for Auto)"
# override, which stays a purely local display correction and never touches the
# car. This button asks the CAR to close its current lap: it snapshots lap
# energy and lap time on the Pi, exactly as a GPS finish-line crossing does.
# ---------------------------------------------------------------------------
_LAP_URL = f"{DB_URL}/lap_command.json"
_LAP_ACK_URL = f"{DB_URL}/lap_command_ack.json"


def send_lap_command(action: str, value=None) -> dict:
    """Write a lap command for the car. Returns the payload sent.

    `id` is milliseconds since the epoch: monotonic in practice, needs no
    read-modify-write round trip against RTDB, and readable in the console. The
    car ignores any id it has already seen, which is what stops Firebase's
    retained-node replay from cutting a phantom lap on every reconnect.
    """
    payload = {"id": int(time.time() * 1000), "action": str(action),
               "value": value, "ts": time.time(), "by": "pit"}
    resp = requests.put(_LAP_URL, params={"access_token": _token()},
                        json=payload, timeout=_TIMEOUT)
    resp.raise_for_status()
    return payload


def send_lap_cut() -> dict:
    """Ask the car to close the current lap now."""
    return send_lap_command("cut_lap")


def send_trip_reset() -> dict:
    """Ask the car to zero its OWN tracked distance total (state["trip_m"] /
    lap_tracker.odometer_m) -- not lap count, not energy, and not the
    controller's own hardware TRIP register (0x620), for which there is no
    documented CAN reset command. See lap_tracker.LapTracker.reset_trip().

    Shares /lap_command and /lap_command_ack with send_lap_cut() -- same node,
    same idempotency gates, same ack shape (with action="reset_trip" so the
    pit can tell this ack apart from a Cut Lap ack landing in between)."""
    return send_lap_command("reset_trip")


def read_lap_ack():
    """The car's acknowledgement of the last lap command, or None.

    Lets the pit show "applied — now on lap 37" instead of hoping the command
    arrived. Returns None on any error: an ack we cannot read is not worth
    breaking the sidebar over.
    """
    try:
        resp = requests.get(_LAP_ACK_URL, params={"access_token": _token()},
                            timeout=_ACK_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Strategy selection — again its own node, for the same lifecycle reason.
# ---------------------------------------------------------------------------
_STRATEGY_URL = f"{DB_URL}/strategy_command.json"
_STRATEGY_ACK_URL = f"{DB_URL}/strategy_ack.json"


def send_strategy(strategy_key: str) -> dict:
    """Tell the car which speed profile to follow.

    Sends only the NAME. The car holds all five generated profiles, so this is
    a few bytes rather than a 400-row table — which matters on a race-day link,
    and means the profile actually flown is the one in git rather than whatever
    happened to be pushed.
    """
    payload = {"id": int(time.time() * 1000), "action": "set_strategy",
               "value": str(strategy_key), "ts": time.time(), "by": "pit"}
    resp = requests.put(_STRATEGY_URL, params={"access_token": _token()},
                        json=payload, timeout=_TIMEOUT)
    resp.raise_for_status()
    return payload


def read_strategy_ack():
    """What the car says it is actually running, or None.

    The pit is choosing how hard the car is driven for the next hour; "the
    message left the pit" is not the same as "the car changed profile".
    """
    try:
        resp = requests.get(_STRATEGY_ACK_URL, params={"access_token": _token()},
                            timeout=_ACK_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def clear_driver_command() -> None:
    """Delete /driver_command so the car HUD hides its banner."""
    resp = requests.delete(_URL, params={"access_token": _token()}, timeout=_TIMEOUT)
    resp.raise_for_status()
