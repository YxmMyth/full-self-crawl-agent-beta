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
    "additionalProperties": False,
}


async def handle(ctx: Any, **kwargs: Any) -> str:
    raw_reason = kwargs.get("reason", "")
    if not isinstance(raw_reason, str):
        return (
            f"Error: 'reason' must be a string, got {type(raw_reason).__name__}: "
            f"{raw_reason!r}"
        )
    reason: str = raw_reason.strip()
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

    # Tamper detection: launcher pinned the original checklist's sha256 onto
    # ctx (in-memory, agent cannot read or modify). If the file on disk no
    # longer matches, the agent has edited the acceptance contract —
    # invariably a satisficing attempt — and we FAIL closed.
    # See 2026-05-25 svg harvest: agent rewrote `## C1.` to `## C1:` and
    # `**criterion**:`/`**check**:` fields so the parser found 0 criteria,
    # then exploited the auto-PASS fallback. Hash check blocks the same path.
    expected_hash = getattr(ctx, "_checklist_sha256", None)
    if expected_hash and checklist_path.is_file():
        import hashlib
        current_hash = hashlib.sha256(checklist_path.read_bytes()).hexdigest()
        if current_hash != expected_hash:
            logger.warning(
                f"mark_done: checklist tampered. "
                f"expected={expected_hash[:16]}... current={current_hash[:16]}..."
            )
            return (
                "VERIFICATION FAIL — checklist.md was modified after mission "
                "start.\n\n"
                "The checklist is the acceptance contract pinned at launch; "
                "you cannot edit it. Editing it (even via apply_patch / bash) "
                "is a satisficing attempt and will always FAIL.\n\n"
                "If a check command is genuinely wrong for the requirement, "
                "complete what the (mis-compiled) check actually verifies — "
                "the checklist is mechanical, not interpretive.\n\n"
                f"Expected sha256: {expected_hash[:16]}...\n"
                f"Current sha256:  {current_hash[:16]}...\n\n"
                "To proceed: restore the original checklist content, or "
                "address the original criteria as compiled."
            )

    # Lazy import to avoid coupling the tool module to harvest internals
    from src.harvest.checklist import run_all_checks, format_results

    results = await run_all_checks(checklist_path, workspace)

    if not results:
        # Fail-closed: parse failure (corrupt checklist / missing file) is
        # NOT a free pass. The previous auto-PASS fallback was abused by a
        # harvest agent rewriting headers to defeat the parser. If the
        # acceptance contract is unreadable, the mission cannot be verified
        # and therefore cannot be marked done.
        logger.warning(
            f"mark_done: no parseable checklist criteria at {checklist_path}, "
            "FAILing closed"
        )
        return (
            "VERIFICATION FAIL — checklist.md exists but the parser found 0 "
            "criteria.\n\n"
            "Possible causes:\n"
            "  - The checklist file is missing or empty\n"
            "  - The format is wrong: each section MUST be `## C<N>.` (period, "
            "not colon) followed by `**criterion**:` and `**check**: \\`...\\``\n"
            "  - The checklist was edited and broke the parser\n\n"
            "The acceptance contract is pinned at mission start — you cannot "
            "rewrite it. If the original is still on disk and the parser "
            "fails, this is a system bug and the mission cannot proceed."
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
