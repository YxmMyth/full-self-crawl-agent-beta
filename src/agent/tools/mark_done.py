"""mark_done — harvest agent claims mission complete.

Internally triggers the harvest Verification subagent. On PASS, sets
ctx._mission_done so AgentSession's loop exits. On FAIL/PARTIAL, the
tool result returns specific gaps and the agent continues.

Feature-gated by Config.VERIFICATION_SUBAGENT_ENABLED — when disabled,
mark_done auto-passes (useful for testing without spending LLM calls
on verification).

Requires the following attributes on ctx:
  - ctx._domain      : the domain string
  - ctx._llm         : a LLMClient instance
  - ctx._requirement : the requirement string (boundary spec)

These are attached by the harvest launcher before running AgentSession.

See: docs/工具重新设计共识.md §2.2c
"""

from __future__ import annotations

from typing import Any

from src.config import Config
from src.utils.logging import get_logger

logger = get_logger(__name__)

TOOL_NAME = "mark_done"
TOOL_DESCRIPTION = (
    "Claim the harvest mission is complete.\n\n"
    "Triggers an adversarial Verification subagent that independently checks "
    "your workspace against the requirement boundary and the recon samples. "
    "It will look for: short counts, missing fields, stub records, missing "
    "reproducibility code.\n\n"
    "Returns one of:\n"
    "  - PASS: mission accepted, harvest ends.\n"
    "  - FAIL: significant gaps reported; you continue and address them.\n"
    "  - PARTIAL: usable but limited; you continue to close named gaps.\n\n"
    "Don't call this optimistically — the verifier will catch corner-cutting "
    "and you'll waste a round. Verify yourself first (count, shape, sample-check) "
    "before claiming done."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "description": (
                "Brief explanation of what you accomplished and why you "
                "believe the mission is complete. The verifier reads this."
            ),
        },
    },
    "required": ["reason"],
}


async def handle(ctx: Any, **kwargs: Any) -> str:
    reason: str = kwargs.get("reason", "").strip()
    if not reason:
        return "Error: mark_done requires a non-empty `reason`."

    # Feature gate — when disabled, mark_done auto-passes.
    if not Config.VERIFICATION_SUBAGENT_ENABLED:
        ctx._mission_done = True
        logger.info("mark_done: verification subagent disabled, auto-PASS")
        return f"VERIFICATION SKIPPED (feature disabled). Mission marked complete: {reason}"

    domain = getattr(ctx, "_domain", None)
    llm = getattr(ctx, "_llm", None)
    requirement = getattr(ctx, "_requirement", None)

    if not domain or llm is None or requirement is None:
        # Misconfigured launcher — fail loudly so the bug surfaces.
        missing = [
            name for name, val in [("_domain", domain), ("_llm", llm), ("_requirement", requirement)]
            if not val
        ]
        return (
            f"Error: mark_done cannot run verification — missing ctx attributes: "
            f"{', '.join(missing)}. This is a launcher bug, not your fault. "
            "Continue trying other approaches; the user will need to fix the launcher."
        )

    from src.harvest.verification import run_harvest_verification

    verdict, feedback = await run_harvest_verification(llm, domain, requirement, reason)

    if verdict == "PASS":
        ctx._mission_done = True
        return f"VERIFICATION PASS. Mission complete.\n\n{feedback}"

    return (
        f"VERIFICATION {verdict}. The mission is NOT yet complete. "
        f"Continue and address these:\n\n{feedback}"
    )
