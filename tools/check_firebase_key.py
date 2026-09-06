"""
check_firebase_key.py — is this machine's Firebase key actually usable?
=======================================================================
Run it on the Pi, or on the pit laptop, whenever telemetry is not moving:

    python tools/check_firebase_key.py

WHY THIS EXISTS
The three ways this fails all look different and none of them names the real
problem, so the same afternoon gets spent twice:

  "The default Firebase app does not exist"
        The file is MISSING (or unreadable). initialize_firebase() catches that,
        prints one CRITICAL line and lets the app carry on -- so the message you
        actually see comes later, from db.reference(), and sounds like a code
        bug. It is not: nothing was ever initialised.

  "invalid_grant: Invalid JWT Signature"
        The file is present and valid JSON, and Google is rejecting it. Either
        the key was deleted in the Cloud console, or this machine's copy is
        corrupt / from a different project. Those need opposite fixes, and the
        error does not distinguish them -- this script does, by reporting the
        key id so you can compare it against the console and against the other
        machine.

  Everything mints fine but nothing arrives
        Auth is not the problem; look at the database rules or the network.

WHAT IT DOES
Reads the key, mints a real access token, and does one READ of the database.
Nothing is written. It only ever prints identifying fields (project, client
email, key id) -- never the private key.
"""

import argparse
import datetime
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)

# Where the two copies live. Both are gitignored, so they do NOT arrive with a
# git pull -- every machine needs its own copy put there by hand.
CANDIDATES = [
    os.path.join(_REPO, "SolarRace_OS", "cloud", "serviceAccountKey.json"),
    os.path.join(_REPO, "Pit_Dashboard", "serviceAccountKey.json"),
]

DB_URL = ("https://solar-race-telemetry-default-rtdb."
          "europe-west1.firebasedatabase.app")

SCOPES = ["https://www.googleapis.com/auth/firebase.database",
          "https://www.googleapis.com/auth/userinfo.email"]

OK, BAD, WARN = "  [ok]  ", "  [FAIL]", "  [warn]"


def inspect(path):
    """Report the file itself. Returns the parsed dict, or None."""
    print(f"\n{os.path.relpath(path, _REPO)}")
    if not os.path.exists(path):
        print(f"{BAD} not present on this machine")
        print("        -> this is the 'default Firebase app does not exist' case.")
        print("        -> copy the key here from a machine that has a working one.")
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception as exc:
        print(f"{BAD} present but not readable as JSON: {exc}")
        return None

    missing = [k for k in ("type", "project_id", "private_key", "client_email")
               if not d.get(k)]
    if missing:
        print(f"{BAD} missing required field(s): {missing}")
        return None

    pk = d.get("private_key", "")
    print(f"{OK} project     {d.get('project_id')}")
    print(f"{OK} client      {d.get('client_email')}")
    print(f"{OK} key id      {d.get('private_key_id')}")
    print(f"        (compare that key id against Google Cloud console ->")
    print(f"         IAM -> Service Accounts -> Keys. Not listed = deleted.)")

    if not pk.startswith("-----BEGIN PRIVATE KEY-----"):
        print(f"{BAD} private_key does not start with a PEM header - file is corrupt")
        return None
    if pk.count("\n") < 20:
        print(f"{BAD} private_key has only {pk.count(chr(10))} newlines; a real "
              f"one has ~28. It was mangled in transit (copy/paste or a text "
              f"editor). Copy the file as BINARY, or scp it.")
        return None
    print(f"{OK} private_key looks structurally intact "
          f"({len(pk)} chars, {pk.count(chr(10))} newlines)")
    return d


def try_auth(path):
    """Mint a token and read the database. Returns True when telemetry can flow."""
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        import requests
    except ImportError as exc:
        print(f"{WARN} cannot test auth here - missing dependency ({exc}).")
        print("        On the Pi: pip install -r SolarRace_OS/requirements.txt")
        return False

    try:
        creds = service_account.Credentials.from_service_account_file(
            path, scopes=SCOPES)
        creds.refresh(Request())
    except Exception as exc:
        text = str(exc)
        print(f"{BAD} Google REJECTED this key: {text[:160]}")
        if "Invalid JWT Signature" in text or "invalid_grant" in text:
            print("        Meaning: the file is well-formed but does not match a")
            print("        key Google holds for this service account. Either")
            print("        (a) it was deleted in the console -> make a new key, or")
            print("        (b) this machine's copy is stale/corrupt -> copy the")
            print("            working file from the other machine.")
            print("        The key id printed above tells you which: if the OTHER")
            print("        machine's key id differs and works, it is (b).")
        return False
    print(f"{OK} Google accepted the key and issued an access token")

    import requests
    try:
        r = requests.get(f"{DB_URL}/live_telemetry.json",
                         params={"access_token": creds.token}, timeout=15)
    except Exception as exc:
        print(f"{BAD} could not reach the database: {exc}")
        print("        Auth is fine; this is a network problem.")
        return False

    if not r.ok:
        print(f"{BAD} database read failed: HTTP {r.status_code} {r.text[:120]}")
        print("        Auth is fine; check the database RULES.")
        return False

    payload = r.json() or {}
    ts = payload.get("timestamp")
    print(f"{OK} database read OK (HTTP 200)")
    if ts:
        age = time.time() - float(ts)
        when = datetime.datetime.fromtimestamp(float(ts)).strftime("%d %b %Y %H:%M:%S")
        if age < 60:
            print(f"{OK} the car is uploading RIGHT NOW (last write {age:.0f}s ago)")
        else:
            print(f"{WARN} last write was {when} "
                  f"({age / 3600:.1f} h ago) - the car is not uploading now")
    else:
        print(f"{WARN} /live_telemetry is empty - the car has never written")

    pub = requests.get(f"{DB_URL}/public/live.json",
                       params={"access_token": creds.token}, timeout=15)
    if pub.ok and pub.json():
        print(f"{OK} /public/live exists - the spectator page has data to show")
    else:
        print(f"{WARN} /public/live is empty - either the car has not run the "
              f"build that writes it, or it is not uploading")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("path", nargs="?", help="a specific key file to check")
    args = ap.parse_args()

    print("Firebase key check")
    print("=" * 60)
    paths = [args.path] if args.path else CANDIDATES

    any_ok = False
    for p in paths:
        d = inspect(p)
        if d:
            any_ok = try_auth(p) or any_ok

    print("\n" + "=" * 60)
    if any_ok:
        print("RESULT: at least one key on this machine can talk to Firebase.")
        print("If telemetry still is not moving, the key is not your problem.")
    else:
        print("RESULT: no usable key on this machine. Nothing will upload.")
        print("Fix the [FAIL] line above, then run this again.")
    return 0 if any_ok else 1


if __name__ == "__main__":
    sys.exit(main())
