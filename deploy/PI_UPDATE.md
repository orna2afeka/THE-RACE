# Pi update runbook — 2026-08-25

Apply this **on the car's Raspberry Pi**, in order. It covers two changes that
need work on the Pi itself, because neither one takes effect from a `git pull`
alone:

| # | Change | Why the Pi needs manual work |
|---|--------|------------------------------|
| 1 | **CAN `can1` 1 Mbit/s → 500 kbit/s** | `config.py` does **not** set the SocketCAN rate. `ip link` does. A Pi left as-is keeps `can1` at 1 Mbit/s and decodes nothing from the MMS. |
| 2 | **Solar current sensor (Yoctopuce Yocto-Amp)** | Needs a pip package *and* a udev rule. Without the rule the USB device is root-only and the HUD (not root) reads nothing. |

Nothing else needs Pi-side action. In particular the **throttle / GPIO0 feature
is automatic** — the car transmits the `0x147` report request itself on every
run, so it just starts working after the restart in step 4.

> **Read step 3.0 before touching the Yocto-Amp.** There is a hardware rating
> check that has to happen before that sensor is wired into the car at all.

---

## 0. Find the repo, and confirm it is the right one

The repo root has been in more than one place on this Pi. It currently lives at
`~/Desktop/THE-RACE-main`; it has also been at `~/THE-RACE`, and inside a
`~/Desktop/THE-RACE-main` that itself contained a `THE-RACE-main` directory.
That is not a trivia note — the autostart entry spent a long time pointing at
the wrong one, which silently disabled both the HUD autostart and its
boot-time `git pull`.

Every command below assumes `$RACE`. Set it once and check it before going on:

```bash
RACE=~/Desktop/THE-RACE-main                 # <-- adjust if the checkout lives elsewhere
ls "$RACE/deploy/can-up.service" "$RACE/SolarRace_OS/main.py"
```

Both files must list. If either says "No such file or directory", find the real
root first and re-set `RACE` — do not continue with a guess:

```bash
find ~ -name can-up.service -not -path '*/.git/*' 2>/dev/null
```

---

## 1. Pull the new code

```bash
cd "$RACE" && git pull
```

If the pull reports local modifications, **stop and report them** rather than
discarding anything — the Pi is where hand-edits get made during a race weekend
and one of them may be worth keeping.

---

## 2. CAN: bring `can1` down to 500 kbit/s

Both the BMS and the MMS now run at 500 kbit/s, so both channels do too.

```bash
"$RACE/deploy/stop_hud.sh"        # release the buses before downing them

sudo cp "$RACE/deploy/can-up.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart can-up.service   # ExecStartPre downs both links first
```

### Verify before trusting it

```bash
ip -details link show can0 | grep -E "state|bitrate"   # expect UP, bitrate 500000
ip -details link show can1 | grep -E "state|bitrate"   # expect UP, bitrate 500000
```

Then confirm real traffic on each wire:

```bash
candump can1        # 0x600-0x628 should stream continuously (MMS)
candump can0        # BMS replies appear once the app is polling (step 4)
```

> ⚠️ **If `can1` reports `bitrate 500000` but `candump can1` stays silent, the
> MMS itself was not re-flashed.** At the wrong bitrate the interface looks
> perfectly healthy and simply never decodes a frame — that is the exact failure
> mode that drove `can0` to `BUS-OFF` last time. Both ends of a wire have to
> agree; check the controller's own configuration before changing anything here.

---

## 3. Solar current sensor (Yoctopuce Yocto-Amp)

### 3.0 Hardware check — a person must do this first

**Do not wire the Yocto-Amp into the car until someone has checked the MPPT's
rated output current against the sensor's limit.**

- Yocto-Amp: **10 A continuous, 17 A peak**, 5 mΩ shunt, terminal block takes
  **16 AWG maximum**.
- A 1 kW array into this ~48 V pack is roughly 20 A — **twice** the continuous
  rating. Over-current here is a fire risk, not just a bad reading.

