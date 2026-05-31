#!/usr/bin/env bash
# Server display stack: Xvfb (virtual screen) + x11vnc (localhost-only) + noVNC (web).
#
# Lets you watch and operate the headed Camoufox browser (login / CAPTCHA /
# human-assist popup) from a web browser, over an SSH tunnel — no VNC client,
# no public exposure. x11vnc binds 127.0.0.1 only; reach it via:
#   local$  ssh -L 6080:localhost:6080 118.196.19.109
#   local$  open http://localhost:6080/vnc.html
#
# Usage:
#   scripts/server_display.sh start   # start Xvfb+x11vnc+noVNC on :99 / 6080
#   scripts/server_display.sh stop
#   scripts/server_display.sh status
#
# After `start`, run a mission ON that display:
#   DISPLAY=:99 .venv/bin/python -m src.main auto <domain> "<req>" --no-gate
set -u

DISPLAY_NUM="${DISPLAY_NUM:-99}"
SCREEN="${SCREEN:-1280x900x24}"
VNC_PORT="${VNC_PORT:-5900}"
WEB_PORT="${WEB_PORT:-6080}"
NOVNC_DIR="${NOVNC_DIR:-/usr/share/novnc}"
RUN_DIR="/tmp/recon_display"
mkdir -p "$RUN_DIR"

# Pattern-based process tracking — robust against daemons that fork/re-exec
# (websockify does, so $! pidfiles go stale). Each service has a unique cmdline
# signature scoped to our DISPLAY_NUM / ports so we never touch other users'
# Xvfb/VNC on this shared box.
_pat() {
  case "$1" in
    xvfb)   echo "Xvfb :$DISPLAY_NUM " ;;
    x11vnc) echo "x11vnc -display :$DISPLAY_NUM " ;;
    novnc)  echo "websockify --web $NOVNC_DIR 127.0.0.1:$WEB_PORT" ;;
  esac
}
_alive() { pgrep -f "$(_pat "$1")" >/dev/null 2>&1; }

start() {
  if _alive xvfb; then echo "Xvfb already running (:$DISPLAY_NUM)"; else
    Xvfb ":$DISPLAY_NUM" -screen 0 "$SCREEN" >"$RUN_DIR/xvfb.log" 2>&1 &
    sleep 1; echo "Xvfb started on :$DISPLAY_NUM ($SCREEN)"
  fi
  # x11vnc — localhost only, no password (SSH tunnel is the auth boundary)
  if _alive x11vnc; then echo "x11vnc already running"; else
    x11vnc -display ":$DISPLAY_NUM" -rfbport "$VNC_PORT" -localhost -nopw \
      -forever -shared -quiet >"$RUN_DIR/x11vnc.log" 2>&1 &
    sleep 1; echo "x11vnc on 127.0.0.1:$VNC_PORT"
  fi
  # noVNC (websockify) — localhost only
  if _alive novnc; then echo "noVNC already running"; else
    websockify --web "$NOVNC_DIR" "127.0.0.1:$WEB_PORT" "localhost:$VNC_PORT" \
      >"$RUN_DIR/novnc.log" 2>&1 &
    sleep 1; echo "noVNC web on 127.0.0.1:$WEB_PORT"
  fi
  echo
  echo "Ready. From your local machine:"
  echo "  ssh -L $WEB_PORT:localhost:$WEB_PORT 118.196.19.109"
  echo "  then open  http://localhost:$WEB_PORT/vnc.html  (Connect)"
  echo "Run a mission on this display:"
  echo "  DISPLAY=:$DISPLAY_NUM .venv/bin/python -m src.main auto <domain> \"<req>\" --no-gate"
}

stop() {
  for svc in novnc x11vnc xvfb; do
    if _alive "$svc"; then pkill -f "$(_pat "$svc")" 2>/dev/null && echo "stopped $svc"; fi
  done
}

status() {
  for svc in xvfb x11vnc novnc; do
    if _alive "$svc"; then echo "$svc: RUNNING (pid $(pgrep -f "$(_pat "$svc")" | head -1))"; else echo "$svc: stopped"; fi
  done
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  restart) stop; sleep 1; start ;;
  *) echo "usage: $0 {start|stop|status|restart}"; exit 2 ;;
esac
