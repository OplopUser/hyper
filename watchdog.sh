#!/bin/bash
# watchdog.sh — Keeps the trading bot and dashboard alive.
# Usage: nohup ./watchdog.sh &
#
# Interpreter: $PYTHON if set, else .venv/ or venv/ next to this script,
# else python3 from PATH. Example: PYTHON=/opt/py311/bin/python ./watchdog.sh

PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "${PYTHON:-}" ]; then
    for candidate in "$PROJ_DIR/.venv/bin/python" "$PROJ_DIR/venv/bin/python"; do
        if [ -x "$candidate" ]; then
            PYTHON="$candidate"
            break
        fi
    done
fi
PYTHON="${PYTHON:-$(command -v python3)}"
if [ -z "$PYTHON" ] || ! "$PYTHON" -c "import sys" >/dev/null 2>&1; then
    echo "watchdog.sh: no usable Python found (set PYTHON=/path/to/python)" >&2
    exit 1
fi
BOT_LOG="/tmp/hyper_bot.log"
DASH_LOG="/tmp/hyper_dash.log"
BOT_PID_FILE="/tmp/hyper_bot.pid"
DASH_PID_FILE="/tmp/hyper_dash.pid"
CHECK_INTERVAL=30  # seconds

cd "$PROJ_DIR"

is_running() {
    local pid_file="$1"
    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        rm -f "$pid_file"
    fi
    return 1
}

start_bot() {
    nohup "$PYTHON" main.py >> "$BOT_LOG" 2>&1 &
    echo $! > "$BOT_PID_FILE"
    disown
    echo "$(date '+%Y-%m-%d %H:%M:%S')  Watchdog: bot started (PID $!)" >> "$BOT_LOG"
}

start_dash() {
    nohup "$PYTHON" dashboard.py >> "$DASH_LOG" 2>&1 &
    echo $! > "$DASH_PID_FILE"
    disown
    echo "$(date '+%Y-%m-%d %H:%M:%S')  Watchdog: dashboard started (PID $!)" >> "$DASH_LOG"
}

# Initial start
start_bot
start_dash

while true; do
    sleep "$CHECK_INTERVAL"

    if ! is_running "$BOT_PID_FILE"; then
        echo "$(date '+%Y-%m-%d %H:%M:%S')  Watchdog: bot is down — restarting" >> "$BOT_LOG"
        start_bot
    fi

    if ! is_running "$DASH_PID_FILE"; then
        echo "$(date '+%Y-%m-%d %H:%M:%S')  Watchdog: dashboard is down — restarting" >> "$DASH_LOG"
        start_dash
    fi
done