If the MPPT can exceed ~8 A, stop: this is the wrong sensor for the line and a
hall-effect sensor should be fitted instead. Wiring procedure is in the root
[README § Solar Current Sensor](../README.md#-solar-current-sensor-yocto-amp).

**The software steps below are safe to run whether or not the sensor is
fitted** — with no sensor present the reading simply stays `—`.

### 3.1 Install the library and the udev rule

```bash
pip install yoctopuce
# or, for everything at once:
# pip install -r "$RACE/SolarRace_OS/requirements.txt"

# Let non-root processes open Yoctopuce devices (USB vendor id 24e0).
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="24e0", MODE="0666"' \
    | sudo tee /etc/udev/rules.d/51-yoctopuce.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

If `pip install` refuses with an "externally-managed-environment" error, install
it into whichever virtualenv the HUD runs from — the same one the rest of
`requirements.txt` is installed in — not with `--break-system-packages`.

### 3.2 Unplug and replug the sensor, then verify

The udev rule only applies to a device that enumerates *after* the reload.

```bash
lsusb | grep 24e0                                   # the module must be listed
python "$RACE/SolarRace_OS/modules/solar_current.py"  # prints amps once a second
```

Expect a serial number and a current **in amps**. Status word meanings:

| Status | Meaning | Fix |
|--------|---------|-----|
| `online` | Reading normally | — |
| `no_library` | `pip install yoctopuce` did not take | check which Python/venv the HUD uses |
| `no_hub` | Cannot claim the USB bus | the udev rule (3.1), or a VirtualHub / second copy of the app already holds the device |
| `searching` | Bus claimed, no Yocto-Amp found | check `lsusb`, the cable, the port |
| `offline` | It was there and vanished | the cable came loose — the expected race failure |
| `implausible` | Past the sensor's 17 A peak rating | wrong sensor for this current, or a fault |

Plug into a **USB-A** port. The Pi 5's own USB-C socket is power input only, so
a Yocto-Amp-C needs a USB-C-to-USB-A cable.

---

## 4. Restart the HUD and confirm

```bash
"$RACE/deploy/start_hud.sh" &
```

Watch the startup output for all three lines:

```
[can] opened socketcan:can0 @ 500kbps
[can] opened socketcan:can1 @ 500kbps
🦶 Throttle report armed: GPIO input 0x8..0x8 -> bank 0 (0x150) every 100 ms
☀️ Solar sensor: online            # or "no_library" / "searching" if not fitted
```

On the driver's screen:

- **DS001** — the efficiency bar under the speedometer should move with the
  throttle pedal (ECO / NORMAL / POWER), and the `☀` badge in the top bar shows
  the solar current.
- **DS002** — the top row now has four gauges, ending in **SOLAR IN**.

On the pit dashboard: **Throttle**, **Efficiency Zone**, **Throttle Raw**,
**Solar Current** and **Solar Sensor** tiles under Live Metrics, and
**Throttle** + **Solar Current** available in the History charts. The pit's
SQLite schema migrates itself on first start — no manual step.

---

## 5. Known-good end state

| Check | Expected |
|-------|----------|
| `ip -details link show can0` | `state UP`, `bitrate 500000` |
| `ip -details link show can1` | `state UP`, `bitrate 500000` |
| `candump can1` | continuous frames, `0x600`–`0x628` |
| `candump can0` | BMS replies while the app polls, plus `0x150` throttle reports |
| `lsusb \| grep 24e0` | Yocto-Amp listed (only if fitted) |
| HUD startup | both CAN channels at `500kbps`, throttle armed |

---

## If something is wrong

Report what you saw rather than working around it — several of these failures
look identical to a healthy system from the software side:

- **`can1` up at 500000 but silent** → the MMS was not re-flashed. Hardware job.
- **Throttle stays `—` while the pedal moves** → the pedal may not be on GPIO0.
  Widen `THROTTLE_GPIO_END_ID` in `SolarRace_OS/config.py` to
  `mms_parser.gpio_input_id(4)` to sweep GPIO0–4 and watch which value moves on
  the pit's **Throttle Raw** tile.
- **Throttle % looks wrong but Throttle Raw moves** → expected. The pedal
  calibration in `efficiency.py` is still a placeholder; read the raw mV with
  the pedal released and floored, and put those two numbers in that file.
- **Solar reads `—` with status `no_hub`** → udev rule, step 3.1.
- **Solar reads a negative current** → the Yocto-Amp's two terminals are
  swapped. Physical fix; the sign is shown on purpose so this is visible.
