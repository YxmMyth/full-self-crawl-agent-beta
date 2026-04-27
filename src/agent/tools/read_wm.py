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
    "Query the World Model — accumulated understanding of the site.\n\n"
    "Modes:\n"
    "- No arguments: returns the current run's Semantic + Procedural Models.\n"
    "- With location: returns observations for that location (current run).\n"
    "- With run_id: read another run's Semantic + Procedural Models (read-only).\n"
    "  Use this to borrow context from past runs on the same domain. "
    "When run_id is set, location is ignored — only Models are returned, "
    "not raw observations.\n\n"
    "Other runs on this domain are listed at artifacts/{domain}/runs/*/. "
    "Use bash to ls them and find run_ids worth reading."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "location": {
            "type": "string",
            "description": (
                "Location ID to query (e.g. 'codepen.io::/tag/{tag}'). "
                "Omit to get Semantic + Procedural Model. Ignored when run_id is set."
            ),
        },
        "run_id": {
            "type": "string",
            "description": (
                "Optional. Read Models from another run (read-only borrow). "
                "Only Models returned, not observations. "
                "Find available run_ids via `bash ls artifacts/{domain}/runs/`."
            ),
        },
    },
    "required": [],
}


async def handle(ctx: Any, **kwargs: Any) -> str:
    location = kwargs.get("location")
    other_run_id = kwargs.get("run_id")
    domain = getattr(ctx, "_domain", None) or ""

    # Cross-run mode: only return Models, no observations
    if other_run_id:
        from src.config import Config
        if other_run_id == Config.RUN_ID:
            # Same as current run — fall through to default behavior
            other_run_id = None
        else:
            semantic, procedural = await db.load_both_models(domain, run_id=other_run_id)
            if not semantic and not procedural:
                return f"No Models found for run_id={other_run_id} on domain {domain}."

            parts = [f"## Models from run: {other_run_id}", "(read-only borrow)\n"]
            parts.append("### Semantic Model\n")
            parts.append(semantic or "(empty)")
            parts.append("\n---\n\n### Procedural Model\n")
            parts.append(procedural or "(empty)")
            return "\n".join(parts)

    if location:
        # Observations for a specific location, current run scope
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

    # Current run's Models
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
