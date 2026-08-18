# Deployment

Two targets: the **car** (Raspberry Pi 5, driver HUD) and the **pit** (Windows
laptop). The pit needs nothing from this folder — double-click
`Start Pit Dashboard.bat` at the repo root and it sets itself up.

---

## Car — Raspberry Pi 5, Raspberry Pi OS Bookworm

Goal: power on → no password prompt → the fullscreen driver HUD appears by
itself, and comes back if it ever crashes.

Everything below assumes the user `pi` and the repo at `~/THE-RACE`. Substitute
your own and keep them consistent.

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

### 3. Python environment

Bookworm marks the system Python "externally managed" (PEP 668), so `pip
install` into it is blocked. Use a venv — with system site-packages, so it can
still see the apt-installed gpiozero:

```bash
python3 -m venv --system-site-packages ~/THE-RACE/.venv
~/THE-RACE/.venv/bin/pip install -r ~/THE-RACE/SolarRace_OS/requirements.txt
```

If `PySide6` has no aarch64 wheel for your Python, install Qt from apt instead —
the venv above will see it:

```bash
sudo apt install -y python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtwidgets
```

### 4. CAN bus at boot

```bash
sudo cp ~/THE-RACE/deploy/can-up.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now can-up.service
ip -details link show can0        # expect "state UP" and "bitrate 1000000"
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
chmod +x ~/THE-RACE/deploy/start_hud.sh
mkdir -p ~/.config/autostart
cp ~/THE-RACE/deploy/solarrace-hud.desktop ~/.config/autostart/
nano ~/.config/autostart/solarrace-hud.desktop   # fix the path if not /home/pi
```

XDG autostart is used rather than a systemd user service or a compositor
autostart file because it is the only one that works **unchanged on both**
wayfire (Pi OS 12) and labwc (Pi OS 13). A compositor-specific file would
silently stop launching the HUD after an OS upgrade — discovered at a race, that
is a bad morning.

Restart-on-crash is handled by `start_hud.sh`, which is the one thing systemd
would otherwise have provided.

### 7. Test before trusting it

```bash
# by hand first — note the cd, it matters (see below)
cd ~/THE-RACE && ./.venv/bin/python3 -u SolarRace_OS/main.py

# then the wrapper
~/THE-RACE/deploy/start_hud.sh &
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
~/THE-RACE/deploy/start_hud.sh &
```

The stop file lives on tmpfs, so **a power cycle always clears it** — a
debugging session can never leave the car booting to a dead screen.

No network? **Ctrl+Alt+F2** switches to a text console, **Ctrl+Alt+F1** returns.

The stop file is still the right tool when the HUD must stay down across a
*reboot-free* debugging session started over SSH, or when the Pi has no keyboard
attached. `Alt+F4` covers the common case: someone standing at the car.

### Logs

`~/hud-logs/hud.log`, rotated each boot and capped at 20 MB, keeping 3 files.

---

## Pit — Windows laptop

```
git clone <repo>
double-click "Start Pit Dashboard.bat"
```

The launcher finds a usable Python, installs dependencies on first run, starts
the collector and the dashboard, and opens the browser. First run needs
internet (~300 MB of wheels) — do it in the workshop, not the paddock.

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
git clone <your repo url> ~/THE-RACE && cd ~/THE-RACE

# 1. System packages
sudo apt update
sudo apt install -y gpsd gpsd-clients can-utils python3-gpiozero python3-lgpio

# 2. GPS — point gpsd at your receiver
sudo nano /etc/default/gpsd        # DEVICES="/dev/ttyUSB1"   GPSD_OPTIONS="-n"
sudo systemctl enable --now gpsd.socket gpsd
gpspipe -w -n 5                     # must print JSON

# 3. Python (Bookworm blocks system pip — venv required)
python3 -m venv --system-site-packages ~/THE-RACE/.venv
~/THE-RACE/.venv/bin/pip install -r ~/THE-RACE/SolarRace_OS/requirements.txt

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

# 7. Test, then reboot
cd ~/THE-RACE && ./.venv/bin/python3 -u SolarRace_OS/main.py
sudo reboot