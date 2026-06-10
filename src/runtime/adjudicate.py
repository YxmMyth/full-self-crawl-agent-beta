"""Shared adjudication for UNCERTAIN audit results.

When an auditor returns UNCERTAIN, the system escalates to a human and holds.
The human's response is interpreted by an LLM (not regex) to decide: accept
the result as-is, or feed correction back to the worker.

Used by both recon (src/planner/recon_planner.py) and harvest
(src/agent/tools/mark_done.py) — extracted here to avoid duplication and
the regex false-positive bug (不可以 matching 可以).
"""

from __future__ import annotations

from typing import Any

from src.utils.logging import get_logger

logger = get_logger(__name__)

_INTERPRET_SYSTEM = (
    "审计员返回了 UNCERTAIN(拿不准),然后把审计报告展示给了人类,人类给了回复。\n"
    "判断人类的回复的语义:\n"
    "- 如果人类认为可以通过/接受(表达了通过、pass、ok、可以、没问题等肯定意思)→ 输出 ACCEPT\n"
    "- 如果人类给了修改意见、指出问题、要求继续补 → 输出 REJECT\n"
    "- 注意:「不可以」「不行」「不算过」是拒绝,不是通过\n"
    "只输出一个词:ACCEPT 或 REJECT"
)


async def adjudicate_uncertain(
    llm: Any,
    gateway: Any,
    report: str,
    context_label: str = "harvest",
) -> tuple[bool, str]:
    """Escalate an UNCERTAIN audit to a human.

    Args:
        llm: LLMClient for interpreting the human's reply.
        gateway: HumanAssistGateway for asking the human.
        report: The auditor's report text (truncated to ~1500 chars for display).
        context_label: "recon" or "harvest" for the question framing.

    Returns:
        (accepted, human_reply):
            accepted=True → the human said it's fine (caller should treat as PASS)
            accepted=False → the human gave feedback (caller should feed it back)
            human_reply is the raw text (empty string if no human answered)
    """
    report_excerpt = report[:1500] if report else "(no report)"
    question = (
        f"审计员拿不准 {context_label} 是否真的达标"
        f"(很可能{'样本种类 / 内容' if context_label == 'recon' else '数据内容真伪 / 覆盖'}存疑)。"
        f"它的判定:\n\n{report_excerpt}\n\n"
        "你来定:回复「通过」算过;或说明哪里不对,让 agent 继续补。"
    )

    answer = ""
    if gateway is not None:
        try:
            resp = await gateway.ask(
                question=question,
                timeout_s=None,  # hold forever — never auto-PASS
                options=["通过", "继续补"],
            )
            answer = (resp.message or "").strip() if resp else ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"UNCERTAIN escalation ask failed: {e}")

    if not answer:
        return False, ""

    # LLM interprets the human's reply semantically (no regex)
    prompt = (
        f"审计报告摘要:\n{report_excerpt[:500]}\n\n"
        f"人类的回复:\n{answer}\n\n"
        "判断:人类是说通过了,还是要继续补?"
    )
    result = await llm.generate(prompt=prompt, system=_INTERPRET_SYSTEM)
    accepted = bool(result and "ACCEPT" in result.upper())

    if accepted:
        logger.info(f"Human adjudicated UNCERTAIN as ACCEPT: {answer[:120]}")
    else:
        logger.info(f"Human adjudicated UNCERTAIN as REJECT: {answer[:120]}")

    return accepted, answer
