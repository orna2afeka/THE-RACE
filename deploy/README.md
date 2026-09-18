# Deployment

Two targets: the **car** (Raspberry Pi 5, driver HUD) and the **pit** (Windows
laptop). The pit needs nothing from this folder — double-click
`Start Pit Dashboard.bat` at the repo root and it sets itself up.

---

## Car — Raspberry Pi 5, Raspberry Pi OS Bookworm

Goal: power on → no password prompt → the fullscreen driver HUD appears by
itself, and comes back if it ever crashes.

Everything below assumes this car's Pi: the user `orna2` and the repo at
`~/Desktop/THE-RACE-main` (i.e. `/home/orna2/Desktop/THE-RACE-main`).
Substitute your own and keep them consistent — and if you change them, change
the `Exec=` line in both `deploy/*.desktop` files too, since XDG autostart
cannot expand `~` and a stale path there fails silently.

### 1. System packages

```bash
sudo apt update
sudo apt install -y gpsd gpsd-clients can-utils python3-gpiozero python3-lgpio
```

`python3-gpiozero` and `python3-lgpio` come from apt on purpose: the pip build
of gpiozero does not drive the Pi 5's GPIO correctly.

### 2. GPS

```bash
sudo nano /etc/default/gpsd
#   DEVICES="/dev/ttyUSB1"
#   GPSD_OPTIONS="-n"
sudo systemctl enable --now gpsd.socket gpsd
gpspipe -w -n 5          # should print JSON
```

`/dev/ttyUSB1` is not stable across reboots. Once it works, switch to the fixed
name from `ls -l /dev/serial/by-id/` so a re-enumeration cannot break GPS.

**This car's GPS is inside the LTE modem**, and gpsd alone is not enough for it.
The receiver is the GNSS engine of the SIMCom SIM7600G-H, which means two extra
things have to happen at every boot — neither of which reports an error when it
does not:

1. **The GNSS engine starts switched off.** The modem enumerates, LTE connects,
   `mmcli -m 0` says `state: connected` — and the NMEA port emits nothing,
   because GNSS is a separate subsystem nobody has started. `mmcli -m 0
   --location-status` showing `enabled: 3gpp-lac-ci` with no `gps-*` entry is
   what that looks like.
2. **gpsd never learns the port exists.** gpsd starts at boot; the modem's
   ttyUSB nodes appear ~20 s later, by which time gpsd has failed to open
   `DEVICES=` and freed it. gpsd's hot-add udev rule only matches known GPS
   vendor IDs, and a SimTech modem is not one, so nothing adds it back.

`gps-up.service` does both, after ModemManager and gpsd are up:

```bash
sudo cp ~/Desktop/THE-RACE-main/deploy/gps-up.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gps-up.service
systemctl status gps-up            # ends with "handed /dev/ttyUSBx to gpsd"
gpspipe -w -n 5                    # now prints JSON with TPV reports
```

It asks ModemManager which port is the GPS one rather than assuming `ttyUSB1`,
so a re-enumeration cannot point it at the modem's AT port instead. Re-running
it is harmless. On a car with a plain USB GPS dongle this unit is unnecessary —
gpsd's own udev rule handles those.

**Diagnosing "no fix":** start `main.py` and read its two `🛰️` lines. The
hardware line names gpsd's device AND the kernel's serial ports, which is what
separates the three causes — nothing enumerated (receiver unplugged), ports
present but gpsd holding none (wrong `DEVICES=`, or this unit did not run), or
gpsd reading the port fine (then it is antenna or sky view; note the SIM7600's
GNSS antenna connector is SEPARATE from its LTE MAIN/AUX ones).

### 3. Python environment

Bookworm marks the system Python "externally managed" (PEP 668), so `pip
install` into it is blocked. Use a venv — with system site-packages, so it can
still see the apt-installed gpiozero:

```bash
python3 -m venv --system-site-packages ~/Desktop/THE-RACE-main/.venv
~/Desktop/THE-RACE-main/.venv/bin/pip install -r ~/Desktop/THE-RACE-main/SolarRace_OS/requirements.txt
```

