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

**To test it without driving the car**, stop the HUD (`deploy/stop_hud.sh`, so
it lets go of the pin) and run the simulator from the Pi's own desktop:

```bash
./"Start HUD Demo.sh"        # then press R to hold the lamp on, R again to release
```

It drives the real `RegenLight` on GPIO 17 from a fake car, with the same
thresholds and the same minimum-on hold. The lamp flashes on its own braking
into every corner, `R` holds it lit while you walk to the back of the car, and
the HUD's status line reads `🛑 BRAKE LIGHT ON` whenever the pin is being
driven high. Hazard `Car silent — no data` (press `H` to reach it) shows the
lamp releasing itself after two quiet seconds rather than sticking on.

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

### ☐ Confirm the GPS is actually receiving — not just that the modem is online

The receiver is the GNSS engine inside the SIM7600 modem, and the modem being
connected says nothing about it: LTE can sit at 100% signal while GNSS is switched
off and gpsd holds no device at all. That combination is silent — the HUD simply
never shows a position.

```bash
systemctl status gps-up      # active (exited), "handed /dev/ttyUSBx to gpsd"
gpspipe -w -n 5              # TPV reports, not just VERSION
```

Or read the two `🛰️` lines `main.py` prints at startup. `searching for
satellites` with a device named on the hardware line means the chain is healthy
and only sky view is missing; `gpsd: NO device` or `no USB/serial port` is a
setup fault to fix before rolling out. **Take the car outside and wait for a
real fix before the race** — the GNSS antenna connector is separate from the
LTE ones, and a loose one looks exactly like being parked indoors.

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

**A running process does not pick up pulled code.** The dashboard's backend
(the "Pit Web" uvicorn window) loads `api.py`, `db.py`, `strategy_engine.py` and
friends once at startup and runs without auto-reload, and the collector and the
profile builder are the same. A pulled change to any Python file does nothing
until its process restarts.

The browser side is different: the frontend is the committed
`Pit_Web/frontend/dist`, read from disk on each request, so a page reload picks
up a pulled bundle without any restart. The page can therefore be new while the
backend it talks to is old, which shows up as a confusing error or a blank
panel for something that plainly exists on disk.

Close the "Pit Web", "Pit Collector" and profile-builder windows, start them
again (`Start Pit Dashboard.bat` brings up the first two), then reload the page
on every device. Same rule as the car: `git pull` alone changes nothing that is
already running.

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
- **Rule 3.5.6 report, every 2 hours**: highest/lowest cell temperature and
  highest/lowest cell voltage. Read it from the pit's **Cell Voltages** tab (top
  section) or from the HUD's last page, **R3.5.6**. Both show the cell and the
  time of each reading, and the window they cover. If the header is amber
  ("only N min of data"), the window is not a full 2 h, so say that in the
  report. The pit's window ends at the newest stored sample, not at the
  laptop's clock.

---

## 5. Speed profiles measured at the track

Build them with the profile builder (double-click `Build Speed Profiles.bat`, or
`streamlit run Pit_Dashboard/profile_builder.py --server.port 8502`). It reads
`telemetry.db` read-only and cannot disturb the pit wall.

### ☐ Pick the right DRIVE, not just the right lap number

Rows in the table are **drives**, not lap numbers. The car's lap counter restarts
whenever it is reset — a fresh image, a cleared checkpoint, a new session — so
the same number comes back later. Where that has happened the table puts the
date next to the number (`7 · 26 Aug 16:45`), and each drive carries the lap
time and energy from **its own** run.

This is not theoretical. In the team's own store lap 1 is three separate
evenings, and before this the builder welded them into one "lap" whose time and
energy were the maximum across all of them.

### ☐ Never build from a lap that says "This is not one drive"

If two copies of the car software publish at the same time — the Pi plus a
laptop with the service key — their samples interleave in the store under one
device id, and lap distance jumps backwards inside a single trace. The builder
detects that and refuses the lap outright. Nothing about such a trace is usable:
not the speed, not the energy, not the lap time.

**Prevention: only ONE machine runs the car software during a session.**

### ☐ Read the energy breakdown for what it is

Each lap now shows its energy, Wh/km, average power, regen, and a nine-sector
split of where the energy went, plus a cumulative-Wh chart around the lap.

* The **total** is the car's own figure, integrated on the car at CAN frame rate.
* The **split** is integrated here from the stored samples, which arrive about
  twice a second. It reads a few percent high — measured 1.04-1.21 against the
  car on real traces — so it is a picture of the SHAPE of the lap, not a second
  opinion about the total.
