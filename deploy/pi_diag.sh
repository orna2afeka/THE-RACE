#!/bin/bash
# =============================================================================
# pi_diag.sh — a read-only snapshot of the Pi while the HUD is running.
#
#   bash deploy/pi_diag.sh            # everything except thread sampling
#   bash deploy/pi_diag.sh --spy      # also sample the HUD's Python stacks
#
# WHY. The driver's screen sometimes freezes and then jumps. The code review
# and the pit's stored data (tools/car_stall_report.py) say the CAN worker
# thread is being BLOCKED, most likely inside the synchronous Firebase upload
# it performs on main. Only the Pi itself can show the rest of the picture:
# thermal or under-voltage throttling, whether the HUD process is pinned to
# one core, what the reverse-camera player costs, whether SocketCAN dropped
# frames while nobody read the socket, and — with --spy — the thread caught
# in the act.
#
# It CHANGES NOTHING: no restart, no config, no writes outside ~/hud-logs.
# Safe to run during a drive. --spy attaches py-spy, which pauses the HUD for
# a few milliseconds per dump (10 dumps, one per second); that is the one
# thing here with any effect on the car, so it is opt-in. --spy may also pip
# install py-spy into the venv the first time, which needs the uplink.
#
# Everything is printed AND saved to ~/hud-logs/pi_diag_<date>.txt. Sections
# degrade one by one when a tool is missing; the script never exits early.
# =============================================================================
set -u

# ── configuration ────────────────────────────────────────────────────────── #
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo ROOT, like start_hud.sh
APP_SUFFIX="SolarRace_OS/main.py"
VENV="${REPO}/.venv"
LOG_DIR="${HOME}/hud-logs"
HUD_LOG="${LOG_DIR}/hud.log"                               # where start_hud.sh sends the HUD's output
SPY=0
SPY_DUMPS=10
TOP_WINDOW_S=5

usage() {
    sed -n '3,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
    case "${arg}" in
        --spy)      SPY=1 ;;
        -h|--help)  usage; exit 0 ;;
        *)          echo "pi_diag.sh: unknown option '${arg}' (try --help)" >&2; exit 2 ;;
    esac
done

# ── output: screen and file ──────────────────────────────────────────────── #
mkdir -p "${LOG_DIR}"
REPORT="${LOG_DIR}/pi_diag_$(date '+%Y%m%d_%H%M%S').txt"
exec > >(tee -a "${REPORT}") 2>&1
TEE_PID=$!

