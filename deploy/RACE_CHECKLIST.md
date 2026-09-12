# Race checklist — Zolder

Everything that has to happen outside the code, in the order it has to happen.
Each item says **why**, because a checklist nobody understands is a checklist
people skip.

Related: [`PI_UPDATE.md`](PI_UPDATE.md) for updating the car, [`README.md`](README.md)
for first-time Pi setup.

---

## 1. Before you travel

### ☐ Get a working key onto the Pi — **nothing uploads without this**

**Checked 6 Sep 2026: the key is valid and Google accepts it.** It was NOT
rotated and does NOT need to be. Run this on any machine to confirm for
yourself:

```bash
python tools/check_firebase_key.py
```

The car stopped uploading on **1 Sep 23:10**. The pit laptop's copy of the key
authenticates fine, so the fault is that **the Pi's copy differs from it** — the
Pi reported `invalid_grant: Invalid JWT Signature`, which means a well-formed
file that Google does not recognise.

Fix it by copying the working file from the pit laptop to the Pi:

```bash
# from the pit laptop
scp Pit_Dashboard/serviceAccountKey.json     orna2@raspberrypi:~/Desktop/THE-RACE-main/SolarRace_OS/cloud/
```

A USB stick works too — but copy the file, do not paste its contents into an
editor. A pasted key usually loses its newlines, and `check_firebase_key.py`
tests for exactly that (a real key has ~28; a mangled one has 0 or 1).

Then on the Pi:

```bash
python tools/check_firebase_key.py     # expect: token issued, HTTP 200
```

> **Separately, and still true:** this key was public in the repo for 12 days,
> so anyone who cloned it in that window can read and write your race telemetry.
> Rotating it is worth doing before the race — but it is *security work, not a
> fix for this outage*, and rotating means putting the new file on **both**
> machines by hand, because both paths are gitignored and do not travel with a
> `git pull`.

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

### ☑ Race window — set

**13:00 Sat 19 Sept → 13:00 Sun 20 Sept 2026, Belgian time (CEST).** Confirmed
against europeansolarchallenge.eu: 24 hours continuous, Le Mans-style start,
sunset 19:47 Saturday and sunrise 07:22 Sunday, with at least one and at most
three recharging stops overnight.

Already baked into the spectator page. If it ever moves, rebuild:

```bash
python tools/build_zolder_animation.py --race-start "2026-09-19T13:00+02:00"
```

or write `/public/race` with `start_ts` / `end_ts` in epoch seconds, which the
page picks up live without a rebuild.

### ☐ Update the car

```bash
cd ~/Desktop/THE-RACE-main && git pull
./deploy/stop_hud.sh && ./deploy/start_hud.sh
```

---

## 2. At the circuit, before the session

### ☐ Check the regen brake light

The brake light on GPIO 17 comes on whenever the motor is regenerating (below
−50 W), because regen slows the car and nothing else on it knows that — the
pedal switch does not move when the driver simply lifts. Scrutineering will
look at this.

Watch for `🛑 regen brake light: GPIO 17 …` in the HUD's boot log. If it says
**NOT DRIVEN**, the pin was not claimed and no lamp is being switched, however
good the wiring looks.

> ⚠️ **A GPIO pin cannot drive a lamp.** 3.3 V, ~16 mA. It switches a
> logic-level MOSFET or an opto-isolated SSR, with a 10k pull-down on the gate
> so the lamp stays off while the Pi boots. Circuit is in
> `SolarRace_OS/modules/regen_light.py`.

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

## 3b. After every `git pull` on the pit laptop — restart the apps

**Streamlit does not pick up changed code in imported modules.** It re-runs the
main script on every interaction, but `db.py`, `constants.py` and friends stay
in memory as they were when the process started — and `.streamlit/config.toml`
sets `fileWatcherType = "none"`, so nothing restarts on its own either.

A pulled change therefore lands half-applied: the page code is new, the modules
it calls are old. That shows up as a confusing error naming a column or function
that plainly does exist on disk.

Close the dashboard, collector and profile-builder windows and start them again.
Same rule as the car: `git pull` alone changes nothing that is already running.

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
- **Energy per lap starts as an estimate and becomes a measurement.** The
  Strategy tab's Wh/lap is the number somebody guessed before the car ever ran,
  and it decides laps-possible, the stint plan and how many charge stops get
  recommended. After **3 completed laps under a profile** it is replaced by the
  median the car actually measured for that profile, and the caption under the
  matrix says which rows are real. Practice laps at Zolder are what turn it
  real — and only laps recorded from this build onward can be attributed,
  because older rows never stored which profile was active.

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
