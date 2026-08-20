# 🏎️ Afeka Racing — 24H Endurance Telemetry & Strategy System

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![Raspberry Pi](https://img.shields.io/badge/Raspberry%20Pi-Edge-C51A4A.svg)](https://www.raspberrypi.org/)
[![CAN Bus](https://img.shields.io/badge/CAN%20Bus-SocketCAN-2C3E50.svg)](https://www.kernel.org/doc/html/latest/networking/can.html)
[![Firebase](https://img.shields.io/badge/Firebase-Realtime%20DB-FFCA28.svg)](https://firebase.google.com/)
[![Streamlit](https://img.shields.io/badge/Pit%20Wall-Streamlit-FF4B4B.svg)](https://streamlit.io/)

Telemetry and race-strategy software for the Afeka Solar & Electric Racing Team,
built for the **iESC 24-Hour Endurance Race at Circuit Zolder, Belgium**.

The system links the **car** (a Raspberry Pi reading the vehicle CAN bus) to the
**pit wall** (a laptop dashboard) through the Google Firebase Realtime Database,
giving engineers live battery, motor, temperature, and strategy data.

---

## 🏁 System Overview

Two subsystems, synchronised through one Firebase node (`live_telemetry`):

```
   ┌─────────────────────── CAR (Raspberry Pi) ───────────────────────┐
   │                                                                   │
   │   CAN bus (can0)                                                  │
   │   ├─ MMS  (SiliXcon LYNX motor controller)   IDs 0x600–0x628      │
   │   ├─ BMS  (JBD battery, polled)              IDs 0x100–0x110      │
   │   └─ TEMP (J1939 thermistor module)          ID  0x1839F380       │
   │            │                                                      │
   │            ▼                                                      │
   │   SolarRace_OS  ──►  parsers  ──►  vehicle_state  ──►  PySide6    │
   │   (main.py)                                │           Driver HUD │
   │                                            ▼                      │
   └──────────────────────────────────  Firebase  ────────────────────┘
                                            │
                                            ▼
   ┌──────────────────────────── PIT WALL (laptop) ───────────────────┐
   │   collector.py  ──(RTDB REST stream)──►  telemetry.db (SQLite)    │
   │        the ONLY process that reads Firebase        │              │
   │                                                    ▼              │
   │   Pit_Dashboard (Streamlit, reads SQLite — never Firebase)        │
   │   • Live speed / SoC / battery temp / motor temp / power          │
   │   • Lap / sector tracking + velocity-profile pace guidance        │
   │   • 24h energy-strategy matrix & SoC forecast                     │
   │   • Open-Meteo solar/weather forecast for Zolder                  │
   │   • Filtered CSV export (date/time + BMS/MMS/Temp subsystems)     │
   └───────────────────────────────────────────────────────────────────┘
```

**SolarRace_OS (car / Raspberry Pi)**
- Reads one shared CAN bus and decodes three protocols off it (motor, battery, temperature).
- Polls the JBD BMS (it is master/slave — it only answers when queried).
- Drives a distraction-free **PySide6 driver HUD**.
- Pushes a live telemetry snapshot to Firebase ~once per second.
- Falls back to **replaying a recorded log** when no CAN hardware is present, so the dashboards stay alive for development.

**Pit_Dashboard (pit wall / laptop)**
- `collector.py` is the **single** Firebase client: it streams the append-only
  `telemetry_history` node (RTDB REST / Server-Sent Events) and stores every
  sample into a local **SQLite** file (`telemetry.db`), the pit's source of truth.
  It is idempotent (the RTDB push key is the primary key) and self-heals after a
  pit dropout by resuming the stream from the last stored key.
- The Streamlit dashboard reads **only** from SQLite — it never opens its own
  Firebase connection. History/charts/exports therefore survive page refreshes.
- Computes pace delta vs. the Zolder velocity profile, lap/sector position,
  the 24h energy-strategy matrix and SoC forecast, and the Open-Meteo forecast.
- `export.py` exports history to CSV, filtered by date/time and subsystem
  (BMS / MMS / Temperature / Motion-GPS), from the dashboard or the command line.

---

## 🔌 CAN Bus Topology

All devices share **one high-speed CAN bus** on the Pi's CAN HAT (`can0`).
This works because the three message-ID ranges never overlap:

| Device | Protocol | Message IDs | Direction |
|--------|----------|-------------|-----------|
| **MMS** (motor) | SiliXcon LYNX | `0x600`–`0x628` (11-bit) | broadcast → Pi |
| **BMS** (battery) | JBD query/response | `0x100`–`0x110` (11-bit) | Pi polls → BMS replies |
| **TEMP** (battery temp) | J1939 thermistor | `0x1839F380` (29-bit) | broadcast → Pi |

> ⚠️ **Per-wire bitrate:** every device sharing **one wire** must run at that
> wire's bitrate — set per channel by `CAN_BITRATES` in `SolarRace_OS/config.py`
> (**can0 at 1 Mbit/s, can1 at 500 kbit/s**). The two channels are independent
> controllers and need not agree with each other, which is what lets a device
> that cannot be reconfigured (the J1939 temp module is often fixed at
> 250 kbit/s) sit on its own channel instead of dragging the whole car down to
> its rate. The BMS baud is user-definable — set it to match whichever channel
> it is wired to. Mixed rates require a **2-channel HAT** (two independent
> MCP2515s); a single-channel HAT gives you `can0` only.

---

## 📂 Repository Structure

```text
THE RACE/
│
├── SolarRace_OS/                     # Edge code — runs on the Raspberry Pi
│   ├── main.py                       # Entry point: opens CAN, polls BMS, runs the HUD, pushes to Firebase
│   ├── config.py                     # ⭐ Central config: bus bitrate, connection candidates, BMS poll list
│   ├── can_worker.py                 # CAN QThread + LYNX decoder + driver-HUD signals
│   ├── driver_dash_v2.py             # PySide6 driver HUD (the active dashboard, RacingDashboard)
│   ├── dashboard.py                  # Alternate standalone PySide6 engineering dashboard (pyqtgraph)
│   ├── test_connection.py            # Quick CAN probe — which bus is live + sample frames
│   ├── requirements.txt              # Pi dependencies
│   ├── modules/
│   │   ├── bms_parser.py             # JBD battery decoder (voltage, current, SoC, cells, temps, protections)
│   │   ├── mms_parser.py             # SiliXcon LYNX motor decoder (RPM, power, errors)
│   │   ├── temp_controller_parser.py # J1939 battery-temperature decoder (low/high/avg)
│   │   └── gps_reader.py             # GPS position via gpsd (background thread, live in main.py)
│   ├── cloud/
│   │   ├── firebase_client.py        # Pushes telemetry to the Realtime DB (throttled)
│   │   └── serviceAccountKey.json    # 🔒 Firebase admin key (keep out of git)
│   └── data/
│       └── can_dump.txt              # Recorded CAN log (MMS+BMS+temp) used for simulation fallback
│
└── Pit_Dashboard/                    # Pit-wall analytics — runs on an engineer's laptop
    ├── collector.py                  # ⭐ ONLY Firebase client: streams telemetry_history -> SQLite
    ├── db.py                         # SQLite schema + idempotent upsert + query helpers
    ├── export.py                     # CSV export (date/time + subsystem filters); also a CLI
    ├── pit_config.py                 # Pit-side config: DB URL, paths, sqlite path, device id
    ├── pit_dashboard.py              # Streamlit dashboard (reads SQLite only, never Firebase)
    ├── weather_service.py            # Open-Meteo Zolder forecast
    ├── strategy_engine.py            # Strategy math, SoC forecast graph, velocity profile
    ├── outputProfile.xlsx            # Zolder velocity profile
    ├── telemetry.db                  # 🔒 local SQLite store (gitignored; created by collector.py)
    ├── requirements_pit.txt          # Pit dependencies
    └── serviceAccountKey.json        # 🔒 Firebase admin key (keep out of git)
```

---

## ⚙️ Configuration

Almost everything car-side is centralised in **`SolarRace_OS/config.py`**:

| Setting | Purpose |
|---------|---------|
| `CAN_BITRATES` | Per-channel bitrate map — **every device on a channel must match its rate** (`can0: 1_000_000`, `can1: 500_000`). For SocketCAN this does not set the rate; `ip link` does (see `deploy/can-up.service`), so keep the two in step. |
| `CAN_BITRATE` | Fallback rate for channels absent from `CAN_BITRATES` — in practice the USB-to-CAN adapters, where python-can really does apply it. |
| `CAN_CANDIDATES` | Connections tried in order: CAN HAT (`socketcan:can0`) first, then a USB-to-CAN adapter. First that opens wins. |
| `BMS_POLL_IDS` / `BMS_POLL_BYTE` / `BMS_POLL_INTERVAL_S` | Which BMS frames to request, the query byte (`0x5A`), and how often (1 Hz). |
| `SIM_LOG_PATH` | Recorded log replayed when no CAN bus is found. |

To use a USB adapter instead of the HAT, or change channels, just edit
`CAN_CANDIDATES` — no other code changes needed.

---

## 🧰 Hardware

- **Raspberry Pi** (3B+ / 4 / 5) running Raspberry Pi OS.
- **CAN HAT** (MCP2515-based, e.g. Waveshare) — ensure 120 Ω termination is correct.
  *(A USB-to-CAN adapter such as PEAK PCAN-USB also works via `CAN_CANDIDATES`.)*
- **Touchscreen** (7"/10") for the driver HUD.
- **Internet** for the Pi (hotspot / cellular dongle) to reach Firebase.
- **GPS module** (NMEA, USB or GPIO) — read through **gpsd**, not directly. Install
  `gpsd gpsd-clients`, point `/etc/default/gpsd` at the device (`DEVICES="/dev/ttyUSB1"`,
  `GPSD_OPTIONS="-n"`), enable the service, and confirm with `gpspipe -w`. gpsd owns the
  serial port; `gps_reader.py` is one of its clients, so nothing else should open that tty.

---

## 🚀 Installation & Running

### A. Car — SolarRace_OS (Raspberry Pi)

**1. Enable the CAN HAT** in `/boot/firmware/config.txt` (or `/boot/config.txt` on older OS), e.g.:
```
dtparam=spi=on
dtoverlay=mcp2515-can1,oscillator=16000000,interrupt=25
dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=23
dtoverlay=spi-bcm2835-overlay
```

Straight from the Waveshare wiki, including the `spi-bcm2835-overlay` line that is
easy to miss. The interrupt pins come from the board's own table:

| Signal | BCM pin | Purpose |
|--------|---------|---------|
| CS_0   | 8 (CE0)  | CAN_0 chip select |
| INT_0  | **23** (default) / 22 | CAN_0 interrupt |
| CS_1   | 7 (CE1)  | CAN_1 chip select |
| INT_1  | **25** (default) / 24 | CAN_1 interrupt |

The alternates (22 / 24) only apply if the solder pads on the PCB were moved.

> ⚠️ **These are the Waveshare 2-CH CAN HAT pins: can0 = GPIO23, can1 = GPIO25.**
> This file previously listed `can0 ... interrupt=25` and no can1 line at all — those
> are the settings for the **single-channel** RS485 CAN HAT, which is a different
> board. On the 2-CH HAT that binds can0 to can1's interrupt line, so can0 either
> never appears or behaves erratically. Check your own `config.txt` against this.

The 2-CH HAT carries **two independent MCP2515 controllers** (plus SN65HVD230
transceivers), which is what makes per-channel bitrates possible — can0 at
1 Mbit/s and can1 at 500 kbit/s are genuinely separate hardware, not one
controller time-shared.

Each channel also has a **switchable 120 Ω termination jumper**. A CAN bus needs
exactly two terminators, one at each physical end. Too few (or too many) causes
reflections, error frames, and eventually `BUS-OFF`. With the bus unpowered,
measure across CANH/CANL: **~60 Ω is correct** (two 120 Ω in parallel). 120 Ω
means a terminator is missing; 40 Ω means there is one too many.

### Two hardware gotchas from the vendor

**VIO jumper must be set to 3.3 V.** The HAT ships with a selectable 3.3 V/5 V
level translator and the Pi is a 3.3 V device. Waveshare: *"The working voltage
level of Raspberry Pi is 3.3V, therefore we need to set the VIO of 2-CH CAN HAT
to 3.3V."* Wrong position gives marginal, flaky signalling rather than a clean
failure.

**Use the standoffs.** On a 2B/3B/4B the back of the CAN screw terminal can
touch the HDMI connector and short out. Waveshare calls this out explicitly and
ships a booster seat and nylon post for it. A short here kills the channel in a
way no amount of software debugging will explain.

Reboot after editing `config.txt`.

### 1 Mbit/s may not be achievable

Waveshare's own FAQ: *"During high-speed communication, the data baud rate may
not reach the nominal maximum rate ... Users need to ensure stability and select
a suitable communication speed according to actual measurements."*

This matters here. `can0` runs at 1 Mbit/s and has been observed in `BUS-OFF`,
while `can1` at 500 kbit/s stays healthy. Isolated transceivers add propagation
delay, and 1 Mbit/s leaves little timing margin over any real cable length. If
`can0` keeps dropping to `BUS-OFF` after the wiring and termination check out,
**the bitrate itself is the suspect** - but the MMS has to be reconfigured to
match, since both ends of a wire must agree.

**2. Bring each bus up** at *its own* configured bitrate (see
`config.CAN_BITRATES`; every device on a given wire must agree with that wire):
```bash
sudo ip link set can0 up type can bitrate 1000000   # can0 — 1 Mbit/s
sudo ip link set can0 txqueuelen 65536
sudo ip link set can1 up type can bitrate 500000    # can1 — 500 kbit/s
sudo ip link set can1 txqueuelen 65536
```
To make it automatic at boot, install the ready-made unit — do not hand-write one:
```bash
sudo cp deploy/can-up.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now can-up.service
```
This is also what lets the driver HUD start at boot without needing `sudo`.
See [deploy/README.md](deploy/README.md) for the full car setup.

**3. Install dependencies and run** — from the **repo root**, not `SolarRace_OS/`
(`main.py` opens the Firebase key by a relative path, so it silently fails to
reach the cloud from anywhere else):
```bash
pip install -r SolarRace_OS/requirements.txt   # python-can, firebase-admin, PySide6
python SolarRace_OS/main.py
```
On Raspberry Pi OS Bookworm the system Python is "externally managed" and pip
will refuse — use a venv, and see [deploy/README.md](deploy/README.md) for the
full setup including auto-boot.

**Check the bus before launching the full app:**
```bash
python test_connection.py   # reports which connection is live + sample frames
candump can0                # raw view (BMS frames appear only once main.py polls)
```

> **Graceful degradation:** if no CAN connection opens, `main.py` automatically
> replays `data/can_dump.txt` so the HUD and cloud sync keep working for testing.

### B. Pit wall — Pit_Dashboard (laptop)

Run from the **repo root** (`THE RACE`), just like `SolarRace_OS` — no need to
`cd` into the folder (all pit paths resolve relative to the package):

```bash
pip install -r Pit_Dashboard/requirements_pit.txt   # streamlit, pandas, matplotlib, requests, google-auth, openpyxl

# 1. Start the collector FIRST — it ingests Firebase into telemetry.db.
python Pit_Dashboard/collector.py

# 2. In a second terminal, start the dashboard (reads telemetry.db only).
streamlit run Pit_Dashboard/pit_dashboard.py
```

(You can still `cd Pit_Dashboard` and run `python collector.py` /
`streamlit run pit_dashboard.py` if you prefer.)

**One-click (Windows):** instead of the two commands above, just run
`run_pit.bat` — it opens the collector and the dashboard in two windows for you.
SQLite needs no install or setup: it's built into Python, and `telemetry.db` plus
its table are created automatically on first run.
The dashboard opens at `http://localhost:8501`. If it shows *"No data yet — is
collector.py running?"*, start the collector. The collector backfills all history
on first run and, after any pit-side network drop, resumes from the last stored
sample (catch-up via `orderBy="$key"&startAt`), so no samples are lost as long as
the car keeps pushing to `telemetry_history`.

**Export.** The dashboard's Export panel produces a clean, readable **Excel
workbook** (`.xlsx`): a formatted **Data** sheet (human-friendly columns with
units, frozen header, filter), a **Charts** sheet of history graphs, and a
**Faults** sheet. The systems multiselect picks which columns/charts/sheets
appear. (Internal keys, redundant timestamps, and the raw fault columns from the
old CSV dump are gone — no more `#NAME?` in Excel.)

**From the command line** (same filters; output format follows the `--out`
extension — `.xlsx` → workbook, anything else → raw CSV):
```bash
python export.py --out race.xlsx                        # Excel workbook (everything)
python export.py --out race.csv                         # raw CSV (machine use)
python export.py --out batt.xlsx --group "BMS (battery)" # one subsystem
python export.py --out window.xlsx --start 2026-06-18T09:00 --end 2026-06-18T11:00
python export.py --list-metrics                         # show metrics & groups
```

---

## 📡 Telemetry Data Model

`main.py` publishes to **two** Firebase nodes each push (~1 Hz):

* **`live_telemetry`** — a single snapshot, **overwritten** every push (the "now").
* **`telemetry_history`** — the same payload `.push()`ed under an auto-generated
  chronological key, **append-only** (nothing is overwritten). This node is what
  the pit collector streams to build local history and to catch up after a drop.

Both carry the identical payload shape shown below. The pit `collector.py` reads
`telemetry_history` only, keying each SQLite row on the RTDB push id so replays
are idempotent.

```jsonc
live_telemetry/
  timestamp: <unix seconds>
  car_data:
    battery:           // JBD BMS
      bms_voltage_V, bms_current_A, bms_remaining_Ah,
      bms_full_capacity_Ah, bms_cycles, bms_soc_percent,
      bms_has_error, bms_error_code, bms_protections[], bms_balancing_active,
      bms_string_count, bms_ntc_count,
      bms_temp_1_C, bms_temp_2_C, bms_temp_3_C,
      bms_cell_01_V ... bms_cell_NN_V
    motor:             // SiliXcon LYNX MMS
      mms_rpm, mms_power_W, mms_temperature_C,
      mms_estimated_soc_percent, mms_measured_voltage_V,
      mms_has_error, mms_error_code,
      odometer_m, calculated_lap
    temp_controller:   // J1939 battery-temperature module
      battery_temp_C (= average), battery_temp_avg_C,
      battery_temp_low_C, battery_temp_high_C, temp_module
    gps:               // live from gpsd (gps_reader.py)
      lat, lon,        // ABSENT entirely when there is no fix — never 0,0
      fix_mode (2=2D, 3=3D), alt_m, speed_kmh, track_deg,
      sats_used, fix_age_s, stale
```

**GPS is published independently of CAN.** Telemetry is normally pushed when a CAN
frame is decoded, so a quiet bus used to mean no position at all. `main.py` also
publishes every `GPS_PUBLISH_INTERVAL_S` whenever a fix exists, so the pit map works
with the car parked and the bus down — which is how you check it before a race. With
no fix nothing extra is sent, so a car without GPS behaves exactly as before.
The pit stores only `lat`/`lon` as columns; the rest is kept in `raw_json`.

**BMS polling:** the JBD BMS is master/slave — it stays silent until queried.
`main.py` transmits each `BMS_POLL_IDS` frame (the ID carrying a single `0x5A` byte)
once per second; the BMS replies on the same ID and those replies are decoded normally.

---

## 🔒 Security & Track Notes

1. **Firebase keys** — `serviceAccountKey.json` (Firebase Admin SDK) is required in both
   `SolarRace_OS/cloud/` and `Pit_Dashboard/`. It is listed in `.gitignore`; **never commit it**.
   If a key was ever pushed, rotate it in the Google Cloud console.
2. **Track adaptation** — the velocity profile, sector layout, and weather coordinates are
   set for **Circuit Zolder (4000 m)**. For another venue, update the track constants in the
   pit dashboard and the coordinates in `weather_service.py` / `fetch_zolder_weather`.

---

*Afeka Solar & Electric Racing Team — telemetry & strategy.*
