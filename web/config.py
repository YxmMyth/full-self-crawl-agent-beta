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
