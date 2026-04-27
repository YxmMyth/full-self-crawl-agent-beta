"""Verification Subagent — DONE gatekeeper, anti-satisficing.

Triggered inside mark_done (not by Planner directly).
Feature-gated via VERIFICATION_SUBAGENT_ENABLED.

4 tools:
  - read_world_model, bash, think (investigation)
  - submit_verdict (terminal — only way to end the loop)

The submit_verdict tool is the explicit termination signal. This avoids the
"exploration vs conclusion" mode-switch failure mode where free-text VERDICT
formatting gets forgotten while the agent keeps calling investigation tools.

See: docs/工具重新设计共识.md §2.2c, docs/SystemPrompts设计.md §四
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.config import Config
from src.llm.client import LLMClient
from src.utils.logging import get_logger
from src.world_model import db

logger = get_logger(__name__)

_SYSTEM_PROMPT = """You are a verification specialist. Your job is to check whether \
reconnaissance is truly complete — or whether the planner is \
stopping too early.

You have 4 tools: read_world_model, bash, think, submit_verdict.

## What to Check

1. COVERAGE AGAINST REQUIREMENT.
   Read the requirement. Read the Model. For each aspect of the \
   requirement, is there concrete evidence in the Model? \
   "We found some data" is not enough — which specific parts of \
   the requirement are addressed, and which are not?

2. UNEXPLORED AREAS.
   Does the Model mention locations marked as "not yet explored" \
   or "quantity unknown"? Are there obvious follow-up paths that \
   were discovered but never investigated?

3. REAL SAMPLES, NOT METADATA RECORDS.
   For each data type discovered, there must be at least one Layer-3 sample \
   on disk (actual image bytes, archive files, extracted full text) — NOT \
   just JSON listings of pointers/URLs.
   Use bash to inspect:
     - List file types: `ls -la samples/` and `file samples/*` if available
     - Spot-check sizes: large binaries (>50KB images, archives) = real content
       Tiny files (<1KB) often = broken save_as or just URL strings
     - JSON files containing only `{name, slug, url}` arrays = INDEX records,
       not Layer-3 content. The mission needed to follow URLs and save bytes.
   A samples/ folder full of metadata-only JSON is INCOMPLETE. FAIL with a
   gap saying which data types have no real-bytes sample.

4. DEPTH VS SURFACE.
   Did the system actually understand the data, or just list pages? \
   A Model that says "this page has items" without field details, \
   access methods, or relationships is surface-level — not done.

## Rules

- The planner WANTS to stop. Your job is to find reasons it shouldn't.
- Focus on WHAT'S MISSING, not what's there.
- When in doubt, FAIL. One more session costs less than incomplete results.

## How to terminate

There is NO "natural stop". You MUST call `submit_verdict` to terminate.

Typical flow: 3-6 rounds of read_world_model / bash / think to gather \
evidence, then `submit_verdict(verdict, gaps, evidence)`.

- PASS: requirement satisfied, no blocking gaps.
- FAIL: significant gaps; one or more new sessions are needed.
- PARTIAL: gaps exist but core deliverables are acceptable.

