#!/bin/bash
# =============================================================================
# start_hud.sh — boot wrapper for the SolarRace driver HUD.
# Raspberry Pi 5 · Raspberry Pi OS Bookworm · Wayland (wayfire or labwc)
#
# Launched by ~/.config/autostart/solarrace-hud.desktop, i.e. from INSIDE the
# desktop session, so WAYLAND_DISPLAY / XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS
# are already inherited. Running it from a plain SSH shell will not give a GUI.
#
#   1. single instance      4. logs to a rotating file
#   2. waits for the compositor and the network
#   3. runs from the REPO ROOT (see the cd note below — this one matters)
#   5. restarts the HUD if it crashes, with backoff
#   6. can be told to stay down, via a file that a reboot clears
# =============================================================================
set -u

# ── configuration ────────────────────────────────────────────────────────── #
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo ROOT (derived from this script's location)
APP="${REPO}/SolarRace_OS/main.py"
VENV_PY="${REPO}/.venv/bin/python3"
LOG_DIR="${HOME}/hud-logs"
LOG="${LOG_DIR}/hud.log"
MAX_LOG_BYTES=$((20 * 1024 * 1024))
KEEP=3
RUNDIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
LOCK_FILE="${RUNDIR}/solarrace-hud.lock"
STOP_FILE="${RUNDIR}/solarrace-hud.stop"
WL_WAIT_S=20
NET_WAIT_S=30
RESTART_MIN_S=3
RESTART_MAX_S=30
HEALTHY_RUN_S=30

# ── 1. single instance ───────────────────────────────────────────────────── #
# Without this, a manual launch plus the autostart entry gives two fullscreen
# HUDs fighting over the CAN bus and the screen.
exec 9>"${LOCK_FILE}" || exit 1
if ! flock -n 9; then
    echo "start_hud.sh: another instance holds ${LOCK_FILE} — exiting." >&2
    exit 0
fi

# ── 2. logging ───────────────────────────────────────────────────────────── #
mkdir -p "${LOG_DIR}"

rotate_logs() {
    local i
    for ((i = KEEP - 1; i >= 1; i--)); do
        [ -f "${LOG}.${i}" ] && mv -f "${LOG}.${i}" "${LOG}.$((i + 1))"
    done
    [ -f "${LOG}" ] && mv -f "${LOG}" "${LOG}.1"
    rm -f "${LOG}.$((KEEP + 1))"
    : > "${LOG}"
}

rotate_logs                       # fresh log each boot; keeps the last 3
exec >>"${LOG}" 2>&1

say() { echo "[$(date '+%F %T')] $*"; }

say "=============================================================="
say "start_hud.sh starting. user=$(id -un) repo=${REPO}"
say "session: WAYLAND_DISPLAY='${WAYLAND_DISPLAY:-}' DISPLAY='${DISPLAY:-}'"

# ── 3. wait for the graphical session ────────────────────────────────────── #
wait_for_display() {
    local t=0 sock
    while [ "${t}" -lt "${WL_WAIT_S}" ]; do
        if [ -n "${WAYLAND_DISPLAY:-}" ] && [ -S "${RUNDIR}/${WAYLAND_DISPLAY}" ]; then
            say "compositor ready: ${RUNDIR}/${WAYLAND_DISPLAY}"; return 0
        fi
        [ -n "${DISPLAY:-}" ] && { say "X11 display ready: ${DISPLAY}"; return 0; }
        # Env not inherited? Look for any wayland socket on this seat.
        for sock in "${RUNDIR}"/wayland-*; do
            if [ -S "${sock}" ]; then
                export WAYLAND_DISPLAY="$(basename "${sock}")"
                say "compositor found by probe: ${WAYLAND_DISPLAY}"; return 0
            fi
        done
        sleep 1; t=$((t + 1))
    done
    say "WARNING: no compositor after ${WL_WAIT_S}s — starting anyway."
}

# Best effort only: the car must still race with no network. Firebase retries
# on its own, and the HUD works entirely offline.
wait_for_network() {
    local t=0
    while [ "${t}" -lt "${NET_WAIT_S}" ]; do
        ip route show default 2>/dev/null | grep -q . && {
            say "default route up after ${t}s."; return 0; }
        sleep 1; t=$((t + 1))
    done
    say "no default route after ${NET_WAIT_S}s — starting anyway."
}

wait_for_display
wait_for_network

if systemctl is-active --quiet gpsd.socket || systemctl is-active --quiet gpsd; then
    say "gpsd is active."
else
    say "WARNING: gpsd is NOT active — no GPS position and no lap trigger."
    say "         fix: sudo systemctl enable --now gpsd.socket gpsd"
fi

# ── 4. interpreter ───────────────────────────────────────────────────────── #
if [ -x "${VENV_PY}" ]; then
    PY="${VENV_PY}"
else
    PY="$(command -v python3 || true)"
    say "WARNING: ${VENV_PY} missing — falling back to ${PY}"
fi
[ -n "${PY}" ] || { say "FATAL: no python3 found."; exit 1; }
[ -f "${APP}" ] || { say "FATAL: ${APP} not found. Is REPO correct?"; exit 1; }

# Prefer native Wayland, fall back to XWayland if this PySide6 build ships no
# wayland platform plugin. Qt 6 accepts a ';'-separated fallback list.
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-wayland;xcb}"
export PYTHONUNBUFFERED=1

# Which output the HUD goes fullscreen on (see config.py: HUD_SCREEN_NAME).
# This car's driver panel is DSI-2 — `wlr-randr` names outputs, and
# ~/.config/kanshi/config pins DSI-2 to position 0,0 so it stays the primary
# screen even if kanshi re-evaluates. Override per-run, e.g. on a bench with
# only one screen attached: SOLARRACE_HUD_SCREEN=DSI-1 ./start_hud.sh
export SOLARRACE_HUD_SCREEN="${SOLARRACE_HUD_SCREEN:-DSI-2}"

# ── 5. supervise ─────────────────────────────────────────────────────────── #
backoff="${RESTART_MIN_S}"
while :; do
    [ -f "${STOP_FILE}" ] && { say "stop file present — staying down."; break; }

    # cd to the REPO ROOT. main.py opens
    # "SolarRace_OS/cloud/serviceAccountKey.json" as a RELATIVE path, so from
    # any other directory Firebase init fails and the pit gets no telemetry —
    # with the HUD itself looking perfectly fine.
    cd "${REPO}" || { say "FATAL: cannot cd ${REPO}"; break; }

    say "--- launching: ${PY} -u ${APP} (cwd=$(pwd)) ---"
    started=$(date +%s)
    "${PY}" -u "${APP}"
    rc=$?
    ran=$(( $(date +%s) - started ))
    say "--- HUD exited rc=${rc} after ${ran}s ---"

    # 42 = "an engineer asked for this" — Alt+F4, Ctrl+Shift+Q, or any other
    # deliberate close of the window. Anything else is a crash and comes back.
    [ "${rc}" -eq 42 ] && { say "clean quit requested."; break; }
    [ -f "${STOP_FILE}" ] && { say "stop file appeared — staying down."; break; }

    if [ "${ran}" -ge "${HEALTHY_RUN_S}" ]; then
        backoff="${RESTART_MIN_S}"      # it ran fine for a while; come back fast
    else
        backoff=$(( backoff * 2 ))      # crash loop: slow down, but never quit
        [ "${backoff}" -gt "${RESTART_MAX_S}" ] && backoff="${RESTART_MAX_S}"
    fi
    say "restarting in ${backoff}s..."
    sleep "${backoff}"
done

say "start_hud.sh finished."
