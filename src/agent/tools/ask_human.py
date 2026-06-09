"""ask_human — ask a human a question and get their typed-text answer.

Sibling to request_human_assist, sharing the same gateway/channel. The two are
different speech acts:

  - request_human_assist  → "go DO something (login / CAPTCHA / stuck)" → returns
    only a status enum; the page state likely changed → agent should re-browse.
  - ask_human             → "ANSWER my question in words" → returns the human's
    text; the page did NOT change → agent just reads the answer.

When to call:
  - You need a decision or clarification the human must express in words:
    an ambiguous requirement, "is this the right kind of sample?", a judgment
    call only the operator can make.
  - The auditor escalating an UNCERTAIN verdict.

When NOT to call:
  - Login pages / CAPTCHAs / stuck browser states → use request_human_assist.

Blocks until the human answers (or the request times out / is cancelled).
Thin wrapper → ctx.human_assist.ask() (the gateway implements the transport).
"""

from __future__ import annotations

from typing import Any

from src.config import Config

TOOL_NAME = "ask_human"
TOOL_DESCRIPTION = (
    "Ask a human a question and get their typed text answer back. Use this when "
    "you need a decision or clarification that the human must express in WORDS — "
    "an ambiguous requirement, 'is this the right kind of data?', a judgment "
    "call only the operator can make.\n\n"
    "This is NOT for login pages, CAPTCHAs, or stuck states where the human must "
    "operate the browser — use request_human_assist for those.\n\n"
    "The question is shown to the operator, who types a reply. You receive their "
    "text in the `answer` field. The browser/page state does NOT change, so you "
    "do NOT need to re-browse afterward — just read the answer and act on it.\n\n"
    "Make the question specific and self-contained — the operator may have no "
    "other context. If you want a constrained choice, say so in the question "
    "(e.g. 'reply A or B')."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": (
                "The question to ask, self-contained. Good examples:\n"
                "  - '需求说采「剧本」,你指玩家读到的真实对白,还是剧情梗概?'\n"
                "  - '贴了这几个样本的前几行,你看是真实对白还是简介?'\n"
                "Bad: 'help' / 'which one'(没有上下文,人看不懂)。"
            ),
        },
    },
    "required": ["question"],
    "additionalProperties": False,
}


async def handle(ctx: Any, **kwargs: Any) -> dict:
    raw_q = kwargs.get("question", "")
    question = raw_q.strip() if isinstance(raw_q, str) else ""
    if not question:
        return {
            "status": "error",
            "message": "question is required and must be a non-empty string",
        }

    gateway = getattr(ctx, "human_assist", None)
    if gateway is None:
        return {
            "status": "error",
            "message": (
                "human_assist gateway not configured in ToolContext. "
                "This is a runtime setup bug — report to the system."
            ),
        }

    # Auto-timeout so an UNATTENDED run doesn't hang forever waiting on an answer.
    # >0 → cap the wait; <=0 → wait forever (attended use). Shared with
    # request_human_assist (Config.HUMAN_ASSIST_TIMEOUT_S).
    timeout_s = Config.HUMAN_ASSIST_TIMEOUT_S if Config.HUMAN_ASSIST_TIMEOUT_S > 0 else None
    response = await gateway.ask(question=question, timeout_s=timeout_s)

    # Gateway returns HumanResponse(status, message). For ask, status=="completed"
    # means the human typed an answer (in message). Map to the agent-facing
    # vocabulary {answered, timeout, cancelled}.
    if response.status == "completed":
        return {
            "status": "answered",
            "answer": response.message,
            "next_step_hint": (
                "The human answered. Read `answer` and act on it. The page did "
                "not change — no need to re-browse."
            ),
        }
    if response.status == "timeout":
        return {
            "status": "timeout",
            "answer": None,
            "next_step_hint": (
                "No human answered within the timeout — this run is effectively "
                "unattended. Do NOT re-ask the same question. Proceed with your "
                "best-supported interpretation (and record the assumption), or if "
                "you genuinely cannot proceed without the answer, call "
                "mark_done(status='blocked', reason=...)."
            ),
        }
    # cancelled
    return {
        "status": "cancelled",
        "answer": None,
        "next_step_hint": (
            "The human dismissed the question without answering. Proceed with "
            "your best judgment, or call mark_done(status='blocked') if the "
            "answer was essential to proceed."
        ),
    }
