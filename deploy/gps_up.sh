#!/bin/bash
# ---------------------------------------------------------------------------
# Start the car's GNSS receiver and hand it to gpsd.
#
# The "GPS" on this car is not a GPS dongle: it is the GNSS engine inside the
# SIMCom SIM7600G-H LTE modem. Two things follow from that, and BOTH of them
# silently produce "no fix" with no error anywhere:
#
#   1. The GNSS engine is OFF until something turns it on. The modem enumerates,
#      LTE connects, `mmcli` reports it as connected — and its NMEA port emits
#      nothing at all, because GNSS is a separate subsystem that has not been
#      started. `mmcli --location-status` shows `enabled: 3gpp-lac-ci` and no
#      gps-* entry when that is the case.
#
#   2. gpsd never learns the port exists. gpsd starts at boot; the modem takes
#      ~20 s after power-on to enumerate its ttyUSBs, by which time gpsd has
#      already failed to open its configured DEVICES= path and freed it. gpsd's
#      own udev hot-add rule only matches known GPS vendor IDs, and a SimTech
#      modem is not one, so nothing ever adds it back.
#
# --location-enable-gps-unmanaged (rather than --location-enable-gps-nmea) is
# deliberate: it starts the GNSS engine but leaves the NMEA port alone, so gpsd
# can own it. With -nmea, ModemManager reads that port itself and gpsd finds a
# device it cannot get a byte out of.
#
# Install: see deploy/gps-up.service (this script is what that unit runs).
# Safe to re-run at any time — every step here is idempotent.
# ---------------------------------------------------------------------------
set -u

MMCLI=${MMCLI:-/usr/bin/mmcli}
GPSDCTL=${GPSDCTL:-/usr/sbin/gpsdctl}
# The modem's ttyUSB nodes appear a good while after the USB device does, and
# ModemManager then needs its own moment to probe them. At boot this script can
# easily start before any of that, so it waits rather than failing.
WAIT_S=${GPS_WAIT_S:-120}

log() { echo "[gps-up] $*"; }

deadline=$((SECONDS + WAIT_S))

# ── 1. Wait for ModemManager to have a modem ─────────────────────────────── #
modem=""
while [ $SECONDS -lt $deadline ]; do
    modem=$("$MMCLI" -L 2>/dev/null | grep -o '/Modem/[0-9]\+' | head -1 | tr -d '\n')
    [ -n "$modem" ] && break
    sleep 2
done
if [ -z "$modem" ]; then
    log "no modem after ${WAIT_S}s — nothing to enable (is it plugged in?)"
    exit 0          # not an error: a car running without the modem is valid
fi
index=${modem##*/}
log "modem $index found"

# ── 2. Find the NMEA port ────────────────────────────────────────────────── #
# Asked, not assumed: the node is ttyUSB1 on this module today, but the index
# depends on enumeration order, and a wrong path fails exactly like a dead
# receiver. ModemManager already knows which port is the GPS one.
port=$("$MMCLI" -m "$index" -K 2>/dev/null |
       awk -F': ' '/ports.value/ && /\(gps\)/ {print $2}' |
       awk '{print $1}' | head -1)
if [ -z "$port" ]; then
    log "ModemManager reports no (gps) port — cannot continue"
    exit 1
fi
dev="/dev/$port"
log "GNSS NMEA port is $dev"

for _ in $(seq 1 10); do
    [ -c "$dev" ] && break
    sleep 1
done
if [ ! -c "$dev" ]; then
    log "$dev never appeared"
    exit 1
fi

# ── 3. Start the GNSS engine, and VERIFY IT BY ITS OUTPUT ────────────────── #
#
# Not by what ModemManager reports. MM's location-status is its own bookkeeping
# and it goes out of step with the module in both directions:
#
#   • Restart MM and its record resets to "disabled" while the module's GNSS
#     engine is still running — the module keeps its GNSS session across an MM
#     restart, and answers the next start command with a plain ERROR, which MM
#     surfaces as a bare "Unknown error".
#   • So a FAILED enable does not mean GNSS is off, and a successful one would
#     not prove bytes are moving either.
#
# The wire settles it. Attempt the enable, ignore what it claims, then look for
# an NMEA sentence. If the port is silent, resync MM with the module (disable,
# pause, enable) and look again — and if it is still silent, exit non-zero so
# systemd retries and `systemctl status` says so, instead of showing green over
# a GPS that will never fix.
#
# gpsd is taken OFF the port first: it reads in a tight loop and would starve
# this check of the very bytes it is looking for. It gets the port back at the
# end — which is also the step that registers it in the first place.
"$GPSDCTL" remove "$dev" >/dev/null 2>&1 || true

nmea_flowing() {
    # A live receiver emits a sentence about once a second; 8 s is generous
    # even for a module that has only just been told to start.
    timeout 8 grep -m1 -q '^\$G' "$dev" 2>/dev/null
}

"$MMCLI" -m "$index" --location-enable-gps-unmanaged 2>&1 | sed 's/^/[gps-up] /'

status=0
if nmea_flowing; then
    log "NMEA confirmed on $dev"
else
    log "no NMEA after enable — resyncing ModemManager with the module"
    "$MMCLI" -m "$index" --location-disable-gps-unmanaged 2>&1 | sed 's/^/[gps-up] /'
    sleep 3
    "$MMCLI" -m "$index" --location-enable-gps-unmanaged 2>&1 | sed 's/^/[gps-up] /'
    if nmea_flowing; then
        log "NMEA confirmed on $dev after resync"
    else
        log "FAILED: $dev is silent — the GNSS engine did not start"
        status=1
    fi
fi

# ── 4. Give the port to gpsd ─────────────────────────────────────────────── #
# This is the step gpsd cannot do for itself: it starts at boot, fails to open
# its configured DEVICES= because the modem has not enumerated yet, and frees
# it; its hot-add udev rule only matches known GPS vendor IDs, and a SimTech
# modem is not one. Done even when the check above failed — if the engine comes
# up later, gpsd is then already listening. gpsdctl add is idempotent.
"$GPSDCTL" add "$dev" 2>&1 | sed 's/^/[gps-up] /'
log "handed $dev to gpsd"
exit $status
