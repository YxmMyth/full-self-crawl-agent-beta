"""DbReadService — read-only window onto the World Model for the console.

Wraps the EXISTING src.world_model.db query functions verbatim. Never writes.
The engine remains the single source of truth; the console only observes.

Observation type discrimination: Observation.raw is free-form JSONB. By
convention the Recording Agent uses keys page_summary / insight /
extraction_method / data_sample. We classify by presence (best-effort, never
authoritative) purely to colour-badge the stream.
"""

from __future__ import annotations

from typing import Any

from src.world_model import db
from src.world_model.model import Location, Observation, Session


def classify_observation(raw: Any) -> str:
    """Best-effort observation type for badging. Never authoritative."""
    if not isinstance(raw, dict):
        return "other"
    if "insight" in raw:
        return "insight"
    if "extraction_method" in raw or "data_sample" in raw:
        return "extract"
    if "page_summary" in raw:
        return "summary"
    return "other"


def observation_preview(raw: Any, limit: int = 240) -> str:
    """A short human preview of an observation's most salient text."""
    if not isinstance(raw, dict):
        text = str(raw)
    else:
        text = ""
        for key in ("insight", "page_summary", "extraction_method", "summary", "note"):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                text = val.strip()
                break
        if not text:
            for v in raw.values():
                if isinstance(v, str) and v.strip():
                    text = v.strip()
                    break
        if not text:
            import json

            text = json.dumps(raw, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class DbReadService:
    """Read-only convenience layer. All methods assume db.connect() was called."""

    async def list_locations(self, domain: str) -> list[Location]:
        return await db.list_locations(domain)

    async def list_observations_since(
        self, domain: str, since_id: int | None, run_id: str | None
    ) -> list[Observation]:
        return await db.list_observations_since(domain, since_id, run_id=run_id)

    async def list_observations(
        self, domain: str, run_id: str | None
    ) -> list[Observation]:
        return await db.list_observations_by_domain(domain, run_id=run_id)

    async def list_sessions(self, run_id: str) -> list[Session]:
        return await db.list_sessions_by_run(run_id)

    async def load_both_models(self, domain: str, run_id: str | None) -> tuple[str, str]:
        return await db.load_both_models(domain, run_id=run_id)

    async def list_runs(self, domain: str) -> list[dict[str, Any]]:
        return await db.list_runs(domain)
