"""mark_done — harvest agent claims mission complete.

Triggers the harvest LLM audit (src/harvest/audit.py). On overall=PASS,
sets ctx._mission_done so AgentSession's loop exits. On BLOCKED, returns
per-criterion feedback so the agent can address gaps and retry.

Previously this ran a pre-compiled bash checklist (workspace/checklist.md).
Replaced 2026-05-25 after the svg run deadlocked: the LLM-compiled bash
check (`file --mime-type | grep text/plain`) rejected `application/javascript`
files even though "non-binary" criterion was satisfied. Root cause was
fortune-telling — checks were compiled before any data existed, then
frozen. LLM late-bind audit avoids that by seeing actual disk state.

Requires the following attribute on ctx (attached by harvest launcher):
  - ctx._domain      : the domain string
  - ctx._llm         : LLMClient for the audit's generate() call
  - ctx._requirement : the requirement text the audit checks against

See: src/harvest/audit.py for the audit phases + criteria.
"""

from __future__ import annotations

from typing import Any

from src.utils.logging import get_logger

logger = get_logger(__name__)

TOOL_NAME = "mark_done"
TOOL_DESCRIPTION = (
    "Claim the harvest mission is complete.\n\n"
    "Triggers a two-phase audit:\n"
    "  Phase 1 (mechanical): data/ has at least one file, total >1KB, "
    "requirement.txt + workspace exist. Cheap, deterministic.\n"
    "  Phase 2 (LLM): single audit call evaluates 6 qualitative criteria — "
    "universe coverage, shape compliance, content quality, requirement fit, "
    "reproducibility, error transparency. Sees the actual catalog universe, "
    "the samples shape contract, and the on-disk listing of your data/ + "
    "scripts + error logs.\n\n"
    "Returns one of:\n"
    "  - PASS: all 6 criteria verified against evidence → harvest ends.\n"
    "  - BLOCKED: per-criterion verdict with evidence + actionable gaps → "
    "you address the gaps and call mark_done again.\n\n"
    "The `reason` you provide is one input to the audit — include concrete "
    "evidence (counts, diff results, script names) the auditor can cross-"
    "reference against the disk. Vague reasons get vague verdicts; concrete "
    "ones get concrete PASS or precise FAIL."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "description": (
                "Concrete evidence summary the auditor can cross-reference: "
                "what you produced (file counts, scope coverage), what's in "
                "the data (sample content type, field set), how you "
                "verified (commands you ran, their outputs). The auditor "
                "compares your claim to the actual on-disk listing."
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
    llm = getattr(ctx, "_llm", None)
    if llm is None:
        return (
            "Error: mark_done cannot run — ctx._llm missing. "
            "This is a launcher bug, not your fault."
        )
    requirement = getattr(ctx, "_requirement", "") or ""

    # Lazy import to avoid coupling this tool to harvest internals at import time
    from src.harvest.audit import run_audit

    result = await run_audit(
        llm=llm,
        domain=domain,
        requirement=requirement,
        mark_done_reason=reason,
    )

    if result.overall == "PASS":
        ctx._mission_done = True
        return (
            f"VERIFICATION PASS — all 6 criteria verified against evidence.\n\n"
            f"Agent reason recorded: {reason}\n\n"
            f"Audit report written to verification/."
        )

    # BLOCKED — feed back specific gaps so agent can iterate
    feedback = result.feedback()
    return (
        f"VERIFICATION BLOCKED — the mission is NOT yet complete.\n\n"
        f"{feedback[:3000]}\n\n"
        "Address the failing criteria's gaps (the evidence + reason fields "
        "tell you what's missing), then call mark_done again with an updated "
        "`reason` citing the new evidence."
    )