* The green line says the two agree and how much of the lap was covered. If it
  is an amber **"Breakdown not trusted"** instead, believe the total and ignore
  the split; the message says which test failed.
* S4 and S6 are barely 100 m long and get three or four samples each. Read them
  as indicative.

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

## 5b. The pit wall (big screen)

Double-click `Start Pit Wall.bat`, or `python tools/pit_wall.py`. It serves
http://localhost:8503 and prints the LAN addresses a TV can open.

It is a **second screen, not a second dashboard**: no controls, nothing to
click, and it reads `telemetry.db` read-only on its own thread in its own
process, so it cannot slow the dashboard down.

### ☐ Set the TV up before the car exists

`python tools/pit_wall.py --demo` drives the page from `profiles/base_210s.csv`
instead of the database - a car that is not there, lapping Zolder. Use it to sort
out the TV, the mount, the viewing angle and the LAN without waiting for a
session. It opens no database at all, so it works on any laptop.

The page says **DEMO — NOT LIVE DATA** in amber the whole time it is running.
If you ever see that badge during a race, somebody started the wrong one.

### ☐ The TV can reach it

The console window lists addresses like `http://192.168.1.24:8503/`. If the TV
cannot open one, the venue WiFi is isolating clients - put both on the pit's own
hotspot. This is the same failure that stops a second laptop reaching the
dashboard, and it has nothing to do with this program.

### ☐ The page says LIVE, not NO SIGNAL

Top right. `LIVE · 0.8s` means the car's last reading is 0.8 s old. Anything
over 20 s dims the whole screen and says `NO SIGNAL · 32s` - visible from
across the garage without reading a word.

### ☐ Nothing on it is a dash that should not be

A dash means "the car is not sending this", never zero. If a value you expect is
dashed, the car stopped reporting that metric - the wall will not draw the last
value it saw as though it were current. Startup also prints any field this
database cannot supply at all, which is a bug in the wall, not in the car.

### ☐ The lap delta names a strategy

Bottom of the LAP card: `Last 3:29.4 · -0.6 s vs base_210s`. If it says "no
strategy set", the car has not reported `active_strategy` and there is nothing
to compare a lap against - the wall shows no target rather than inventing one.

### ☐ Rebuild the page after changing a profile

`wall.html` bakes in each profile's lap time for the delta. After writing new
profiles at the track, run `python tools/build_zolder_animation.py` and reload
the TV. The server does not need restarting.

---

## 5c. The sidebar badge now tells you WHICH thing is broken

The car heartbeats to Firebase every 5 s whether or not CAN and GPS are working.
So the sidebar badge no longer means "the car's sensors are fine" — it means
"the Pi is reachable", and it says separately if anything on the car is not.

| Badge | What it means | What to do |
|---|---|---|
| `LIVE · 3s ago` | Everything is fine | Nothing |
| `Pi alive 3s ago · can0 silent 47s` | Pi and network fine, **CAN is not talking** | Check the CAN wiring / `can-up.service`, not the network |
| `Pi alive 3s ago · no GPS fix` | Pi and CAN fine, no sky | Normal in the garage; must clear on track |
| `Pi alive 3s ago · can1 silent 3600s` | One channel dead, the other fine | The second BMS is not answering |
| `Stale · 4m ago` | The Pi itself is not reaching us | Power, WiFi, or the Pi is down |
| `No data — is collector.py running?` | Nothing received at all | Start the collector |

### ☐ Before the race: see it work

With the car on and CAN connected, the badge must read plain `LIVE`. Then unplug
CAN for ten seconds — it must change to `Pi alive … · canN silent …s` and
change back when you reconnect. If it stays `LIVE` with CAN unplugged, the Pi is
running an older image: `git pull` and restart the HUD.

### ☐ The Pi needs the new code

None of this reaches the pit until the Pi has pulled and the HUD has restarted.
An older car simply sends no health block, and the badge then behaves exactly as
it always did — plain `LIVE` — which is deliberate: silence from an old build
is not evidence of a fault.

---

## 6. Quick reference

| Thing | Where |
|---|---|
| Pit dashboard | `Start Pit Dashboard.bat` → http://localhost:8000 (phones: http://<laptop-ip>:8000) |
| Profile builder | `Build Speed Profiles.bat` → http://localhost:8502 |
| Pit wall (big screen) | `python tools/pit_wall.py` → http://localhost:8503 |
| Spectator page (public) | https://orna2afeka.github.io/THE-RACE/ |
| Presentation map | `docs/zolder_animation.html` (self-contained, works offline) |
| Car logs | `~/hud-logs/hud.log` on the Pi |
| Collector output | the "Pit Collector" console window |
