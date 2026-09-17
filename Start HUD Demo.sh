#!/bin/bash
# =============================================================================
# Start HUD Demo.sh — the real driver HUD on the Pi, driven by a fake car.
#
# The Pi twin of "Start HUD Demo.bat". No CAN adapter, no GPS, no Firebase:
# tools/hud_sim.py opens the same RacingDashboard the car runs and feeds every
# screen (DS001-DS004) plus a tour of the real hazards.
#
# WHAT IS DIFFERENT ON THE PI: the regen brake light is REAL. The simulator
# drives modules.regen_light.RegenLight on GPIO 17 from the fake car's motor
# power, so braking into a corner lights the bench lamp with the same
# thresholds, the same -50/-20 W hysteresis and the same minimum-on hold the
# car uses. Read the electrical note at the top of
# SolarRace_OS/modules/regen_light.py BEFORE connecting anything: the pin
# drives a MOSFET or an SSR, never a lamp.
#
#   ./"Start HUD Demo.sh"                 # windowed, hazard tour on
#   ./"Start HUD Demo.sh" --fullscreen    # as it runs in the car
#   ./"Start HUD Demo.sh" --speed 5       # five simulated seconds per real one
#   ./"Start HUD Demo.sh" --no-regen-light  # leave GPIO alone
#   ./"Start HUD Demo.sh" --regen-pin 27    # lamp wired to another pin
#
# Keys: R hold regen (lamp stays lit for a wiring check) · H next hazard
#       X clear hazard · M pit message · N clear · T turn · P pause
#       Alt+F4 / Ctrl+Shift+Q quit
#
# RUN IT FROM THE DESKTOP SESSION, not a bare SSH shell: it is a Qt window and
# needs the compositor, exactly like deploy/start_hud.sh. Over SSH, either use
# `ssh -X`, or set WAYLAND_DISPLAY/XDG_RUNTIME_DIR to the desktop session's.
#
# STOP THE REAL HUD FIRST if it is running (deploy/stop_hud.sh). Two HUDs mean
# two claims on GPIO 17: the second one loses, prints "GPIO 17 unavailable" and
# runs with nothing wired — the demo looks fine and the lamp never moves.
#
# Safe during a session: the simulator never touches Firebase or telemetry.db.
# =============================================================================
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${REPO}" || exit 1

# ── interpreter ──────────────────────────────────────────────────────────── #
# The venv first — it is what deploy/start_hud.sh runs the car with, so the
# demo is exercising the same PySide6 and the same gpiozero the race uses.
# Each candidate has to actually import PySide6: a Pi with a system python3 and
# no PySide6 would otherwise open a console saying "No module named PySide6".
PY=""
for candidate in "${REPO}/.venv/bin/python3" python3 python; do
    command -v "${candidate}" >/dev/null 2>&1 || [ -x "${candidate}" ] || continue
    if "${candidate}" -c "import PySide6" >/dev/null 2>&1; then
        PY="${candidate}"
        break
    fi
done

if [ "${1:-}" = "--which" ]; then
    echo "${PY:-none}"
    exit 0
fi

if [ -z "${PY}" ]; then
    cat >&2 <<'MSG'

  No Python on this Pi can import PySide6, which the HUD needs.

  Install it into the repo's venv, which is what the car uses:
      .venv/bin/python3 -m pip install PySide6

  then run this again.

MSG
    exit 1
fi

# ── GPIO ─────────────────────────────────────────────────────────────────── #
# Only a warning: the simulator runs fine with nothing driven, and says so on
# its own startup line. This just explains it before the window covers the
# console.
if ! "${PY}" -c "import gpiozero" >/dev/null 2>&1; then
    echo "NOTE: gpiozero is not installed for ${PY} — the brake-light logic will"
    echo "      run and show on screen, but no pin will be driven."
    echo "      Install it with: ${PY} -m pip install gpiozero lgpio"
fi

# Same platform preference as the car: native Wayland, XWayland as a fallback
# for a PySide6 build with no wayland plugin.
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-wayland;xcb}"
export PYTHONUNBUFFERED=1

echo "Starting the HUD demo with: ${PY}"
echo "Keys: R hold regen · H next hazard · X clear · M pit message · N clear · T turn · P pause"
exec "${PY}" tools/hud_sim.py "$@"