If `PySide6` has no aarch64 wheel for your Python, install Qt from apt instead —
the venv above will see it:

```bash
sudo apt install -y python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtwidgets
```

### 4. CAN bus at boot

```bash
sudo cp ~/Desktop/THE-RACE-main/deploy/can-up.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now can-up.service
ip -details link show can0        # expect "state UP" and "bitrate 500000"
ip -details link show can1        # expect "state UP" and "bitrate 500000"
```

This runs as root at boot, so the HUD never needs `sudo` — see the note in
`can-up.service` for why that matters. `main.py` checks whether each interface
is already up and does nothing when this unit has done its job.

### 5. Auto-login and no screen blanking

```bash
sudo raspi-config nonint do_boot_behaviour B4   # desktop, autologin
sudo raspi-config nonint do_blanking 1          # 1 = DISABLE blanking
sudo raspi-config nonint do_ssh 0               # 0 = ENABLE ssh (your way in)
sudo raspi-config nonint do_hostname solarcar
```

The `0 = enable` convention reads backwards; it is the shell convention where 0
means success. Verify:

```bash
sudo raspi-config nonint get_autologin    # 0 means autologin is ON
systemctl get-default                     # graphical.target
```

**Enable SSH.** Once the HUD is fullscreen with a hidden cursor and restarts
itself, SSH is your only comfortable way back in.

Under Wayland the raspi-config blanking setting writes an X11 config that the
compositor ignores. On Pi OS 12 (wayfire) also add to `~/.config/wayfire.ini`:

```ini
[idle]
dpms_timeout = -1
screensaver_timeout = -1
```

### 6. Autostart the HUD

```bash
chmod +x ~/Desktop/THE-RACE-main/deploy/start_hud.sh
mkdir -p ~/.config/autostart
cp ~/Desktop/THE-RACE-main/deploy/solarrace-hud.desktop ~/.config/autostart/
nano ~/.config/autostart/solarrace-hud.desktop   # fix the path if not /home/pi
```

XDG autostart is used rather than a systemd user service or a compositor
autostart file because it is the only one that works **unchanged on both**
wayfire (Pi OS 12) and labwc (Pi OS 13). A compositor-specific file would
silently stop launching the HUD after an OS upgrade — discovered at a race, that
is a bad morning.

Restart-on-crash is handled by `start_hud.sh`, which is the one thing systemd
would otherwise have provided.

### 6b. Autostart the reverse camera (second screen)

Only needed on a car with the reverse camera fitted. Nothing in this repo
decodes video: the camera is a USB UVC device and `mpv` reads it directly, so
this step is entirely Pi-side and touches no Python.

```bash
sudo apt install -y mpv v4l-utils
chmod +x ~/Desktop/THE-RACE-main/deploy/start_camera.sh
cp ~/Desktop/THE-RACE-main/deploy/solarrace-camera.desktop ~/.config/autostart/
nano ~/.config/autostart/solarrace-camera.desktop   # fix the path if not /home/pi
```

**Check the camera is seen, and on which node**, before trusting the autostart:

```bash
v4l2-ctl --list-devices          # what is attached
ls -l /dev/v4l/by-id/            # the stable names start_camera.sh prefers
~/Desktop/THE-RACE-main/deploy/start_camera.sh   # run it by hand once, from the desktop
tail -f ~/hud-logs/camera.log       # what it decided
```

A USB capture dongle normally registers **two** `/dev/video` nodes and only one
of them captures; opening the other gives a black window and no error. That is
why the script picks by capability rather than by number, and prefers
`/dev/v4l/by-id/` — those names survive a reboot and a change of USB port,
which `/dev/video0` does not once the GPS dongle is also enumerating.

**Which screen it lands on** is the one thing that needs a real test. The script
fullscreens on output index 1 (the second screen); if the camera and the HUD end
up on the same screen, or swapped, flip it:

