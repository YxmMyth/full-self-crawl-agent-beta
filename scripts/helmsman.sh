#!/usr/bin/env bash
# Helmsman operator-console process manager (server side).
#
# One command to bring the whole console up: display stack (Xvfb + x11vnc +
# noVNC) + the FastAPI app, in a tmux session that survives SSH disconnect.
# Idempotent — safe to run repeatedly.
#
#   scripts/helmsman.sh start      # ensure display stack + start Helmsman
#   scripts/helmsman.sh stop       # stop Helmsman (display stack left up)
#   scripts/helmsman.sh restart    # stop + start
#   scripts/helmsman.sh status     # show what's running
#   scripts/helmsman.sh logs       # tail the Helmsman log
#
# View from your laptop (any machine with SSH access to the box):
#   ssh -L 9090:127.0.0.1:8080 118.196.19.109
#   open http://localhost:9090/        (launch form)
#         http://localhost:9090/run    (live dashboard, after launching)
#
# Env overrides:
#   HELMSMAN_PORT   (default 8080)  — port 8000 is taken by another user, don't use it
#   DISPLAY_NUM     (default 99)    — the Xvfb display the mission's Camoufox renders to
set -u

APP_DIR="/mnt/data1/full-self-crawl-agent-beta"
PORT="${HELMSMAN_PORT:-8080}"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
SESSION="helmsman"
LOG="/tmp/helmsman.log"
PY="$APP_DIR/.venv/bin/python"

cd "$APP_DIR" 2>/dev/null || { echo "ERROR: $APP_DIR not found"; exit 1; }

_listening() { ss -ltn 2>/dev/null | grep -q ":$PORT "; }

start() {
  # 1) Display stack (idempotent — its own script no-ops if already up)
  bash scripts/server_display.sh start >/dev/null 2>&1 || true
  if ! pgrep -f "Xvfb :$DISPLAY_NUM " >/dev/null; then
    echo "WARN: Xvfb :$DISPLAY_NUM not running — VNC + headed browser won't render."
  fi

  # 2) Helmsman
  if _listening; then
    echo "Helmsman already listening on 127.0.0.1:$PORT"
  else
    # Clean a stale tmux session whose process already died
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    tmux new-session -d -s "$SESSION" \
      "cd '$APP_DIR' && DISPLAY=:$DISPLAY_NUM HELMSMAN_PORT=$PORT '$PY' -m web > '$LOG' 2>&1"
    # Wait up to ~12s for it to bind
    for _ in $(seq 1 24); do
      sleep 0.5
      _listening && break
    done
    if _listening; then
      echo "Helmsman started on 127.0.0.1:$PORT (DISPLAY=:$DISPLAY_NUM, tmux '$SESSION')"
    else
      echo "ERROR: Helmsman failed to bind $PORT in 12s. Last log lines:"
      tail -15 "$LOG" 2>/dev/null
      exit 1
    fi
  fi

  echo
  echo "View from your laptop:"
  echo "  ssh -L 9090:127.0.0.1:$PORT 118.196.19.109"
  echo "  open http://localhost:9090/"
}

stop() {
  tmux kill-session -t "$SESSION" 2>/dev/null && echo "stopped tmux '$SESSION'" || echo "no '$SESSION' tmux session"
  # Belt-and-suspenders: kill any orphan uvicorn for our module on our port
  pkill -f "HELMSMAN_PORT=$PORT.*-m web" 2>/dev/null || true
}

status() {
  echo "== Helmsman =="
  if _listening; then
    echo "  port $PORT: LISTENING  (healthz: $(curl -s -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:$PORT/healthz 2>/dev/null))"
  else
    echo "  port $PORT: DOWN"
  fi
  tmux has-session -t "$SESSION" 2>/dev/null && echo "  tmux '$SESSION': present" || echo "  tmux '$SESSION': absent"
  echo "== Display stack =="
  bash scripts/server_display.sh status 2>&1 | sed 's/^/  /'
  echo "== Active run =="
  curl -s --max-time 4 "http://127.0.0.1:$PORT/api/runs/active" 2>/dev/null \
    | "$PY" -c "import sys,json;d=json.load(sys.stdin);print('  active:',d['active'],'| phase:',d['phase'],'| run_id:',d['run_id'])" 2>/dev/null \
    || echo "  (console not reachable)"
}

case "${1:-}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)  status ;;
  logs)    tail -f "$LOG" ;;
  *) echo "usage: $0 {start|stop|restart|status|logs}"; exit 2 ;;
esac
