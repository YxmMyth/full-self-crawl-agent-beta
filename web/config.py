"""Web console configuration.

Security note: HOST is hard-coded to 127.0.0.1. The console has NO app-level
auth — the SSH tunnel is the sole auth boundary (deployment trust model). A
0.0.0.0 bind would expose an unauthenticated console AND, in Phase 2, an
interactive VNC into a logged-in browser. Do not change HOST without adding
real authentication first.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from src.config import Config as AgentConfig


class WebConfig:
    """Read-once config for the Helmsman web console."""

    # Cache-busting token for static assets. Computed once at process start, so
    # every Helmsman restart (which happens on every deploy) forces browsers to
    # re-fetch CSS/JS instead of serving a stale cached copy — the cause of the
    # "I deployed a fix but the browser still runs the old code" class of bugs.
    ASSET_VERSION: str = str(int(time.time()))

    # ── Bind (locked to localhost — see module docstring) ────
    HOST: str = "127.0.0.1"
    PORT: int = int(os.getenv("HELMSMAN_PORT", "8000"))

    # ── Subprocess launch ────────────────────────────────
    # Interpreter used to spawn `python -u -m src.main …`. Defaults to the same
    # interpreter running the web app (same venv).
    PYTHON_EXE: str = os.getenv("HELMSMAN_PYTHON", sys.executable)
    PROJECT_ROOT: Path = AgentConfig.PROJECT_ROOT

    # ── Live update cadence ──────────────────────────────
    POLL_INTERVAL_S: float = float(os.getenv("HELMSMAN_POLL_INTERVAL_S", "1.5"))
    EVENT_RING_SIZE: int = int(os.getenv("HELMSMAN_EVENT_RING", "1000"))

    # ── noVNC reverse proxy (Phase 2) ────────────────────
    NOVNC_UPSTREAM: str = os.getenv("HELMSMAN_NOVNC_UPSTREAM", "127.0.0.1:6080")

    # ── Containerized run mode (option B) ────────────────
    # HELMSMAN_CONTAINERIZED=1 → the supervisor launches each mission as a
    # disposable `docker run` of RUN_IMAGE instead of a host python subprocess.
    # The container carries its own browser + display stack; profiles + artifacts
    # are bind-mounted; PostgreSQL is reached over the docker bridge. The
    # container publishes its noVNC (:6080) to host NOVNC_HOST_PORT, which the
    # existing static /vnc upstream (127.0.0.1:6080) already targets — so the
    # single-serial path needs no proxy change (per-run dynamic ports are the
    # later concurrency workstream).
    CONTAINERIZED: bool = os.getenv("HELMSMAN_CONTAINERIZED", "0") == "1"
    RUN_IMAGE: str = os.getenv("HELMSMAN_RUN_IMAGE", "fsc-run:dev")
    NOVNC_HOST_PORT: int = int(os.getenv("HELMSMAN_NOVNC_HOST_PORT", "6080"))

    # Max runs allowed to EXECUTE concurrently (RunRegistry admission). Shipped
    # at 1 (single-serial, byte-identical to pre-registry behavior); raised to 6
    # once the N>1 landmines are fixed. The per-domain serial lock applies on top
    # regardless of this number.
    MAX_CONCURRENT_RUNS: int = int(os.getenv("HELMSMAN_MAX_CONCURRENT_RUNS", "1"))

    # ── Paths ────────────────────────────────────────────
    ARTIFACTS_DIR: Path = AgentConfig.ARTIFACTS_DIR
    WEB_DIR: Path = Path(__file__).resolve().parent
    STATIC_DIR: Path = WEB_DIR / "static"
    TEMPLATES_DIR: Path = WEB_DIR / "templates"

    @classmethod
    def runs_root(cls, domain: str) -> Path:
        """artifacts/{domain}/runs/ — where this domain's runs live."""
        return cls.ARTIFACTS_DIR / domain / "runs"

    @classmethod
    def run_dir(cls, domain: str, run_id: str) -> Path:
        """artifacts/{domain}/runs/{run_id}/ for an arbitrary (domain, run_id).

        Unlike AgentConfig.run_dir(), this does NOT depend on the global
        Config.RUN_ID — the console inspects many runs, not one.
        """
        return cls.ARTIFACTS_DIR / domain / "runs" / run_id