```bash
wlr-randr                        # lists the outputs in index order
# then either edit FS_SCREEN in start_camera.sh, or set it per-boot:
SOLARRACE_CAM_SCREEN=0 ~/Desktop/THE-RACE-main/deploy/start_camera.sh
```

⚠️ **Adding a second screen can move the HUD.** The HUD asks to be fullscreen
and lets the compositor choose where, which with one screen was never a
question. If it comes up on the camera's screen, pin it in
`~/.config/wayfire.ini` with a window rule matching the HUD, or simply swap
which physical HDMI each panel is plugged into — the cable is the quicker fix
and it cannot rot across an OS upgrade.

To stop the camera without touching the autostart (it clears on reboot, exactly
like the HUD's):

```bash
touch "${XDG_RUNTIME_DIR}/solarrace-camera.stop"
pkill -f start_camera.sh
```

Latency is tuned over picture quality on purpose (`--profile=low-latency
--untimed`): a reverse view a second behind reality is worse than a slightly
choppy one. If the feed is choppy rather than late, drop `input_format=mjpeg`
from the script and let mpv negotiate, or lower `SOLARRACE_CAM_FPS`.

### 7. Test before trusting it

```bash
# by hand first — note the cd, it matters (see below)
cd ~/Desktop/THE-RACE-main && ./.venv/bin/python3 -u SolarRace_OS/main.py

# then the wrapper
~/Desktop/THE-RACE-main/deploy/start_hud.sh &
tail -f ~/hud-logs/hud.log

sudo reboot     # the real test
```

**Always run from the repo root.** `main.py` opens
`SolarRace_OS/cloud/serviceAccountKey.json` by a *relative* path, so from any
other directory Firebase silently fails to initialise — the HUD looks perfect
and the pit receives nothing. The wrapper `cd`s to the root for this reason.

### 8. Getting out of the HUD

**With a keyboard on the Pi — `Alt+F4`.** The HUD exits with code 42, which the
wrapper reads as "an engineer asked for this" and stays down; a reboot (or
running `start_hud.sh` again) brings it back. `Ctrl+Shift+Q` does exactly the
same, and is the fallback if the compositor has `Alt+F4` disabled. Other useful
chords: `Ctrl+Shift+C` unhides the mouse cursor, `Ctrl+R` restarts the CAN
worker, `Ctrl+T` stops it.

No keyboard, only SSH? It is fullscreen, the cursor is hidden, and the wrapper
restarts it on any *other* exit code. So:

```bash
ssh pi@solarcar.local
tail -f ~/hud-logs/hud.log                      # watch it live

touch "$XDG_RUNTIME_DIR/solarrace-hud.stop"     # tell the wrapper to stay down
pkill -f 'SolarRace_OS/main.py'                 # then stop the HUD
# ... debug ...
rm -f "$XDG_RUNTIME_DIR/solarrace-hud.stop"     # hand it back
~/Desktop/THE-RACE-main/deploy/start_hud.sh &
```

The stop file lives on tmpfs, so **a power cycle always clears it** — a
debugging session can never leave the car booting to a dead screen.

No network? **Ctrl+Alt+F2** switches to a text console, **Ctrl+Alt+F1** returns.

The stop file is still the right tool when the HUD must stay down across a
*reboot-free* debugging session started over SSH, or when the Pi has no keyboard
attached. `Alt+F4` covers the common case: someone standing at the car.

### Logs

`~/hud-logs/hud.log`, rotated each boot and capped at 20 MB, keeping 3 files.

### Is the Pi struggling? `deploy/pi_diag.sh`

```bash
bash ~/Desktop/THE-RACE-main/deploy/pi_diag.sh          # read-only snapshot
bash ~/Desktop/THE-RACE-main/deploy/pi_diag.sh --spy    # + 10 stack dumps of the HUD
```

Prints, and saves to `~/hud-logs/pi_diag_<date>.txt`: throttling and
under-voltage flags decoded, per-thread CPU of the HUD process, the camera
player's cost, CAN RX overruns (frames the kernel dropped because nobody read
the socket), upload-error counts from `hud.log`, and HTTPS round-trip times to
the database. It changes nothing and is safe during a drive.

