#!/bin/bash
# =============================================================================
# start_camera.sh — boot wrapper for the reverse camera on the second screen.
# Raspberry Pi 5 · Raspberry Pi OS Bookworm · Wayland (wayfire)
#
# Deliberately the same shape as start_hud.sh: single instance, its own rotating
# log, waits for the compositor, supervises with backoff, and can be told to
# stay down by a file that a reboot clears. Those behaviours were earned by the
# HUD and the camera needs every one of them — a reverse camera that dies
# silently is worse than no camera, because the driver keeps checking it.
#
# It runs a plain video player, NOT any of this project's Python. Nothing in the
# repo decodes video: the camera is a USB UVC device and mpv reads it directly.
#
# PREREQUISITE (once, on the Pi):   sudo apt install -y mpv v4l-utils
# =============================================================================
set -u

# ── configuration ────────────────────────────────────────────────────────── #
LOG_DIR="${HOME}/hud-logs"
LOG="${LOG_DIR}/camera.log"
MAX_LOG_BYTES=$((5 * 1024 * 1024))
KEEP=3
RUNDIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
LOCK_FILE="${RUNDIR}/solarrace-camera.lock"
STOP_FILE="${RUNDIR}/solarrace-camera.stop"
WL_WAIT_S=20
CAM_WAIT_S=30
RESTART_MIN_S=2
RESTART_MAX_S=20
HEALTHY_RUN_S=20

# Which screen the camera goes fullscreen on. 0 is the first output, 1 the
# second. If the camera and the HUD come up on the same screen, or swapped,
# change this to the other value — that is the whole fix, and it is the first
# thing to try. `wlr-randr` on the Pi lists the outputs in index order.
FS_SCREEN="${SOLARRACE_CAM_SCREEN:-1}"

# Capture format. MJPEG because a USB capture dongle that offers it hands over
# already-compressed frames, so the Pi does not spend a core on raw YUV at
# 30 fps. If mpv reports it cannot set the format, drop the input_format line
# from MPV_DEMUX_OPTS below and let it negotiate.
CAM_W="${SOLARRACE_CAM_W:-640}"
CAM_H="${SOLARRACE_CAM_H:-480}"
CAM_FPS="${SOLARRACE_CAM_FPS:-30}"
CAM_FORMAT="${SOLARRACE_CAM_FORMAT:-mjpeg}"

# ── 1. single instance ───────────────────────────────────────────────────── #
# Two players on one /dev/video node is not a race the kernel resolves nicely:
# the second one usually fails to open the device, which looks exactly like
# broken hardware.
exec 9>"${LOCK_FILE}" || exit 1
if ! flock -n 9; then
    echo "start_camera.sh: another instance holds ${LOCK_FILE} — exiting." >&2
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

rotate_logs
exec >>"${LOG}" 2>&1

say() { echo "[$(date '+%F %T')] $*"; }

say "=============================================================="
say "start_camera.sh starting. user=$(id -un)"
say "session: WAYLAND_DISPLAY='${WAYLAND_DISPLAY:-}' DISPLAY='${DISPLAY:-}'"
say "target screen: ${FS_SCREEN}  (override with SOLARRACE_CAM_SCREEN)"