When you are about to write 'VERDICT:' as text — STOP and call submit_verdict instead. \
Plain text verdicts are not parsed."""


# Tool schemas
_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "read_world_model",
            "description": "Read the World Model. No args = full Semantic + Procedural Model. With location = that location's observations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Location ID. Omit for full Model."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a command. Use to verify: list samples, check files, curl APIs, run scripts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "think",
            "description": "Reason about verification findings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought": {"type": "string", "description": "Your reasoning."},
                },
                "required": ["thought"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_verdict",
            "description": (
                "Terminate verification with your final judgment. "
                "This is the ONLY way to end the verification round — "
                "without calling this, the loop continues until max_rounds is hit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "verdict": {
                        "type": "string",
                        "enum": ["PASS", "FAIL", "PARTIAL"],
                        "description": (
                            "PASS: requirement met, no blocking gaps. "
                            "FAIL: significant gaps require more sessions. "
                            "PARTIAL: minor gaps but core deliverables acceptable."
                        ),
                    },
                    "gaps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Specific missing pieces, one per item. "
                            "Required for FAIL/PARTIAL. Empty list for PASS."
                        ),
                    },
                    "evidence": {
                        "type": "string",
                        "description": (
                            "Brief summary of what you actually verified — "
                            "reference specific files, Model sections, counts. "
                            "Supports your verdict."
                        ),
                    },
                },
                "required": ["verdict", "gaps", "evidence"],
            },
        },
    },
]

# Counter for verification rounds (report filenames)
_verification_round = 0


async def run_verification(
    llm: LLMClient,
    domain: str,
    requirement: str,
    mark_done_reason: str,
) -> tuple[str, str]:
    """Run the Verification Subagent.

    Returns:
        (verdict, feedback) — verdict is 'PASS', 'FAIL', or 'PARTIAL'.
        feedback is human-readable text (gaps + evidence) the Planner can
        feed back to itself for the next iteration.
    """
    global _verification_round
    _verification_round += 1
    round_num = _verification_round

    semantic, procedural = await db.load_both_models(domain)

    workspace = Config.run_dir(domain) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    user_msg = (
        f"## Requirement\n{requirement}\n\n"
        f"## Planner's reason for stopping\n{mark_done_reason}\n\n"
        f"## Current Semantic Model\n{semantic or '(empty)'}\n\n"
        f"## Current Procedural Model\n{procedural or '(empty)'}\n\n"
        f"Domain: {domain}\n"
        f"Artifacts directory: artifacts/{domain}/"
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    max_rounds = 12
    reasoning_chain: list[str] = []
    final_verdict: str | None = None
    final_gaps: list[str] = []
    final_evidence: str = ""
    nudged = False

    for round_idx in range(max_rounds):
        response = await llm.chat_with_tools(messages, _TOOLS_SCHEMA)
        if response is None:
            logger.warning(f"Verification round {round_num}: LLM returned None at round {round_idx + 1}")
            break

        if response.content:
            reasoning_chain.append(f"[round {round_idx + 1}]\n{response.content}")

        assistant_msg: dict[str, Any] = {"role": "assistant"}
        if response.content:
            assistant_msg["content"] = response.content
        if response.tool_calls:
            assistant_msg["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)}}
                for tc in response.tool_calls
            ]
        # OpenAI requires content or tool_calls — if neither, skip this turn
        if "content" not in assistant_msg and "tool_calls" not in assistant_msg:
            logger.warning(f"Verification round {round_num}: empty response at round {round_idx + 1}")
            break
        messages.append(assistant_msg)

        # Agent stopped without calling submit_verdict — nudge once, then break
        if not response.tool_calls:
            if not nudged:
                logger.info(f"Verification round {round_num}: agent stopped without submit_verdict, nudging")
                messages.append({
                    "role": "user",
                    "content": (
                        "You stopped without calling submit_verdict. The verification "
                        "round MUST end via submit_verdict. Either continue investigating "
                        "(read_world_model / bash / think) or call submit_verdict NOW."
                    ),
                })
                nudged = True
                continue
            logger.warning(f"Verification round {round_num}: agent stopped twice without verdict, breaking")
            break

        # Process all tool calls; capture submit_verdict if present
        submit_called = False
        for tc in response.tool_calls:
            if tc.name == "submit_verdict":
                args = tc.arguments or {}
                v = (args.get("verdict") or "").upper()
                if v in ("PASS", "FAIL", "PARTIAL"):
                    final_verdict = v
                    raw_gaps = args.get("gaps") or []
                    final_gaps = [str(g) for g in raw_gaps if str(g).strip()]
                    final_evidence = str(args.get("evidence") or "").strip()
                    submit_called = True
                    messages.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": "Verdict accepted. Verification round ending.",
                    })
                else:
                    messages.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": (
                            f"Invalid verdict '{v}'. Must be PASS, FAIL, or PARTIAL. "
                            f"Call submit_verdict again with a valid value."
                        ),
                    })
            elif tc.name == "read_world_model":
                from src.agent.tools.read_wm import handle as wm_handle
                class _Ctx:
                    _domain = domain
                try:
                    result = await wm_handle(_Ctx(), **tc.arguments)
                except Exception as e:
                    result = f"Error: {e}"
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
            elif tc.name == "bash":
                import asyncio as _asyncio
                try:
                    proc = await _asyncio.create_subprocess_shell(
                        tc.arguments.get("command", ""),
                        stdout=_asyncio.subprocess.PIPE,
                        stderr=_asyncio.subprocess.STDOUT,
                        cwd=str(workspace),
                    )
                    stdout, _ = await _asyncio.wait_for(proc.communicate(), timeout=30)
                    result = f"{stdout.decode('utf-8', errors='replace')}\n[exit code: {proc.returncode}]"
                except Exception as e:
                    result = f"Error: {e}"
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
            elif tc.name == "think":
                result = tc.arguments.get("thought", "")
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
            else:
                messages.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": f"Unknown tool: {tc.name}",
                })

        if submit_called:
            break

    # Decide final verdict
    if final_verdict is None:
        # NO_VERDICT — never called submit_verdict in max_rounds.
        # Treat as FAIL for caller (so Planner re-tries) but log distinctly.
        logger.warning(
            f"Verification round {round_num}: NO_VERDICT — agent never called "
            f"submit_verdict in {max_rounds} rounds. Treating as FAIL."
        )
        verdict = "FAIL"
        final_gaps = ["Verification agent did not produce a verdict (loop exhausted)."]
        final_evidence = "(no evidence produced)"
    else:
        verdict = final_verdict
        logger.info(f"Verification round {round_num}: {verdict}")

    # Build the report
    report_parts = [
        f"# Verification Round {round_num}",
        "",
        f"**Verdict:** {verdict}",
        "",
    ]
    if final_evidence:
        report_parts += ["## Evidence", "", final_evidence, ""]
    if final_gaps:
        report_parts += ["## Gaps", ""]
        report_parts += [f"- {g}" for g in final_gaps]
        report_parts += [""]
    if reasoning_chain:
        report_parts += ["## Reasoning Chain", "", "\n\n---\n\n".join(reasoning_chain), ""]

    report_text = "\n".join(report_parts)
    ver_dir = Config.run_dir(domain) / "verification"
    ver_dir.mkdir(parents=True, exist_ok=True)
    (ver_dir / f"round_{round_num}.md").write_text(report_text, encoding="utf-8")

    # Build feedback for Planner
    feedback_parts = [f"Verification {verdict}."]
    if final_gaps:
        feedback_parts.append("Gaps to address:")
        for g in final_gaps:
            feedback_parts.append(f"- {g}")
    if final_evidence:
        feedback_parts.append(f"Evidence reviewed: {final_evidence}")
    feedback = "\n".join(feedback_parts)

    return verdict, feedback
