#!/bin/bash
# =============================================================================
# stop_hud.sh — shut the driver HUD down and keep it down until next reboot.
#
# The in-window routes (Ctrl+Shift+Q, Alt+F4) exit 42, which start_hud.sh reads
# as "deliberate". This script is the route that still works when the window is
# frozen, when the compositor eats the key, or when you are on SSH with no GUI.
#
# It writes the stop file FIRST, so the supervisor loop cannot race us and
# relaunch the HUD in the gap between the kill and the loop's next check.
# That file lives in XDG_RUNTIME_DIR, which the Pi clears on every boot — so
# stopping the HUD today never stops it starting tomorrow.
# =============================================================================
set -u

RUNDIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
STOP_FILE="${RUNDIR}/solarrace-hud.stop"
GRACE_S=10

# ── finding the right processes ──────────────────────────────────────────── #
# A plain `pgrep -f SolarRace_OS/main.py` is NOT safe here: it matches any
# shell whose command line merely CONTAINS that text — including the terminal
# you launch this from, and including this script's own pgrep. That mistake
# kills your session instead of the HUD.
#
# So both matchers below require the path to be a real argv element (cmdline is
# NUL-separated; we compare whole fields) and check the process name too. Text
# buried inside some shell's `-c '...'` string is a single argv blob and never
# compares equal to the path, so interactive shells are immune.
argv_has_suffix() {          # $1 = pid, $2 = path suffix to match exactly
    local pid="$1" want="$2" arg
    while IFS= read -r -d '' arg; do
        [[ "${arg}" == *"${want}" ]] && return 0
    done < "/proc/${pid}/cmdline" 2>/dev/null
    return 1
}

find_pids() {                # $1 = argv suffix, $2 = /proc comm prefix
    local pid comm
    for pid in $(pgrep -f "$1" 2>/dev/null); do
        [ "${pid}" = "$$" ] && continue
        [ -r "/proc/${pid}/comm" ] || continue
        comm="$(< "/proc/${pid}/comm")"
        [[ "${comm}" == "$2"* ]] || continue
        argv_has_suffix "${pid}" "$1" && echo "${pid}"
    done
}

touch "${STOP_FILE}" || { echo "stop_hud.sh: cannot write ${STOP_FILE}" >&2; exit 1; }
echo "stop file set: ${STOP_FILE}"

# Kill the supervisor before the app, or it sees the app die and counts a crash.
mapfile -t wrappers < <(find_pids "deploy/start_hud.sh" "bash")
if [ "${#wrappers[@]}" -gt 0 ]; then
    echo "stopping supervisor: ${wrappers[*]}"
    kill "${wrappers[@]}" 2>/dev/null
fi

# SIGTERM the HUD itself. main.py installs a SIGTERM handler that shuts the CAN
# worker down cleanly — pulling the socket out from under a live bus instead
# would leave can0/can1 with a half-open socket for the next run to trip over.
mapfile -t apps < <(find_pids "SolarRace_OS/main.py" "python")
if [ "${#apps[@]}" -eq 0 ]; then
    echo "no HUD process running."
    exit 0
fi

echo "stopping HUD: ${apps[*]}"
kill "${apps[@]}" 2>/dev/null

for ((i = 0; i < GRACE_S; i++)); do
    mapfile -t apps < <(find_pids "SolarRace_OS/main.py" "python")
    [ "${#apps[@]}" -eq 0 ] && { echo "HUD stopped cleanly."; exit 0; }
    sleep 1
done

echo "still alive after ${GRACE_S}s — forcing." >&2
kill -KILL "${apps[@]}" 2>/dev/null
sleep 1
mapfile -t apps < <(find_pids "SolarRace_OS/main.py" "python")
[ "${#apps[@]}" -gt 0 ] && { echo "FAILED to stop: ${apps[*]}" >&2; exit 1; }
echo "HUD killed."
