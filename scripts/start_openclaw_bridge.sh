#!/usr/bin/env bash
# Start OpenClaw Task Bridge on 127.0.0.1:8791
set -euo pipefail

# [2026-11-04 Q860] 可用 LL_OPENCLAW_DIR 覆盖，默认保留原值。
OPENCLAW_DIR="${LL_OPENCLAW_DIR:-$HOME/.openclaw/workspace}"
PID_FILE="/tmp/openclaw_task_bridge.pid"
LOG_FILE="/tmp/openclaw_task_bridge.log"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "OpenClaw Task Bridge is already running (PID: $(cat "$PID_FILE"))"
    exit 0
fi

echo "Starting OpenClaw Task Bridge on 127.0.0.1:8791..."
nohup node "$OPENCLAW_DIR/scripts/openclaw-task-bridge.js" --host=127.0.0.1 --port=8791 > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
sleep 1

if kill -0 "$NEW_PID" 2>/dev/null; then
    echo "OpenClaw Task Bridge started successfully (PID: $NEW_PID)"
else
    echo "Failed to start OpenClaw Task Bridge. Check $LOG_FILE"
    exit 1
fi
