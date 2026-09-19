#!/bin/bash
# ---------------------------------------------------------------------------
# Put the GNSS port back when the modem takes it away.
#
# WHAT THIS EXISTS FOR
# gps_up.sh hands the modem's NMEA port to gpsd once, at boot. Any re-enumeration
# of the modem after that — a USB cable change, an LTE reset, a power dip under
# transmit load — makes gpsd drop the device, and NOTHING gives it back:
#
#     $ gpspipe -w -n 2
#     {"class":"DEVICES","devices":[]}      <- gpsd has nothing
#     $ fuser /dev/ttyUSB1                  <- and nobody is holding the port
#
# gpsd's own hot-add rule only matches known GPS vendor IDs and a SimTech modem
# is not one (gps_up.sh says so at length). So the receiver carries on emitting
# NMEA into a port nobody is reading, the car keeps serving its LAST fix with a
# growing age, and the pit sees a car that claims a GPS fix and never moves.
# On 2026-09-18 that cost two hours of a test session before anyone looked.
#
# WHY IT CHECKS BEFORE IT ACTS
# The obvious version of this — run gps_up.sh on a timer — would be WORSE than
# the bug. gps_up.sh takes gpsd off the port (gpsdctl remove), greps the device
# for up to 8 s, may disable and re-enable the GNSS session, and only then hands
# the port back. Doing that every minute would interrupt a perfectly good fix
# every minute, and a GNSS session restart throws away the ephemeris the
# receiver spent 30 s per satellite decoding.
#
# So this asks gpsd one question -- do you have a device? -- and does nothing at
# all unless the answer is no. A healthy car pays one gpspipe call a minute.
#
# WHAT IT DELIBERATELY DOES NOT DO
# It does not care whether there is a FIX. No receiver can promise a fix; a car
# in a garage or under a carbon shell will sit at mode 1 forever and that is not
# something to restart anything over. The only thing repaired here is the one
# thing that is unambiguously broken and unambiguously fixable: gpsd holding no
# device while the modem is sitting there with a live NMEA port.
#
# Install:
#   sudo cp ~/Desktop/THE-RACE-main/deploy/gps-watchdog.service /etc/systemd/system/
#   sudo cp ~/Desktop/THE-RACE-main/deploy/gps-watchdog.timer   /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now gps-watchdog.timer
#   systemctl list-timers gps-watchdog        # next run, and the last result
#   journalctl -u gps-watchdog -f             # only prints when it repairs
#
# Safe to run by hand at any time. Exits 0 whenever there is nothing to do.
# ---------------------------------------------------------------------------
set -u

GPSPIPE=${GPSPIPE:-/usr/bin/gpspipe}
SYSTEMCTL=${SYSTEMCTL:-/bin/systemctl}
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GPS_UP=${GPS_UP:-$HERE/gps_up.sh}

log() { echo "[gps-watchdog] $*"; }

# ── 1. Ask gpsd what it is holding ───────────────────────────────────────── #
# gpsd answers VERSION + DEVICES + WATCH immediately on connect, so three
# records is enough and the timeout is a backstop, not the normal path.
if [ ! -x "$GPSPIPE" ]; then
    log "gpspipe not found at $GPSPIPE — cannot check, doing nothing"
    exit 0
fi

devices=$(timeout 10 "$GPSPIPE" -w -n 3 2>/dev/null | grep -m1 '"class":"DEVICES"')

if [ -z "$devices" ]; then
    # No answer at all: gpsd is down or not accepting connections. That is a
    # different fault with a different fix, and restarting the GNSS session
    # would not help it. Say so and stop.
    log "gpsd did not answer — not a port problem, leaving it alone"
    exit 0
fi

# The empty-list form is exactly what gpsd prints when it holds nothing:
#   {"class":"DEVICES","devices":[]}
case "$devices" in
    *'"devices":[]'*) ;;                 # broken: fall through and repair
    *) exit 0 ;;                         # healthy: this is the normal path
esac

# ── 2. Repair ────────────────────────────────────────────────────────────── #
log "gpsd holds no device — re-running the handoff"

# restart, NOT start. gps-up.service is Type=oneshot with RemainAfterExit=yes,
# so once it has succeeded systemd considers it active and `start` is a silent
# no-op that reports success without running anything. That cost real time to
# spot on the car; it is the whole reason this line is not `start`.
if [ -x "$SYSTEMCTL" ] && "$SYSTEMCTL" list-unit-files gps-up.service >/dev/null 2>&1; then
    "$SYSTEMCTL" restart gps-up.service
    rc=$?
else
    bash "$GPS_UP"
    rc=$?
fi

# Report what it achieved, so `journalctl -u gps-watchdog` reads as a history of
# repairs rather than of attempts.
after=$(timeout 10 "$GPSPIPE" -w -n 3 2>/dev/null | grep -m1 '"class":"DEVICES"')
case "$after" in
    *'"devices":[]'*|"") log "still no device after the handoff (rc=$rc)" ; exit 1 ;;
    *) log "device back: $(echo "$after" | grep -o '"path":"[^"]*"' | head -1)" ;;
esac
exit 0