`--spy` is opt-in because it attaches py-spy to the HUD, which pauses it for a
few milliseconds per dump (and may `pip install py-spy` into the venv the first
time). Its tally says, per dump, whether the CAN thread was inside a Firebase
upload, reading the bus, or idle, and whether the GUI thread was painting.
Most dumps in the upload = the freeze-then-jump the driver sees. The pit-side
half of the same question is `python tools/car_stall_report.py`, which reads
the freezes out of the pit's own telemetry store.

---

## Pit — Windows laptop

```
git clone <repo>
double-click "Start Pit Dashboard.bat"
```

The launcher finds a usable Python, installs dependencies on first run, starts
the collector and the dashboard, and opens the browser at
http://localhost:8000 (other devices on the pit LAN use
http://<laptop-ip>:8000). No Node is needed: the built frontend is committed.
First run needs internet (~300 MB of wheels) — do it in the workshop, not the
paddock.

**Python 3.9–3.12 only.** The pinned numpy/pandas/matplotlib publish no wheels
for 3.13+, so pip would try to compile numpy from source and fail. The launcher
checks the version and says so rather than letting you read a wall of compiler
errors.

**`serviceAccountKey.json` is not in the repo.** It is a secret and is
gitignored, so a fresh clone cannot reach Firebase. Get it from the team and put
it in `Pit_Dashboard/`. Without it the launcher still starts the dashboard —
useful for reviewing stored telemetry — but no new data arrives.

**Windows Firewall** will prompt on first launch because the dashboard binds all
interfaces for LAN access. Click Allow, or phones and other laptops will not be
able to connect.

**Pit LAN:** use your own phone hotspot or router. Campus and venue WiFi
normally isolate clients from each other, which blocks every device except the
laptop running the dashboard.



# 0. Get the code there
git clone <your repo url> ~/Desktop/THE-RACE-main && cd ~/Desktop/THE-RACE-main

# 1. System packages
sudo apt update
sudo apt install -y gpsd gpsd-clients can-utils python3-gpiozero python3-lgpio

# 2. GPS — point gpsd at your receiver
sudo nano /etc/default/gpsd        # DEVICES="/dev/ttyUSB1"   GPSD_OPTIONS="-n"
sudo systemctl enable --now gpsd.socket gpsd
# GPS lives inside the SIM7600 modem: its GNSS engine boots OFF and gpsd never
# sees the port on its own. This unit fixes both — see section 2 above.
sudo cp deploy/gps-up.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gps-up.service
gpspipe -w -n 5                     # must print JSON

# 3. Python (Bookworm blocks system pip — venv required)
python3 -m venv --system-site-packages ~/Desktop/THE-RACE-main/.venv
~/Desktop/THE-RACE-main/.venv/bin/pip install -r ~/Desktop/THE-RACE-main/SolarRace_OS/requirements.txt

# 4. CAN bus up at every boot
sudo cp deploy/can-up.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now can-up.service

# 5. Auto-login, no screen blanking, SSH on
sudo raspi-config nonint do_boot_behaviour B4    # desktop autologin
sudo raspi-config nonint do_blanking 1           # 1 = DISABLE blanking
sudo raspi-config nonint do_ssh 0                # 0 = ENABLE ssh

# 6. Start the HUD at boot
chmod +x deploy/start_hud.sh
mkdir -p ~/.config/autostart
cp deploy/solarrace-hud.desktop ~/.config/autostart/
nano ~/.config/autostart/solarrace-hud.desktop   # fix the path if not /home/pi

# 6b. Reverse camera at boot (only on a car that has one)
sudo apt install -y mpv v4l-utils
chmod +x deploy/start_camera.sh
cp deploy/solarrace-camera.desktop ~/.config/autostart/
nano ~/.config/autostart/solarrace-camera.desktop
wlr-randr                                        # confirm which screen is which

# 7. Test, then reboot
cd ~/Desktop/THE-RACE-main && ./.venv/bin/python3 -u SolarRace_OS/main.py
sudo reboot