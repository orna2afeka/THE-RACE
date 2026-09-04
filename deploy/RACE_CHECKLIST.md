# Race checklist — Zolder

Everything that has to happen outside the code, in the order it has to happen.
Each item says **why**, because a checklist nobody understands is a checklist
people skip.

Related: [`PI_UPDATE.md`](PI_UPDATE.md) for updating the car, [`README.md`](README.md)
for first-time Pi setup.

---

## 1. Before you travel

### ☐ Rotate the Firebase service-account key — **nothing works without this**

A key was public in this repo for 12 days and must be treated as compromised.
The symptom of a stale key is not obvious: the car prints

```
[Network Error] Failed to update Firebase: invalid_grant: Invalid JWT Signature
```

which means the file parses fine but Google has revoked it.

1. Google Cloud console → IAM → Service Accounts → Keys → create a new key,
   delete the old one.
2. Put it at **both** paths (they are gitignored, so they do not travel with a
   `git pull` — you must copy them by hand):
   - `SolarRace_OS/cloud/serviceAccountKey.json` (on the Pi)
   - `Pit_Dashboard/serviceAccountKey.json` (on the pit laptop)
3. Restart `main.py` on the Pi and look for `Firebase connection established
   successfully.`

> A missing file fails differently and more confusingly: `initialize_firebase`
> catches the error, prints `CRITICAL: Failed to initialize Firebase`, and lets
> the app carry on — so every later push fails with *"The default Firebase app
> does not exist"*, which sounds like a code bug and is not.

### ☐ Publish the database rules (only needed for the spectator page)

Firebase console → Realtime Database → Rules → paste → Publish:

```json
{
  "rules": {
    ".read": false,
    ".write": false,
    "public": { ".read": true, ".write": false }
  }
}
```

This exposes **only** `/public/live` — eight fields, deliberately **no GPS
coordinates**; the spectator page places the car from lap distance along the
baked centreline instead. Everything else stays private. The car and
`collector.py` authenticate with the service account, which bypasses rules, so
`".write": false` does not stop them.

### ☐ Enable GitHub Pages

Repo → Settings → Pages → Deploy from a branch → `main`, folder `/docs`.
The family link is then **`https://orna2afeka.github.io/THE-RACE/`**.

### ☐ Set the race window

Otherwise the spectator page hides its race clock rather than counting down to a
guessed date. Either rebuild:

```bash
python tools/build_zolder_animation.py --race-start "2026-09-19T12:00+02:00"
```

(offset required — family watching from Israel should read it in their own
timezone) or write `/public/race` with `start_ts` / `end_ts` in epoch seconds,
which the page picks up live with no rebuild.

### ☐ Update the car

```bash
cd ~/Desktop/THE-RACE-main && git pull
./deploy/stop_hud.sh && ./deploy/start_hud.sh
```

---

## 2. At the circuit, before the session

### ☐ Put the pit on its own network

Campus and venue WiFi use client isolation, so two devices on the same SSID
cannot see each other and the dashboard will not open from a second laptop or a
phone. Use a **phone hotspot or your own router**.

### ☐ Confirm the car is actually reaching the pit

Three independent things to look at, in this order:

| Check | Where | Means |
|---|---|---|
| `NET` badge green | driver HUD | the radio link is up |
| `PIT` badge green | driver HUD | writes are **landing** — this is the one that matters |
| Sidebar says `LIVE`, age a few seconds | pit dashboard | the collector is storing them |

`NET` green with `PIT` red is the specific failure the badges exist to catch: a
perfectly good internet connection and a dead uplink.

### ☐ Check GPS lap detection — **unproven, watch it**

No lap in the store has ever been GPS-triggered: every lap boundary so far came
from the 4400 m odometer force-cut, because the finish-line geofence is at
Zolder and all testing was done in Israel. On the first laps, check `lap_source`
(shown per lap in the profile builder). If it reads `odometer` rather than `gps`
at Zolder, finish-line detection is not working — lap times and any measured
speed profile inherit that error.

---

## 3. Immediately before pressing START RACE

### ☐ Archive the database, so the race starts on an empty one

Every query gets faster and the WAL restarts at zero. **Do this before the race
clock starts** — the clock is stored in `app_state` inside that same file, so
swapping it mid-race loses it.

```bash
# with collector.py and the dashboard both STOPPED
cd Pit_Dashboard
mv telemetry.db "telemetry.db.$(date +%Y%m%d_%H%M%S).prerace.bak"
```

Rename, don't use the dashboard's "Reset History" button: that is a `DELETE`,
which frees pages inside the file but does not shrink it. The collector handles
an empty file correctly — it goes straight to the live tail instead of
re-downloading everything.

> A 24 h race generates ~144,000 rows, more than is in the store now, so this
> buys the first two-thirds of the race, not all of it.

### ☐ If you keep the existing database instead, shrink the WAL first

```bash
# both processes stopped, after taking a .bak copy
python -c "import sqlite3; c=sqlite3.connect('Pit_Dashboard/telemetry.db'); \
c.execute('PRAGMA journal_size_limit=33554432'); \
print(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()); c.close()"
```

`(0, N, N)` means it worked. A leading `1` means something still has the file
open. The collector now checkpoints once a minute on its own, so this is a
one-off for the WAL that already grew to 151 MB.

**Do not `VACUUM` before the race** — it needs roughly double the free space and
holds an exclusive lock for the duration.

---

## 4. During the race

- **A dash means the car did not report it. It never means zero.** This holds
  everywhere: tiles, charts, exports and the spectator page.
- The History tab's window setting is per-browser-session. Wide windows are much
  more expensive than narrow ones; the tab now only does that work while it is
  actually open.
- After any pit command, the dashboard polls the car for an acknowledgement for
  30 s. "Sent" and "the car is running it" are different things — wait for the
  ack.

---

## 5. Speed profiles measured at the track

Build them with the profile builder (double-click `Build Speed Profiles.bat`, or
`streamlit run Pit_Dashboard/profile_builder.py --server.port 8502`). It reads
`telemetry.db` read-only and cannot disturb the pit wall.

### ☐ The car will not pick up a new profile until the HUD restarts

Profiles are loaded **once, at startup**. There is no reload command and no file
watcher.

```bash
# pit laptop
git add profiles/ && git commit -m "profiles: measured at Zolder" && git push

# car's Pi
cd ~/Desktop/THE-RACE-main && git pull
./deploy/stop_hud.sh && ./deploy/start_hud.sh
```

Confirm by sending the strategy from the pit and watching for the car's ack.

### Undo, if a measured profile turns out wrong

```bash
git checkout -- profiles/     # on BOTH machines, then restart the HUD
```

This works because the five original keys are never renamed. If git is not an
option, `python tools/generate_profiles.py` rebuilds the synthetic five from
`Pit_Dashboard/210s.xlsx`.

---

## 6. Quick reference

| Thing | Where |
|---|---|
| Pit dashboard | `Start Pit Dashboard.bat` → http://localhost:8501 |
| Profile builder | `Build Speed Profiles.bat` → http://localhost:8502 |
| Spectator page (public) | https://orna2afeka.github.io/THE-RACE/ |
| Presentation map | `docs/zolder_animation.html` (self-contained, works offline) |
| Car logs | `~/hud-logs/hud.log` on the Pi |
| Collector output | the "Pit Collector" console window |
