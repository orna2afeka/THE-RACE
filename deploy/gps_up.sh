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

# ── 2. Start the GNSS engine ─────────────────────────────────────────────── #
if "$MMCLI" -m "$index" --location-status 2>/dev/null | grep -q "gps-unmanaged"; then
    if "$MMCLI" -m "$index" --location-status 2>/dev/null |
            grep -A1 "enabled:" | grep -q "gps-unmanaged"; then
        log "GNSS already enabled"
    else
        "$MMCLI" -m "$index" --location-enable-gps-unmanaged 2>&1 | sed 's/^/[gps-up] /'
    fi
else
    log "modem reports no gps-unmanaged capability — skipping GNSS enable"
fi

# ── 3. Find the NMEA port and give it to gpsd ────────────────────────────── #
# Asked, not assumed: the node is ttyUSB1 on this module today, but the index
# depends on enumeration order, and a wrong path fails exactly like a dead
# receiver. ModemManager already knows which port is the GPS one.
port=$("$MMCLI" -m "$index" -K 2>/dev/null |
       awk -F': ' '/ports.value/ && /\(gps\)/ {print $2}' |
       awk '{print $1}' | head -1)
if [ -z "$port" ]; then
    log "ModemManager reports no (gps) port — leaving gpsd alone"
    exit 0
fi
dev="/dev/$port"
log "GNSS NMEA port is $dev"

for _ in $(seq 1 10); do
    [ -c "$dev" ] && break
    sleep 1
done
if [ ! -c "$dev" ]; then
    log "$dev never appeared"
    exit 0
fi

# gpsdctl add is idempotent — gpsd ignores a device it already holds.
"$GPSDCTL" add "$dev" 2>&1 | sed 's/^/[gps-up] /'
log "handed $dev to gpsd"
