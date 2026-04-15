"""World Model dataclasses — the data structures for site understanding.

Three-layer memory architecture (CoALA):
  Transcript (immutable) → Observations (Recording Agent CRUD) → Models (maintain_model rewrites)

See: docs/WorldModel设计.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Observation:
    """A single structured observation about a location.

    Maintained by the Recording Agent (create/update/merge/delete).
    raw is free-form JSONB — type distinguished by key presence:
      "page_summary" in raw → page snapshot
      "extraction_method" in raw → extraction result
      "insight" in raw → agent understanding
    """
    id: int | None
    location_id: str
    agent_step: int | None
    raw: dict[str, Any]
    created_at: datetime | None = None


@dataclass
class Location:
    """A discovered URL pattern on the target site.

    ID format: domain::pattern (e.g. "codepen.io::/tag/{tag}")
    Granularity decided by the agent — system does not enforce URL normalization rules.
    """
    id: str                           # domain::pattern
    run_id: str | None
    domain: str
    pattern: str                      # agent-decided granularity
    how_to_reach: str | None = None
    observations: list[Observation] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class Session:
    """Execution log for one Agent Session.

    Not part of the World Model — records what the agent did, not site understanding.
    """
    id: str
    run_id: str | None = None
    direction: str | None = None        # briefing direction for this session
    started_at: datetime | None = None
    ended_at: datetime | None = None
    outcome: str | None = None          # natural_stop / context_exhausted / consecutive_errors / safety_net
    steps_taken: int | None = None
    trajectory_summary: str | None = None


@dataclass
class SiteWorldModel:
    """Aggregate root — the complete understanding of a site.

    Combines all three memory layers for a given domain:
    - locations + observations (episodic memory)
    - semantic_model (semantic memory) — ~8000 chars, site understanding
    - procedural_model (procedural memory) — ~6000 chars, methodology
    """
    domain: str
    locations: list[Location] = field(default_factory=list)
    semantic_model: str = ""
    procedural_model: str = ""

    @property
    def is_empty(self) -> bool:
        """True if this is a fresh domain with no prior knowledge."""
        return not self.locations and not self.semantic_model

    @property
    def observation_count(self) -> int:
        return sum(len(loc.observations) for loc in self.locations)

    def get_location(self, location_id: str) -> Location | None:
        """Find a location by ID."""
        for loc in self.locations:
            if loc.id == location_id:
                return loc
        return None
