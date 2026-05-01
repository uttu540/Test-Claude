#!/usr/bin/env bash
# start_paper.sh — kill any running bot session then start fresh in paper mode
set -euo pipefail

BOT_DIR="/Users/utkarshagrawal/Documents/Test Claude"
LOG_FILE="$BOT_DIR/logs/paper_$(date +%Y-%m-%d).log"
PID_FILE="$BOT_DIR/logs/paper.pid"

mkdir -p "$BOT_DIR/logs"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === start_paper.sh fired ===" >> "$LOG_FILE"

# ── Kill existing session ──────────────────────────────────────────────────────
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Killing existing session (PID $OLD_PID)" >> "$LOG_FILE"
        # Kill honcho and all its children (bot + uvicorn)
        pkill -P "$OLD_PID" 2>/dev/null || true
        kill "$OLD_PID" 2>/dev/null || true
        sleep 3
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Stale PID file ($OLD_PID not running) — cleaning up" >> "$LOG_FILE"
    fi
    rm -f "$PID_FILE"
fi

# Also kill any stray main.py / uvicorn processes left over from a crash
pkill -f "venv/bin/python main.py" 2>/dev/null || true
pkill -f "uvicorn api.main:app"    2>/dev/null || true
sleep 1

# ── Start fresh in a visible Terminal window ──────────────────────────────────
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Opening Terminal window for PAPER mode" >> "$LOG_FILE"

# Open a new Terminal window — output goes to BOTH terminal (visible) and log file (persistent)
osascript <<APPLESCRIPT
tell application "Terminal"
    activate
    set newTab to do script "cd \"$BOT_DIR\" && APP_ENV=paper venv/bin/honcho start 2>&1 | tee -a \"$LOG_FILE\""
    set custom title of newTab to "Trading Bot — PAPER"
end tell
APPLESCRIPT

# Give Terminal a moment to spawn the process, then find and save its PID
sleep 2
NEW_PID=$(pgrep -f "honcho start" | head -1)
if [ -n "$NEW_PID" ]; then
    echo "$NEW_PID" > "$PID_FILE"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Started in Terminal (PID $NEW_PID)" >> "$LOG_FILE"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Warning: could not detect PID after Terminal launch" >> "$LOG_FILE"
fi