hr()   { printf '\n== %s ==\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
run()  {                       # echo the command, run it, never abort the report
    local rc
    echo "\$ $*"
    "$@" 2>&1 | sed 's/^/  /'
    rc=${PIPESTATUS[0]}
    [ "${rc}" -ne 0 ] && echo "  (exit ${rc})"
    return 0
}

# ── finding the HUD process (copied from stop_hud.sh — same reasoning) ──── #
# A plain `pgrep -f SolarRace_OS/main.py` matches any shell whose command line
# merely CONTAINS that text, including the terminal this runs from. Both
# matchers require the path to be a real argv element and check the process
# name, so interactive shells never match.
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

# The second sample of a two-sample `top` is the only honest one: the first
# reports averages since boot (or since the process started), not "now".
top_last_block() {           # stdin = `top -b -n 2 ...` output; prints the last block
    awk '/^top - /{blk=""; next} {blk=blk $0 "\n"} END{printf "%s", blk}'
}

decode_throttled() {         # $1 = the 0x... value from vcgencmd get_throttled
    local v="$1" n
    [[ "${v}" =~ ^0x[0-9a-fA-F]+$ ]] || { echo "  (unreadable value '${v}')"; return; }
    n=$((v))
    if [ "${n}" -eq 0 ]; then
        echo "  0x0 — no under-voltage, no frequency cap, no throttling, now or since boot"
        return
    fi
    (( n & 0x1 ))     && echo "  NOW: under-voltage (the supply is not holding 5 V)"
    (( n & 0x2 ))     && echo "  NOW: ARM frequency capped"
    (( n & 0x4 ))     && echo "  NOW: throttled"
    (( n & 0x8 ))     && echo "  NOW: soft temperature limit active"
    (( n & 0x10000 )) && echo "  since boot: under-voltage has occurred"
    (( n & 0x20000 )) && echo "  since boot: ARM frequency capping has occurred"
    (( n & 0x40000 )) && echo "  since boot: throttling has occurred"
    (( n & 0x80000 )) && echo "  since boot: soft temperature limit has been reached"
}

count_in_log() {             # $1 = fixed string; prints 0 when absent or unreadable
    local n
    n="$(grep -cF -- "$1" "${HUD_LOG}" 2>/dev/null)"
    echo "${n:-0}"
}

# ── 1. identity ──────────────────────────────────────────────────────────── #
hr "pi_diag.sh  $(date '+%F %T')"
echo "host: $(hostname 2>/dev/null || echo ?)   user: $(id -un)   report: ${REPORT}"
[ -r /proc/device-tree/model ] && echo "model: $(tr -d '\0' < /proc/device-tree/model)"
[ -r /etc/os-release ] && echo "os: $(. /etc/os-release; echo "${PRETTY_NAME:-?}")"
echo "kernel: $(uname -r 2>/dev/null || echo ?)   cores: $(nproc 2>/dev/null || echo ?)"
have uptime && run uptime
echo "repo: ${REPO}"
if have git && [ -d "${REPO}/.git" ]; then
    echo "branch: $(git -C "${REPO}" rev-parse --abbrev-ref HEAD 2>&1)   head: $(git -C "${REPO}" log -1 --oneline 2>&1)"
    mods="$(git -C "${REPO}" status --short 2>&1)"
    if [ -n "${mods}" ]; then
        echo "local edits on the Pi (not in git):"
        echo "${mods}" | sed 's/^/  /'
    else
        echo "working tree clean"
    fi
fi

# ── 2. thermal / power ───────────────────────────────────────────────────── #
hr "thermal / power"
if have vcgencmd; then
    run vcgencmd measure_temp
    run vcgencmd measure_clock arm
    raw="$(vcgencmd get_throttled 2>/dev/null | sed 's/.*=//')"
    echo "get_throttled: ${raw:-?}"
    decode_throttled "${raw:-}"
else
    echo "vcgencmd not found — not a Raspberry Pi, or the firmware tools are missing"
fi
if [ -r /sys/class/thermal/thermal_zone0/temp ]; then
    echo "thermal_zone0: $(( $(cat /sys/class/thermal/thermal_zone0/temp) / 1000 )) C"
fi

# ── 3. system load ───────────────────────────────────────────────────────── #
hr "system load (a ${TOP_WINDOW_S} s window; %CPU is of ONE core)"
[ -r /proc/loadavg ] && echo "loadavg: $(cat /proc/loadavg)"
have free && run free -m
if have top; then
    echo "\$ top -b -n 2 -d ${TOP_WINDOW_S}   (second sample, top processes)"
    top -b -n 2 -d "${TOP_WINDOW_S}" 2>/dev/null | top_last_block | head -22 | sed 's/^/  /'
else
    echo "top not found"
fi

# ── 4. the HUD process ───────────────────────────────────────────────────── #
hr "HUD process"
HUD_PID=""
mapfile -t apps < <(find_pids "${APP_SUFFIX}" "python")
if [ "${#apps[@]}" -eq 0 ]; then
    echo "no HUD process running (nothing has ${APP_SUFFIX} in its argv)"
else
    HUD_PID="${apps[0]}"
    [ "${#apps[@]}" -gt 1 ] && echo "WARNING: ${#apps[@]} HUD processes (${apps[*]}) — reporting on ${HUD_PID}"
    echo "pid: ${HUD_PID}   started: $(ps -o lstart= -p "${HUD_PID}" 2>/dev/null)   cpu time so far: $(ps -o time= -p "${HUD_PID}" 2>/dev/null)"
    grep -E '^(State|Threads|VmRSS|VmSwap|voluntary_ctxt_switches|nonvoluntary_ctxt_switches)' "/proc/${HUD_PID}/status" 2>/dev/null | sed 's/^/  /'
    echo "threads (tid  name — Qt names its own; a bare python3 is an unnamed Python thread):"
    for t in /proc/"${HUD_PID}"/task/*; do
        printf '  %s  %s\n' "$(basename "${t}")" "$(cat "${t}/comm" 2>/dev/null)"
    done
    if have top; then
        echo "\$ top -H -b -n 2 -d ${TOP_WINDOW_S} -p ${HUD_PID}   (per-thread %CPU over ${TOP_WINDOW_S} s)"
        block="$(top -H -b -n 2 -d "${TOP_WINDOW_S}" -p "${HUD_PID}" 2>/dev/null | top_last_block)"
        echo "${block}" | sed 's/^/  /'
        total="$(echo "${block}" | awk 'NR>1 && $9 ~ /^[0-9.]+$/ {s+=$9} END{printf "%.0f", s}')"
        echo "process total: ~${total} % of one core."
        echo "  Reading: the CAN thread and the Qt GUI thread share Python's GIL, so this"
        echo "  process can never usefully exceed ~100 %. Near 100 % = CPU-bound. Low %"
        echo "  while the screen still freezes = blocked on I/O, not busy."
    fi
fi

# ── 5. the reverse camera ────────────────────────────────────────────────── #
hr "reverse camera (mpv)"
mpv_pid="$(pgrep -x mpv 2>/dev/null | head -1)"
if [ -z "${mpv_pid}" ]; then
    echo "no mpv process (no camera on this car, or it is not running)"
elif have top; then
    top -b -n 2 -d 2 -p "${mpv_pid}" 2>/dev/null | top_last_block | tail -n 2 | sed 's/^/  /'
    echo "  (a separate process: it costs a core but does not compete for the HUD's GIL)"
else
    echo "pid ${mpv_pid}"
fi

# ── 6. CAN ───────────────────────────────────────────────────────────────── #
hr "CAN interfaces"
if have ip; then
    for ch in can0 can1; do
        echo "\$ ip -s -d link show ${ch}"
        ip -s -d link show "${ch}" 2>&1 | sed 's/^/  /'
    done
    echo "Reading: RX 'overrun' counts frames the kernel dropped because the socket"
    echo "  was not read in time — i.e. the CAN thread was not draining. 'bus-off' or"
    echo "  rising 'error' counters are wiring/bitrate trouble, a different problem."
else
    echo "ip not found"
fi
if [ -r /proc/net/can/stats ]; then
    echo "\$ cat /proc/net/can/stats   (live rates: 'frames rx per sec' is the real load)"
    sed 's/^/  /' /proc/net/can/stats
fi

# ── 7. the HUD log ───────────────────────────────────────────────────────── #
hr "HUD log  ${HUD_LOG}"
if [ -f "${HUD_LOG}" ]; then
    echo "size: $(du -h "${HUD_LOG}" 2>/dev/null | cut -f1)   lines: $(wc -l < "${HUD_LOG}")   last write: $(date -r "${HUD_LOG}" '+%F %T' 2>/dev/null)"
    echo "(the HUD's own lines carry no timestamps — only the wrapper's do — so these are counts)"
    ne="$(count_in_log '[Network Error]')"
    cs="$(count_in_log 'Cloud Sync Payload')"
    hb="$(count_in_log '[Heartbeat]')"
    re="$(count_in_log 'CAN read error')"
    si="$(count_in_log 'CAN silence')"
    lv="$(count_in_log 'Live CAN traffic detected')"
    printf '  %-34s %s\n' "[Network Error] (upload failed)" "${ne}"
    printf '  %-34s %s\n' "Cloud Sync Payload (1/s decoding)" "${cs}"
    printf '  %-34s %s\n' "[Heartbeat] errors" "${hb}"
    printf '  %-34s %s\n' "CAN read error" "${re}"
    printf '  %-34s %s\n' "CAN silence (bus went quiet)" "${si}"
    printf '  %-34s %s\n' "Live CAN traffic detected" "${lv}"
    if [ "${cs}" -gt 0 ]; then
        echo "  ~${cs} s of frame decoding in this log; upload failures per decoding minute: $(awk -v a="${ne}" -v b="${cs}" 'BEGIN{printf "%.2f", a/(b/60)}')"
    fi
    echo "last 15 [Network Error] lines:"
    grep -F '[Network Error]' "${HUD_LOG}" 2>/dev/null | tail -15 | cut -c1-240 | sed 's/^/  /'
    echo "last 8 wrapper lines (start_hud.sh, timestamped):"
    grep -E '^\[[0-9]{4}-[0-9]{2}-[0-9]{2} ' "${HUD_LOG}" 2>/dev/null | tail -8 | sed 's/^/  /'
else
    echo "no log at ${HUD_LOG}"
fi

# ── 8. uplink ────────────────────────────────────────────────────────────── #
hr "uplink"
have ip && run ip route show default
have ping && run ping -c 3 -W 2 8.8.8.8
if have mmcli; then
    echo "\$ mmcli -m any   (modem state / signal / access technology)"
    mmcli -m any 2>&1 | grep -iE 'signal quality|access tech|state|operator name|power state' | sed 's/^/  /'
fi
# The database host is the literal db_url in SolarRace_OS/main.py; read it from
# there so this never drifts from what the app actually talks to.
db_host="$(grep -oE 'https://[A-Za-z0-9.-]+firebasedatabase\.app' "${REPO}/SolarRace_OS/main.py" 2>/dev/null | head -1)"
if [ -n "${db_host}" ] && have curl; then
    echo "HTTPS round trip to ${db_host}"
    echo "  (unauthenticated, so HTTP 401 is the expected answer — the timings are the point;"
    echo "   a healthy write measured ~40 ms at the bench, the app gives up after 8 s per attempt)"
    for i in 1 2 3; do
        curl -s -o /dev/null --max-time 10 \
            -w "  connect %{time_connect}s  tls %{time_appconnect}s  first-byte %{time_starttransfer}s  total %{time_total}s  http %{http_code}\n" \
            "${db_host}/.json?shallow=true" || echo "  attempt ${i}: curl exit $? (timed out or no route)"
    done
else
    echo "curl not found, or no firebasedatabase.app URL in SolarRace_OS/main.py"
fi

# ── 9. py-spy: the threads caught in the act (opt-in) ────────────────────── #
hr "py-spy thread sampling"
if [ "${SPY}" -ne 1 ]; then
    echo "skipped — run with --spy to take ${SPY_DUMPS} stack dumps of the HUD, one per second."
    echo "Each dump pauses the HUD for a few milliseconds; that is why it is opt-in."
elif [ -z "${HUD_PID}" ]; then
    echo "no HUD process to sample"
else
    PYSPY=""
    if [ -x "${VENV}/bin/py-spy" ]; then
        PYSPY="${VENV}/bin/py-spy"
    elif have py-spy; then
        PYSPY="$(command -v py-spy)"
    elif [ -x "${VENV}/bin/pip" ]; then
        echo "py-spy not installed — trying: ${VENV}/bin/pip install py-spy  (needs the uplink)"
        "${VENV}/bin/pip" install -q py-spy 2>&1 | tail -3 | sed 's/^/  /'
        [ -x "${VENV}/bin/py-spy" ] && PYSPY="${VENV}/bin/py-spy"
    fi
    if [ -z "${PYSPY}" ]; then
        echo "py-spy unavailable (pip could not install it) — skipped"
    else
        # Attaching to another process needs ptrace rights: root, or
        # kernel.yama.ptrace_scope=0. Try passwordless sudo; fall back to plain.
        SUDO=""
        if [ "$(id -u)" -ne 0 ]; then
            if sudo -n true 2>/dev/null; then
                SUDO="sudo -n"
            else
                echo "sudo wants a password in this shell — trying without (works only if ptrace_scope is 0)"
            fi
        fi
        DUMPDIR="$(mktemp -d)"
        ok=0
        for ((i = 1; i <= SPY_DUMPS; i++)); do
            if ${SUDO} "${PYSPY}" dump --pid "${HUD_PID}" > "${DUMPDIR}/${i}.txt" 2>&1; then
                ok=$((ok + 1))
            else
                echo "dump ${i} failed: $(head -2 "${DUMPDIR}/${i}.txt" | tr '\n' ' ' | cut -c1-200)"
            fi
            sleep 1
        done
        echo "dumps taken: ${ok} / ${SPY_DUMPS}"
        if [ "${ok}" -gt 0 ]; then
            # One line per thread per dump, classified by what its Python stack
            # is doing. The upload markers are functions ONLY the CAN worker
            # calls, so the three firebase-admin listener threads (which also
            # sit in requests/ssl forever, by design) never count as blocked.
            TALLY_AWK='
function flush() {
    if (hdr == "") return
    st = (hdr ~ /\(active\)/) ? "active" : "idle"
    k = "other thread"
    if (body ~ /push_telemetry_to_cloud|_push_directly|push_public_snapshot|ack_lap_command|ack_strategy|_send_batch_to_firebase|_publish_heartbeat/)
        k = "CAN thread: BLOCKED inside a Firebase upload"
    else if (body ~ /_decode_message|recv \(/ && body ~ /main\.py/)
        k = "CAN thread: reading / decoding frames"
    else if (body ~ /_poll_bms|_request_gpio_report/)
        k = "CAN thread: bus.send (BMS poll / GPIO request)"
    else if (body ~ /subprocess\.py/ && body ~ /main\.py|config\.py/)
        k = "CAN thread: subprocess (ip link show)"
    else if (body ~ /run \([^)]*main\.py/)
        k = "CAN thread: loop helpers / idle sleep"
    else if (body ~ /paintEvent|_fit_font|_fit_strip_text|sizeHint|driver_dash_v2\.py/)
        k = "GUI thread: painting / layout / slots"
    else if (body ~ /<module> \([^)]*main\.py/)
        k = "GUI thread: Qt event loop (idle, or inside C++ Qt)"
    else if (body ~ /gps_reader\.py/)   k = "gpsd reader thread"
    else if (body ~ /net_monitor\.py/)  k = "net monitor thread"
    else if (body ~ /firebase_admin|sseclient|listen/) k = "firebase listener thread"
    printf "%s [%s]\n", k, st
    hdr = ""; body = ""
}
/^Thread / { flush(); hdr = $0; next }
{ body = body $0 "\n" }
END { flush() }
'
            echo "thread states across all ${ok} dumps (count  what the thread was doing [py-spy state]):"
            for f in "${DUMPDIR}"/*.txt; do awk "${TALLY_AWK}" "${f}"; done | sort | uniq -c | sort -rn | sed 's/^/  /'
            blocked="$(grep -lE 'push_telemetry_to_cloud|_push_directly|push_public_snapshot|_send_batch_to_firebase' "${DUMPDIR}"/*.txt 2>/dev/null | wc -l)"
            painting="$(grep -lE 'paintEvent|_fit_font|_fit_strip_text|sizeHint' "${DUMPDIR}"/*.txt 2>/dev/null | wc -l)"
            echo "CAN thread inside a Firebase upload in ${blocked} of ${ok} dumps; GUI thread painting/laying out in ${painting} of ${ok}."
            echo "  Reading: with a healthy link an upload takes ~80 ms per 0.5 s, so ~1-2 dumps"
            echo "  in 10 is normal. Most dumps = the thread lives in the upload = the freeze."
            echo "first dump, verbatim:"
            sed 's/^/  /' "${DUMPDIR}/1.txt"
        fi
        rm -rf "${DUMPDIR}"
    fi
fi

# ── done ─────────────────────────────────────────────────────────────────── #
hr "done"
echo "report saved: ${REPORT}"
exec >&- 2>&-
wait "${TEE_PID}" 2>/dev/null
exit 0