# ── 3. wait for the graphical session ────────────────────────────────────── #
# Same probe as start_hud.sh, including the fallback for a session that did not
# inherit WAYLAND_DISPLAY.
wait_for_display() {
    local t=0 sock
    while [ "${t}" -lt "${WL_WAIT_S}" ]; do
        if [ -n "${WAYLAND_DISPLAY:-}" ] && [ -S "${RUNDIR}/${WAYLAND_DISPLAY}" ]; then
            say "compositor ready: ${RUNDIR}/${WAYLAND_DISPLAY}"; return 0
        fi
        [ -n "${DISPLAY:-}" ] && { say "X11 display ready: ${DISPLAY}"; return 0; }
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

# ── 4. find the camera ───────────────────────────────────────────────────── #
# A USB capture device usually registers TWO /dev/video nodes: one that captures
# and one that only carries metadata. Opening the wrong one gives "no frames"
# with no error, so the node is chosen by CAPABILITY, never by number.
#
# /dev/v4l/by-id/ is preferred where it exists because it is stable across
# reboots and across which USB port the camera is in. /dev/video0 is not: plug
# in a second UVC device, or boot with the GPS dongle enumerating first, and the
# numbers move.
find_camera() {
    local dev
    for dev in /dev/v4l/by-id/*-video-index0; do
        [ -e "${dev}" ] || continue
        if v4l2-ctl --device "${dev}" --all 2>/dev/null | grep -q "Video Capture"; then
            echo "${dev}"; return 0
        fi
    done
    for dev in /dev/video*; do
        [ -e "${dev}" ] || continue
        if v4l2-ctl --device "${dev}" --all 2>/dev/null | grep -q "Video Capture"; then
            echo "${dev}"; return 0
        fi
    done
    # v4l-utils absent? Fall back to the first node that exists at all, so a
    # missing package degrades to "probably works" rather than "no camera".
    for dev in /dev/video*; do
        [ -e "${dev}" ] && { echo "${dev}"; return 0; }
    done
    return 1
}

wait_for_camera() {
    local t=0 dev
    while [ "${t}" -lt "${CAM_WAIT_S}" ]; do
        dev="$(find_camera)" && { echo "${dev}"; return 0; }
        sleep 1; t=$((t + 1))
    done
    return 1
}

wait_for_display

command -v mpv >/dev/null || {
    say "FATAL: mpv is not installed. Fix: sudo apt install -y mpv v4l-utils"
    exit 1
}
command -v v4l2-ctl >/dev/null || \
    say "WARNING: v4l-utils missing — cannot pick the capture node by capability."

# ── 5. supervise ─────────────────────────────────────────────────────────── #
# --profile=low-latency and --untimed matter here more than picture quality: a
# reverse camera a second behind reality is actively dangerous. The trade is
# dropped frames under load, which is the right way round.
MPV_OPTS=(
    --profile=low-latency
    --untimed
    --no-audio
    --no-osc                    # no on-screen controls over the picture
    --no-input-default-bindings # a stray keypress must not pause the feed
    --cursor-autohide=always
    --no-border
    --fs
    "--fs-screen=${FS_SCREEN}"
    --keep-open=no
    --msg-level=all=warn
)

backoff="${RESTART_MIN_S}"
while :; do
    [ -f "${STOP_FILE}" ] && { say "stop file present — staying down."; break; }

    CAM_DEV="$(wait_for_camera)" || {
        say "no capture device after ${CAM_WAIT_S}s — retrying in ${RESTART_MAX_S}s."
        sleep "${RESTART_MAX_S}"
        continue
    }
    say "--- launching mpv on ${CAM_DEV} (screen ${FS_SCREEN}) ---"

    started=$(date +%s)
    mpv "${MPV_OPTS[@]}" \
        "--demuxer-lavf-o=input_format=${CAM_FORMAT},video_size=${CAM_W}x${CAM_H},framerate=${CAM_FPS}" \
        "av://v4l2:${CAM_DEV}"
    rc=$?
    ran=$(( $(date +%s) - started ))
    say "--- mpv exited rc=${rc} after ${ran}s ---"

    [ -f "${STOP_FILE}" ] && { say "stop file appeared — staying down."; break; }

    if [ "${ran}" -ge "${HEALTHY_RUN_S}" ]; then
        backoff="${RESTART_MIN_S}"
    else
        backoff=$(( backoff * 2 ))
        [ "${backoff}" -gt "${RESTART_MAX_S}" ] && backoff="${RESTART_MAX_S}"
    fi
    say "restarting in ${backoff}s..."
    sleep "${backoff}"
done

say "start_camera.sh finished."
