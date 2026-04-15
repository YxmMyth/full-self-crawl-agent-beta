"""Centralized configuration — all environment variables in one place."""

from __future__ import annotations

import os
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
        """Return the artifacts directory for a specific domain, creating it if needed."""
        p = cls.ARTIFACTS_DIR / domain
        p.mkdir(parents=True, exist_ok=True)
        return p
