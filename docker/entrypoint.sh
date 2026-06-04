#!/usr/bin/env bash
# Per-run container entrypoint (option B).
#
# Brings up THIS container's own display stack — Xvfb + x11vnc + websockify —
# then execs the mission passed as arguments. Mirrors scripts/server_display.sh
# but scoped to one disposable container, so the display number and ports never
# collide with the host or with other concurrent runs.
set -euo pipefail

DISPLAY_NUM="${DISPLAY#:}"                       # ":99" -> "99"
SCREEN_GEOMETRY="${SCREEN_GEOMETRY:-1280x900x24}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"

log() { echo "[entrypoint] $*" >&2; }

# 1) Virtual X server ─────────────────────────────────────────────────────────
log "starting Xvfb on :${DISPLAY_NUM} (${SCREEN_GEOMETRY})"
Xvfb ":${DISPLAY_NUM}" -screen 0 "${SCREEN_GEOMETRY}" -nolisten tcp &
# Wait for the X socket, then prove the display answers (fail loud, not silent).
for _ in $(seq 1 50); do
    [ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ] && break
    sleep 0.1
done
if ! xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
    log "FATAL: X server ${DISPLAY} did not come up"
    exit 1
fi
log "X server up"

# 2) VNC server — container-internal localhost only ───────────────────────────
log "starting x11vnc on 127.0.0.1:${VNC_PORT}"
x11vnc -display "${DISPLAY}" -rfbport "${VNC_PORT}" -localhost -nopw \
       -forever -shared -quiet -bg

# 3) noVNC (websockify) ───────────────────────────────────────────────────────
#    Bind 0.0.0.0 INSIDE the container so the supervisor's published
#    `-p 127.0.0.1:<hostport>:6080` reaches it. The container boundary is the
#    isolation; the published port is host-loopback-only, never public.
log "starting websockify (noVNC) on 0.0.0.0:${NOVNC_PORT} -> 127.0.0.1:${VNC_PORT}"
websockify --web /usr/share/novnc "0.0.0.0:${NOVNC_PORT}" "127.0.0.1:${VNC_PORT}" \
       >/tmp/websockify.log 2>&1 &

# 4) Hand off to the mission ──────────────────────────────────────────────────
log "display stack ready; exec: $*"
exec "$@"
