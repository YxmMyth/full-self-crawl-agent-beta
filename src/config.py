"""Centralized configuration — all environment variables in one place."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (src/../.env)
_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / ".env")


class Config:
    """Read-once config from environment variables.

    Required vars raise on access if missing.
    Optional vars have defaults.
    """

    # ── LLM ──────────────────────────────────────────────
    LLM_API_KEY: str = os.environ.get("LLM_API_KEY", "")
    LLM_BASE_URL: str = os.environ.get("LLM_BASE_URL", "")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "deepseek-chat")
    VISION_LLM_MODEL: str = os.getenv("VISION_LLM_MODEL", "kimi-k2.5")

    # ── Database ─────────────────────────────────────────
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")

    # ── Browser ──────────────────────────────────────────
    BROWSER_WS_URL: str | None = os.getenv("BROWSER_WS_URL")
    BROWSER_CDP_URL: str | None = os.getenv("BROWSER_CDP_URL")

    # ── Artifacts ────────────────────────────────────────
    ARTIFACTS_DIR: Path = Path(os.getenv("ARTIFACTS_DIR", "./artifacts"))

    # ── Feature gates ────────────────────────────────────
    VERIFICATION_SUBAGENT_ENABLED: bool = (
        os.getenv("VERIFICATION_SUBAGENT_ENABLED", "true").lower() == "true"
    )

    # ── Safety net (Planner) ─────────────────────────────
    MAX_PLANNER_TOOL_CALLS: int = int(os.getenv("MAX_PLANNER_TOOL_CALLS", "200"))
    MAX_SESSIONS: int = int(os.getenv("MAX_SESSIONS", "15"))
    MAX_CONSECUTIVE_SAME_TOOL: int = int(os.getenv("MAX_CONSECUTIVE_SAME_TOOL", "5"))

    # ── Derived ──────────────────────────────────────────
    PROJECT_ROOT: Path = _project_root

    # ── Run identity ─────────────────────────────────────
    # Set once at main.py startup via Config.set_run_id().
    # All per-run artifacts live under artifacts/{domain}/runs/{RUN_ID}/.
    # All DB rows are tagged with this RUN_ID.
    RUN_ID: str = ""

    @classmethod
    def require(cls, *var_names: str) -> None:
        """Validate that required config vars are non-empty. Raise early."""
        missing = [name for name in var_names if not getattr(cls, name, "")]
        if missing:
            raise RuntimeError(
                f"Missing required config: {', '.join(missing)}. "
                f"Set them in .env or environment."
            )

    @classmethod
    def artifacts_for(cls, domain: str) -> Path:
        """Domain-level artifacts root — `artifacts/{domain}/`.

        Use this only for things shared across runs of the same domain
        (in practice: nothing — see run_dir() for run-scoped paths).
        """
        p = cls.ARTIFACTS_DIR / domain
        p.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def run_dir(cls, domain: str) -> Path:
        """Per-run artifacts root — `artifacts/{domain}/runs/{RUN_ID}/`.

        All per-run outputs (samples, sessions, verification, research,
        workspace, transcripts) go under here. Set RUN_ID first via
        Config.set_run_id(). Created on first access.
        """
        if not cls.RUN_ID:
            raise RuntimeError(
                "Config.RUN_ID not set. Call Config.set_run_id(...) at startup."
            )
        p = cls.ARTIFACTS_DIR / domain / "runs" / cls.RUN_ID
        p.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def set_run_id(cls, requirement: str) -> str:
        """Generate and set RUN_ID from requirement string + timestamp.

        Format: {YYYYMMDD-HHMM}_{requirement_slug_max_30_chars}
        Returns the generated RUN_ID.
        """
        ts = time.strftime("%Y%m%d-%H%M", time.localtime())
        slug = _slugify(requirement, max_len=30)
        cls.RUN_ID = f"{ts}_{slug}" if slug else ts
        return cls.RUN_ID


_SLUG_BAD_CHARS = re.compile(r"[/\\:?\"<>|*\x00-\x1f]+")
_SLUG_DASHES = re.compile(r"-{2,}")


def _slugify(text: str, max_len: int = 30) -> str:
    """Make a filesystem-safe folder slug from arbitrary text.

    Keeps Chinese / Unicode (modern OS handle them in paths). Just strips
    chars that are filesystem-unsafe on Windows/POSIX.
    """
    if not text:
        return ""
    s = text.strip()
    s = _SLUG_BAD_CHARS.sub("-", s)
    s = s.replace(" ", "-")
    s = _SLUG_DASHES.sub("-", s)
    s = s.strip("-")
    return s[:max_len]
