"""think — no side-effect reasoning tool.

Lets the agent reason without taking action. Returns the thought as-is.
"""

from __future__ import annotations

from typing import Any

TOOL_NAME = "think"
TOOL_DESCRIPTION = (
    "Use this to reason through a problem step by step before acting. "
    "No side effects — your thought is recorded but nothing else happens. "
    "Good for: planning next steps, analyzing what you've seen, "
    "deciding between approaches."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "thought": {
            "type": "string",
            "description": "Your reasoning or analysis.",
        },
    },
    "required": ["thought"],
}


async def handle(ctx: Any, **kwargs: Any) -> str:
    thought = kwargs.get("thought", "")
    return thought
