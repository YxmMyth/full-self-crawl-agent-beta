"""research — tactical research without leaving the session.

The kungal lesson (2026-06-03 forensics): research ability lived ONLY at the
Planner (spawn_research), so an execution agent holding an undecodable `.arc`
archive could not investigate "what decodes this" — the 5-hop round-trip
(bubble up → Planner researches → new briefing → new session) never fired,
and the agent satisficed in place with metadata.

This tool closes the loop where it breaks: the doer researches at the moment
it is stuck. The research runs in its OWN context (the same Research Subagent
the Planner spawns — web_search / web_fetch / bash / think), and only the
findings come back; the session's context stays clean. Full report lands in
research/ for deeper reading via bash.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.utils.logging import get_logger

logger = get_logger(__name__)

_FINDINGS_LIMIT = 4000  # chars of the report returned inline to the session

TOOL_NAME = "research"
TOOL_DESCRIPTION = (
    "Investigate something on the internet via a research subagent (web search "
    "+ page fetching, in its own context) and get back concrete findings.\n\n"
    "WHEN to use — you hit something the target site itself can't teach you, "
    "and the answer plausibly exists out there:\n"
    "  - an unknown binary/encoded format you need to decode (game-engine "
    "archives, proprietary dumps, exotic encodings) — research what it is and "
    "what tool decodes it\n"
    "  - an undocumented API, auth scheme, or anti-bot wall others have solved\n"
    "  - a third-party tool/library you need but don't know by name\n\n"
    "WHEN NOT to use: anything answerable by browsing the target site "
    "(browse/fetch), or by pure reasoning (think). One focused call beats "
    "three vague ones — pack your concrete questions in.\n\n"
    "Returns key findings inline (tool names, commands, URLs — actionable) "
    "plus the full report path (read it with bash if you need more). Apply "
    "the findings directly: install what it recommends (pip/apt), run it."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "topic": {
            "type": "string",
            "description": (
                "What to research, specific enough to search on. "
                "Good: 'SiglusEngine .arc archive extraction tools'. "
                "Bad: 'help with file'."
            ),
        },
        "questions": {
            "type": "string",
            "description": (
                "Concrete questions the findings must answer, one per line. "
                "E.g. 'what tool unpacks Scene.pck from SiglusEngine? "
                "pip-installable or needs build? exact command for one file?'. "
                "Defaults to the topic itself."
            ),
        },
    },
    "required": ["topic"],
    "additionalProperties": False,
}


async def handle(ctx: Any, **kwargs: Any) -> dict:
    raw_topic = kwargs.get("topic", "")
    topic = raw_topic.strip() if isinstance(raw_topic, str) else ""
    if not topic:
        return {"status": "error", "message": "topic is required and must be non-empty"}
    raw_q = kwargs.get("questions", "")
    questions = raw_q.strip() if isinstance(raw_q, str) else ""

    domain = getattr(ctx, "_domain", None)
    if not domain:
        return {
            "status": "error",
            "message": "ctx._domain missing — launcher bug, not your fault.",
        }

    # Fresh LLM client: the subagent's conversation must not share the
    # session's client state, and research is rare enough that a per-call
    # client costs nothing.
    from src.llm.client import LLMClient
    from src.research.agent import run_research

    logger.info(f"Tactical research spawned: {topic[:120]}")
    llm = LLMClient()
    try:
        result = await run_research(llm, domain, topic, questions or topic)
    except Exception as e:  # noqa: BLE001 — research failure must not kill the session
        logger.warning(f"Tactical research failed: {e}")
        return {
            "status": "error",
            "message": f"research subagent failed: {e}. You can retry once with a "
            "narrower topic, or proceed without it.",
        }
    finally:
        await llm.close()

    # Return the report body inline (truncated) — 500-char key_findings is too
    # thin to act on; the doer needs the commands/URLs now, not a pointer.
    report_path = result.get("report_path", "")
    findings = result.get("key_findings", "")
    try:
        full = Path(report_path).read_text(encoding="utf-8")
        findings = full[:_FINDINGS_LIMIT] + (
            f"\n... [truncated; full report: {report_path}]"
            if len(full) > _FINDINGS_LIMIT else ""
        )
    except Exception:
        pass

    return {
        "status": "completed",
        "findings": findings,
        "report_path": str(report_path),
        "next_step_hint": (
            "Apply the findings now: install the recommended tool (bash pip/apt), "
            "run it on your target, verify the output. Read the full report with "
            "bash if you need details beyond the excerpt."
        ),
    }
