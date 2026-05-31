#!/usr/bin/env bash
# Run a mission on the server's virtual display (:99), inside tmux so it
# survives SSH disconnect. Watch it live via the noVNC web page (see
# scripts/server_display.sh for the tunnel).
#
# Usage:
#   scripts/server_run.sh <domain> "<requirement>" [--no-gate]
# Example:
#   scripts/server_run.sh codepen.io "采集 three.js 相关 pen 50 个..." --no-gate
#
# Reattach / watch logs:
#   tmux attach -t mission
#   tail -f /mnt/data1/full-self-crawl-agent-beta/run_*.log
set -eu

APP_DIR="/mnt/data1/full-self-crawl-agent-beta"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
SESSION="${SESSION:-mission}"

cd "$APP_DIR"

# Ensure the display stack is up (idempotent)
bash scripts/server_display.sh start >/dev/null 2>&1 || true
if ! pgrep -f "Xvfb :$DISPLAY_NUM " >/dev/null; then
  echo "ERROR: Xvfb :$DISPLAY_NUM not running — start it: scripts/server_display.sh start" >&2
  exit 1
fi

domain="${1:?usage: server_run.sh <domain> \"<requirement>\" [--no-gate]}"
requirement="${2:?missing requirement}"
shift 2
extra="$*"

log="$APP_DIR/run_$(date +%Y%m%d-%H%M%S).log"
echo "Launching mission in tmux '$SESSION' on DISPLAY=:$DISPLAY_NUM"
echo "Log: $log"

tmux new-session -d -s "$SESSION" \
  "cd '$APP_DIR' && DISPLAY=:$DISPLAY_NUM .venv/bin/python -m src.main auto '$domain' '$requirement' $extra > '$log' 2>&1; echo EXIT=\$? >> '$log'"

echo "Started. Watch: tail -f '$log'  |  attach: tmux attach -t $SESSION"
echo "See the browser: tunnel 6080 and open http://localhost:6080/vnc.html"
