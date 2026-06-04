# syntax=docker/dockerfile:1
#
# ── Full-Self-Crawl-Agent — per-run image (option B: self-contained machine) ──
#
# One disposable `docker run --rm` of this image == one mission. It holds
# EVERYTHING a run touches at runtime:
#   - python + the project's pinned deps
#   - Camoufox (a patched Firefox) + its runtime libraries
#   - its OWN display stack: Xvfb + x11vnc + websockify/noVNC  (per-run :99,
#     so two runs never share a framebuffer — the multi-user-safe choice)
#
# What stays OUTSIDE (reached at runtime, NOT baked in):
#   - PostgreSQL World Model        → over the network (DATABASE_URL)
#   - per-domain login profiles      → bind-mounted at /app/artifacts/_profiles
#   - run artifacts (samples/...)    → bind-mounted at /app/artifacts/<domain>/runs
#
# The container runs as root INTERNALLY; isolation/safety on the shared server
# comes from launching it under a *rootless* daemon (container-root maps to an
# unprivileged host user). Locally (Docker Desktop) the daemon is already a
# private VM, so root-inside is fine. See memory: project-containerization-design.

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # CN-friendly pip index (this box + the target server are both in China; the
    # default pypi.org is slow/flaky from here). Persists into the image so the
    # agent's runtime `pip install` (decode tools, etc.) is fast too.
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    # Container-internal virtual display the entrypoint creates and the mission
    # renders onto. Each container gets its own :99 (no collision — containers
    # are isolated). The supervisor passes the SAME DISPLAY=:99 explicitly.
    DISPLAY=:99 \
    SCREEN_GEOMETRY=1280x900x24 \
    VNC_PORT=5900 \
    NOVNC_PORT=6080 \
    # In-container artifacts root == PROJECT_ROOT/artifacts; the host bind-mount
    # lands here so Config.run_dir() resolves identically inside and out.
    ARTIFACTS_DIR=/app/artifacts

# ── APT mirror + resilience ───────────────────────────────────────────────────
#   The default deb.debian.org is slow/flaky from China (the first build died
#   here: 77 kB/s, then "Connection failed" on 3 packages after 21 min). Point
#   apt at the Tsinghua mirror and add retries. Persists into the image so the
#   agent's runtime `apt-get install` (mono/wine/ffmpeg/...) is fast too.
#   Handle BOTH the new deb822 (debian.sources) and the legacy sources.list.
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list; \
    fi; \
    printf 'Acquire::Retries "8";\nAcquire::http::Timeout "30";\n' > /etc/apt/apt.conf.d/80-retries

# ── System packages ──────────────────────────────────────────────────────────
#   display : xvfb (virtual X), x11vnc (VNC server), novnc + websockify
#             (browser-based VNC — same stack as scripts/server_display.sh)
#   x11-utils: xdpyinfo, for the entrypoint's fail-loud display smoke test
#   fonts   : liberation + noto-cjk + emoji so Chinese/JP galgame sites render
#             (not tofu boxes) — this system crawls CJK sites
#   utils   : procps(pgrep), ca-certificates, curl, git (the agent's bash tool
#             may need them; the run can apt-install more at runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify x11-utils \
        fonts-liberation fonts-noto-cjk fonts-noto-color-emoji \
        procps ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python deps (own layer: copy only the manifest first for cache reuse) ─────
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# ── Camoufox: system libs for its Firefox, then fetch the browser binary ──────
#   `playwright install-deps firefox` apt-installs the exact Firefox runtime
#   libs (gtk/dbus/alsa/...). Camoufox is a patched Firefox, so the same libs
#   apply. `camoufox fetch` then downloads the Camoufox build INTO the image so
#   no per-run download is needed. The fetch pulls from GitHub releases (can be
#   flaky from CN) — retry up to 3×, fail the build only if all 3 fail.
RUN playwright install-deps firefox \
    && (python -m camoufox fetch || python -m camoufox fetch || python -m camoufox fetch)

# ── Project code (last, so editing code doesn't bust the heavy dep layers) ────
#   .dockerignore keeps artifacts/.git/.venv/.env out. The runtime bind-mount
#   re-overlays /app/artifacts with the host's real data.
COPY . /app

# ── Entrypoint: bring up the display stack, then exec the mission ─────────────
RUN chmod +x /app/docker/entrypoint.sh

# noVNC web port — the supervisor publishes this to a host loopback port per run.
EXPOSE 6080

ENTRYPOINT ["/app/docker/entrypoint.sh"]
# Default CMD = recon usage banner; the supervisor overrides with the real argv
# (e.g. python -u -m src.main explore <domain> "<req>").
CMD ["python", "-u", "-m", "src.main"]
