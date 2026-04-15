"""read_world_model — query the World Model (two-layer retrieval).

No params → return complete Semantic + Procedural Model (the index).
With location → return that location's Observations (the evidence).

See: docs/工具重新设计共识.md §2.2
"""

from __future__ import annotations

import json
from typing import Any

from src.world_model import db

TOOL_NAME = "read_world_model"
TOOL_DESCRIPTION = (
    "Query the World Model — your accumulated understanding of the site.\n\n"
    "Two retrieval modes:\n"
    "- No arguments: returns the Semantic Model (site structure, data relationships, "
    "requirement mapping) + Procedural Model (working methods, failed approaches, "
    "navigation patterns). Use this to recall the big picture.\n"
    "- With location: returns all Observations for that location (detailed evidence). "
    "Use this to recall specifics about a page/endpoint you've visited.\n\n"
    "The Models are the index; Observations are the evidence. "
    "Read the Model first to know where to look, then drill into specific locations."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "location": {
            "type": "string",
            "description": (
                "Location ID to query (e.g. 'codepen.io::/tag/{tag}'). "
                "Omit to get the full Semantic + Procedural Model."
            ),
        },
    },
    "required": [],
}


async def handle(ctx: Any, **kwargs: Any) -> str:
    location = kwargs.get("location")
    domain = getattr(ctx, "_domain", None) or ""

    if location:
        # Retrieve observations for a specific location
        observations = await db.list_observations_by_location(location)
        if not observations:
            return f"No observations found for location: {location}"

        lines = [f"## Observations for {location}", f"({len(observations)} observations)", ""]
        for obs in observations:
            raw_str = json.dumps(obs.raw, ensure_ascii=False, indent=2)
            step_info = f" (step {obs.agent_step})" if obs.agent_step else ""
            lines.append(f"### Observation #{obs.id}{step_info}")
            lines.append(raw_str)
            lines.append("")
        return "\n".join(lines)

    else:
        # Return complete Models
        semantic, procedural = await db.load_both_models(domain)

        parts = []
        if semantic:
            parts.append("## Semantic Model\n")
            parts.append(semantic)
        else:
            parts.append("## Semantic Model\n(empty — no sessions completed yet)")

        parts.append("\n\n---\n\n")

        if procedural:
            parts.append("## Procedural Model\n")
            parts.append(procedural)
        else:
            parts.append("## Procedural Model\n(empty — no sessions completed yet)")

        return "\n".join(parts)
