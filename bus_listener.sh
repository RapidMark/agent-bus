#!/bin/bash
# Persistent agent-bus listener.
# Polls every 30s, consumes messages, appends to log files.
#
# Output:
#   $LOG       — TSV summary (ts \t sender \t first_120_chars_of_msg)
#   $FULL      — full JSON, one message per line
#   $HEARTBEAT — ISO timestamp updated every poll (use to detect liveness)
#
# Configuration (env vars or defaults):
#   AGENT_BUS_URL      base URL of the bus (required)
#   AGENT_NAME         this agent's name (required, used as ?to=$AGENT_NAME)
#   AGENT_BUS_UA       User-Agent header (default: Mozilla/5.0)
#   AGENT_BUS_CHANNEL  channel for traffic isolation (default: "default").
#                      Only messages sent on the same channel are received.
#   LOG, FULL, HEARTBEAT, PID_FILE — output paths (defaults under /tmp)
#
# Start:  AGENT_BUS_URL=... AGENT_NAME=spark nohup ./bus_listener.sh \
#               > /tmp/bus_listener_main.log 2>&1 &
# Check:  tail -20 "$LOG"
# Stop:   pkill -f bus_listener.sh
#
# Differences vs. bus_recv.py:
#   - Polls (30s interval) instead of long-poll, so cheaper for idle channels
#   - Persists to disk instead of stdout, so detached daemons can run unattended
#   - Use bus_recv.py when piping to Claude Code's Monitor tool

set -u

: "${AGENT_BUS_URL:?AGENT_BUS_URL env var required}"
: "${AGENT_NAME:?AGENT_NAME env var required}"
UA="${AGENT_BUS_UA:-Mozilla/5.0}"
CHANNEL="${AGENT_BUS_CHANNEL:-default}"
URL="${AGENT_BUS_URL%/}/recv?to=${AGENT_NAME}&channel=${CHANNEL}&max=50"
LOG="${LOG:-/tmp/bus_listener_${AGENT_NAME}.log}"
FULL="${FULL:-/tmp/bus_listener_${AGENT_NAME}_full.jsonl}"
HEARTBEAT="${HEARTBEAT:-/tmp/bus_listener_${AGENT_NAME}_heartbeat}"
PID_FILE="${PID_FILE:-/tmp/bus_listener_${AGENT_NAME}.pid}"

echo $$ > "$PID_FILE"

while true; do
  date -u +%Y-%m-%dT%H:%M:%SZ > "$HEARTBEAT"
  resp=$(curl -sS -A "$UA" --max-time 15 "$URL" 2>/dev/null)
  if [ -n "$resp" ]; then
    echo "$resp" | jq -c '.messages[]?' 2>/dev/null >> "$FULL"
    echo "$resp" | jq -r '.messages[]? | "\(.ts // .timestamp // 0)\t\(.from // "?")\t\((.msg // .body // "") | gsub("\n"; " | ")[:120])"' 2>/dev/null >> "$LOG"
  fi
  sleep 30
done
