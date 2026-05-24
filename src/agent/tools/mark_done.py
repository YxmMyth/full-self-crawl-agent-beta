"""mark_done — harvest agent claims mission complete.

Runs the checklist mechanical verifier (no LLM in the loop). On all-PASS,
sets ctx._mission_done so AgentSession's loop exits. On any FAIL, returns
the specific failed criteria as the tool result and the agent continues.

The checklist (workspace/checklist.md) is compiled from requirement.txt
by the harvest launcher at mission start. mark_done parses + runs it.

Requires the following attribute on ctx (attached by harvest launcher):
  - ctx._domain      : the domain string

See: src/harvest/checklist.py for the parse + run engine.
"""

from __future__ import annotations

from typing import Any

from src.config import Config
from src.utils.logging import get_logger

logger = get_logger(__name__)

TOOL_NAME = "mark_done"
TOOL_DESCRIPTION = (
    "Claim the harvest mission is complete.\n\n"
    "Runs the acceptance checklist at workspace/checklist.md — a set of "
    "concrete bash checks (file counts, shape match, content non-empty, etc.) "
    "compiled from the requirement at mission start. Each check is a bash "
    "command; exit 0 means PASS.\n\n"
    "Returns one of:\n"
    "  - PASS: all criteria satisfied → harvest ends.\n"
    "  - FAIL: lists specific failed criteria (id, criterion, check command, "
    "exit code, output) → you fix the underlying gaps and call mark_done again.\n\n"
    "Before calling, read `workspace/checklist.md` yourself, run each check "
    "command via bash to confirm it passes — that way you avoid round-trips "
    "through this tool. If you're unsure whether the check WILL pass, you're "
    "not done."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "description": (
                "Brief explanation of what you accomplished. Logged with the "
                "verdict; if the checklist FAILs, this is preserved for the "
                "next mark_done attempt."
            ),
        },
    },
    "required": ["reason"],
}


async def handle(ctx: Any, **kwargs: Any) -> str:
    reason: str = kwargs.get("reason", "").strip()
    if not reason:
        return "Error: mark_done requires a non-empty `reason`."

    domain = getattr(ctx, "_domain", None)
    if not domain:
        return (
            "Error: mark_done cannot run — ctx._domain missing. "
            "This is a launcher bug, not your fault."
        )

    workspace = Config.run_dir(domain) / "workspace"
    checklist_path = workspace / "checklist.md"

    # Lazy import to avoid coupling the tool module to harvest internals
    from src.harvest.checklist import run_all_checks, format_results

    results = await run_all_checks(checklist_path, workspace)

    if not results:
        # No checklist or it parsed to 0 criteria — fall back to agent claim.
        # The launcher would normally have written at least a stub; if even
        # that is missing, we don't block the mission.
        ctx._mission_done = True
        logger.warning(
            f"mark_done: no checklist criteria at {checklist_path}, "
            "auto-PASS based on agent reason"
        )
        return (
            f"VERIFICATION SKIPPED — no parseable checklist at "
            f"{checklist_path}. Mission marked complete based on agent reason.\n\n"
            f"Reason: {reason}"
        )

    all_passed = all(r.passed for r in results)
    summary = format_results(results)
    logger.info(
        f"mark_done checklist: {sum(r.passed for r in results)}/{len(results)} "
        f"PASS, all_passed={all_passed}"
    )

    if all_passed:
        ctx._mission_done = True
        return f"VERIFICATION PASS.\n\n{summary}\n\nAgent reason: {reason}"

    return (
        f"VERIFICATION FAIL — the mission is NOT yet complete.\n\n"
        f"{summary}\n\n"
        "Fix each failed criterion (run the failing `check` command yourself "
        "via bash to inspect, then apply_patch / bash to address the gap), "
        "then call mark_done again."
    )
