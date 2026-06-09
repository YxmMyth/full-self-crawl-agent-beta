"""mark_done — harvest agent claims mission complete.

Triggers the harvest LLM audit (src/harvest/audit.py). The audit loads
mission-specific criteria from workspace/checklist.md (compiled by the
launcher at mission start) and evaluates each against actual on-disk
evidence. On overall=PASS, sets ctx._mission_done. On BLOCKED, returns
per-criterion feedback.

This replaces the previous bash-checklist mechanism (deleted in 0993590).
The mistake there was deleting the checklist artifact too — agent lost
the "I know what done means" anchor. Restored 2026-05-25: checklist.md
keeps criterion descriptions (no bash), audit reads them at verify time.

Tamper resistance: the launcher pins sha256(checklist.md) onto ctx. We
verify the on-disk file matches before invoking the audit — any edit
fails the mission immediately, with a clear "you can't edit the
acceptance contract" message.

Requires the following attribute on ctx (attached by harvest launcher):
  - ctx._domain             : the domain string
  - ctx._llm                : LLMClient for the audit's generate() call
  - ctx._requirement        : the requirement text the audit checks against
  - ctx._checklist_sha256   : pinned hash for tamper detection

See: src/harvest/audit.py + src/harvest/checklist.py.
"""

from __future__ import annotations

import hashlib
from typing import Any

from src.config import Config
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

    # Tamper detection: launcher pinned the original checklist's sha256
    # onto ctx (in-memory, agent cannot read or modify). If the file on
    # disk no longer matches, agent has edited the acceptance contract —
    # invariably a satisficing attempt — and we FAIL closed.
    checklist_path = Config.run_dir(domain) / "workspace" / "checklist.md"
    expected_hash = getattr(ctx, "_checklist_sha256", None)
    if expected_hash and checklist_path.is_file():
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
                "If a criterion is genuinely wrong for the requirement, "
                "satisfy what it actually says — the contract is the contract.\n\n"
                f"Expected sha256: {expected_hash[:16]}...\n"
                f"Current sha256:  {current_hash[:16]}...\n\n"
                "To proceed: restore the original checklist content."
            )

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
            "VERIFICATION PASS — the data was verified against the acceptance "
            "criteria and the intent's evidence standard.\n\n"
            f"Agent reason recorded: {reason}\n\n"
            "Audit report written to verification/."
        )

    if result.overall == "UNCERTAIN":
        return await _adjudicate_uncertain(ctx, result)

    # FAIL — feed back specific gaps so the agent can iterate
    feedback = result.feedback()
    return (
        "VERIFICATION FAIL — the mission is NOT yet complete.\n\n"
        f"{feedback[:3000]}\n\n"
        "Address the gaps (the auditor quoted the evidence), then call mark_done "
        "again with an updated `reason` citing the new evidence."
    )


async def _adjudicate_uncertain(ctx: Any, result: Any) -> str:
    """Auditor returned UNCERTAIN → escalate to a human and HOLD (never auto-PASS).
    The human's typed answer is authoritative: an explicit accept → mission done;
    anything else → feed it back. No human present → gateway.ask holds with no
    timeout; if no gateway at all, return an UNCERTAIN message (don't pass)."""
    import re

    gateway = getattr(ctx, "human_assist", None)
    report = (getattr(result, "report", "") or result.feedback())[:1500]
    question = (
        "审计员拿不准 harvest 是否真的达标(很可能数据内容真伪 / 覆盖存疑)。它的判定:\n\n"
        f"{report}\n\n"
        "你来定:回复「通过」算完成;或说明哪里不对,让 agent 继续补。"
    )
    answer = ""
    if gateway is not None:
        try:
            resp = await gateway.ask(question=question, timeout_s=None)  # hold for human
            answer = (resp.message or "").strip() if resp else ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"UNCERTAIN escalation ask failed: {e}")
    if answer and re.search(r"通过|pass|accept|\bok\b|可以|没问题|算过", answer, re.IGNORECASE):
        ctx._mission_done = True
        return (
            "VERIFICATION PASS (human-adjudicated UNCERTAIN) — operator confirmed "
            f"the result is acceptable: {answer[:200]}"
        )
    note = answer or "(审计 UNCERTAIN,且无人裁定)"
    return (
        "VERIFICATION UNCERTAIN — the auditor could not establish completion and a "
        f"human weighed in.\n\n{result.feedback()[:2400]}\n\n[人工裁定] {note}\n\n"
        "Address the note and call mark_done again, or stop if truly blocked."
    )
