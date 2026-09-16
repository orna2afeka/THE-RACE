# edge_sync (vendored, modified)

A copy of the `edge_sync` package from
https://github.com/leerosenblit/Telemetry-Edge-Sync-SDK (commit `8c9bac9`, version
0.1.1), **with changes that are not in that repo yet**: `__init__.py`, `client.py`,
`queue.py` and `sync_policy.py` only. The SDK repo is being left alone until after the
race; the changes go upstream then.

What changed from 0.1.1, and why the car needs it:

- One row per snapshot, rows deleted once uploaded, every query indexed. 0.1.1 took
  6.2 s to store one second of car data once two minutes of backlog had built up.
- Ids in Firebase push-key format that only ever increase, across reboots and clock
  jumps, plus `reconcile()` so the pit collector (which resumes from its newest key)
  never skips a backlog and never gets a batch twice.
- `track()` never raises; a record Firebase refuses is kept aside on disk instead of
  blocking the queue; `stats()`; `close()` is time-bounded; `httpx` is only imported by
  the REST sender, which the car does not use.

It is copied rather than pip-installed because the Pi installs its packages from
`SolarRace_OS/requirements.txt`, and the car often boots without network. It needs
nothing beyond the Python standard library.

Used by `cloud/firebase_client.py`: every telemetry sample is saved to
`SolarRace_OS/telemetry_outbox.db` and uploaded to Firebase from a background thread.
See "THE OUTBOX" in that file. Offline test: `python tools/check_outbox.py`.

**Do not edit these files here.** Change the SDK, run its tests, then copy the four
files over again.
