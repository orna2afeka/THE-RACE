# Pi update runbook — 2026-08-25

Apply this **on the car's Raspberry Pi**, in order. It covers the change that
needs work on the Pi itself, because it does not take effect from a `git pull`
alone:

| # | Change | Why the Pi needs manual work |
|---|--------|------------------------------|
| 1 | **CAN `can1` 1 Mbit/s → 500 kbit/s** | `config.py` does **not** set the SocketCAN rate. `ip link` does. A Pi left as-is keeps `can1` at 1 Mbit/s and decodes nothing from the MMS. |

Nothing else needs Pi-side action. In particular the **throttle / GPIO0 feature
is automatic** — the car transmits the `0x147` report request itself on every
run, so it just starts working after the restart in step 3.

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
candump can0        # BMS replies appear once the app is polling (step 3)
```

> ⚠️ **If `can1` reports `bitrate 500000` but `candump can1` stays silent, the
> MMS itself was not re-flashed.** At the wrong bitrate the interface looks
> perfectly healthy and simply never decodes a frame — that is the exact failure
> mode that drove `can0` to `BUS-OFF` last time. Both ends of a wire have to
> agree; check the controller's own configuration before changing anything here.

---

## 3. Restart the HUD and confirm

```bash
"$RACE/deploy/start_hud.sh" &
```

Watch the startup output for these lines:

```
[can] opened socketcan:can0 @ 500kbps
[can] opened socketcan:can1 @ 500kbps
🦶 Throttle report armed: GPIO input 0x8..0x8 -> bank 0 (0x150) every 100 ms
```

On the driver's screen:

- **DS001** — speed, target and the side gauges update; the top bar shows
  NET · PIT · the power-map badge.
- **DS002** — speed, SoC and pack voltage on the top row; currents and
  temperatures below.

On the pit dashboard: **Throttle**, **Efficiency Zone** and **Throttle Raw**
tiles under Live Metrics, and **Throttle** available in the History charts. The pit's
SQLite schema migrates itself on first start — no manual step.

---

## 4. Known-good end state

| Check | Expected |
|-------|----------|
| `ip -details link show can0` | `state UP`, `bitrate 500000` |
| `ip -details link show can1` | `state UP`, `bitrate 500000` |
| `candump can1` | continuous frames, `0x600`–`0x628` |
| `candump can0` | BMS replies while the app polls, plus `0x150` throttle reports |
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
