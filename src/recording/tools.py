"""Recording Agent's 4 CRUD tools for maintaining Observations.

Separate from the execution agent's tool set.
Pattern follows Claude Code's Read/Edit/Write model.

See: 架构共识文档.md §7.4, docs/工具重新设计共识.md §1.5
"""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools.registry import ToolRegistry
from src.world_model import db
from src.utils.logging import get_logger

logger = get_logger(__name__)


# ── read_observations ────────────────────────────────────

READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_observations",
        "description": (
            "View current observations. Always read before creating or editing "
            "to avoid duplicates and ID mismatches.\n\n"
            "No arguments: list all observations for the current domain.\n"
            "With location: list observations for that specific location."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Location ID (e.g. 'codepen.io::/tag/{tag}'). Omit for all.",
                },
            },
            "required": [],
        },
    },
}


async def handle_read(domain: str, **kwargs: Any) -> str:
    location = kwargs.get("location")

    if location:
        observations = await db.list_observations_by_location(location)
        if not observations:
            return f"No observations for location: {location}"
    else:
        observations = await db.list_observations_by_domain(domain)
        if not observations:
            return "No observations recorded yet."

    lines = [f"Observations ({len(observations)}):"]
    for obs in observations:
        raw_preview = json.dumps(obs.raw, ensure_ascii=False)[:300]
        loc = obs.location_id
        lines.append(f"\n[id={obs.id}] location={loc}")
        lines.append(f"  {raw_preview}")
    return "\n".join(lines)


# ── create_observation ───────────────────────────────────

CREATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_observation",
        "description": (
            "Create a new observation for a location.\n\n"
            "The location is specified as a pattern string (e.g. '/tag/{tag}', "
            "'/pen/{id}', '/api/graphql'). The system automatically finds or "
            "creates the Location record.\n\n"
            "One observation = one fact. Keep them atomic and location-specific."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Location pattern (e.g. '/tag/{tag}', '/api/graphql').",
                },
                "raw": {
                    "type": "object",
                    "description": "Observation content. Free-form JSON. Include specific findings, not actions.",
                },
            },
            "required": ["location", "raw"],
        },
    },
}


async def handle_create(domain: str, **kwargs: Any) -> str:
    location_pattern = kwargs.get("location", "")
    raw = kwargs.get("raw", {})

    if not location_pattern:
        return "Error: location is required"
    if not raw:
        return "Error: raw content is required"

    # Auto find-or-create Location
    loc = await db.find_or_create_location(domain, location_pattern)

    obs = await db.create_observation(loc.id, raw)
    logger.info(
        f"Recording Agent created observation #{obs.id} at {loc.id}",
        extra={"domain": domain},
    )
    return f"Created observation #{obs.id} at {loc.id}"


# ── edit_observation ─────────────────────────────────────

EDIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "edit_observation",
        "description": (
            "Update an existing observation's content.\n\n"
            "Use when new findings extend or correct an existing observation. "
            "Always read_observations first to get the correct ID."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "Observation ID (from read_observations).",
                },
                "raw": {
                    "type": "object",
                    "description": "Updated observation content (replaces entire raw).",
                },
            },
            "required": ["id", "raw"],
        },
    },
}


async def handle_edit(domain: str, **kwargs: Any) -> str:
    obs_id = kwargs.get("id")
    raw = kwargs.get("raw", {})

    if obs_id is None:
        return "Error: id is required"
    if not raw:
        return "Error: raw content is required"

    await db.update_observation(obs_id, raw)
    logger.info(f"Recording Agent updated observation #{obs_id}", extra={"domain": domain})
    return f"Updated observation #{obs_id}"


# ── delete_observation ───────────────────────────────────

DELETE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delete_observation",
        "description": (
            "Delete a redundant or superseded observation.\n\n"
            "Use when merging observations or removing outdated info. "
            "Always read_observations first to confirm the ID."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "Observation ID to delete.",
                },
            },
            "required": ["id"],
        },
    },
}


async def handle_delete(domain: str, **kwargs: Any) -> str:
    obs_id = kwargs.get("id")
    if obs_id is None:
        return "Error: id is required"

    await db.delete_observation(obs_id)
    logger.info(f"Recording Agent deleted observation #{obs_id}", extra={"domain": domain})
    return f"Deleted observation #{obs_id}"


# ── Registry builder ─────────────────────────────────────

def build_recording_registry(domain: str) -> ToolRegistry:
    """Build a ToolRegistry with the 4 recording tools, bound to a domain."""
    registry = ToolRegistry()

    # Bind domain into handlers
    async def _read(ctx: Any, **kw: Any) -> str:
        return await handle_read(domain, **kw)

    async def _create(ctx: Any, **kw: Any) -> str:
        return await handle_create(domain, **kw)

    async def _edit(ctx: Any, **kw: Any) -> str:
        return await handle_edit(domain, **kw)

    async def _delete(ctx: Any, **kw: Any) -> str:
        return await handle_delete(domain, **kw)

    registry.register(
        "read_observations",
        READ_SCHEMA["function"]["description"],
        READ_SCHEMA["function"]["parameters"],
        _read,
    )
    registry.register(
        "create_observation",
        CREATE_SCHEMA["function"]["description"],
        CREATE_SCHEMA["function"]["parameters"],
        _create,
    )
    registry.register(
        "edit_observation",
        EDIT_SCHEMA["function"]["description"],
        EDIT_SCHEMA["function"]["parameters"],
        _edit,
    )
    registry.register(
        "delete_observation",
        DELETE_SCHEMA["function"]["description"],
        DELETE_SCHEMA["function"]["parameters"],
        _delete,
    )

    return registry


def recording_tool_schemas() -> list[dict]:
    """Return OpenAI-format tool schemas for LLM calls."""
    return [READ_SCHEMA, CREATE_SCHEMA, EDIT_SCHEMA, DELETE_SCHEMA]
