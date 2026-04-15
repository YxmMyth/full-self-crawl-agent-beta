"""Verification Subagent — DONE gatekeeper, anti-satisficing.

Triggered inside mark_done (not by Planner directly).
Feature-gated via VERIFICATION_SUBAGENT_ENABLED.
3 tools: read_world_model, bash, think. Read-only + execute.

See: docs/工具重新设计共识.md §2.2c, docs/SystemPrompts设计.md §四
"""

from __future__ import annotations

import json
import time
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

You have 3 tools: read_world_model, bash, think.

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

3. SAMPLES EXIST.
   Use bash to list artifacts/{domain}/samples/. Are there actual \
   files? A complete reconnaissance should have at least some \
   saved samples proving the methods work.

4. DEPTH VS SURFACE.
   Did the system actually understand the data, or just list pages? \
   A Model that says "this page has items" without field details, \
   access methods, or relationships is surface-level — not done.

## Rules

- The planner WANTS to stop. Your job is to find reasons it shouldn't.
- Focus on WHAT'S MISSING, not what's there.
- When in doubt, FAIL. One more session costs less than incomplete results.

## Output

For each gap found, state it clearly.

Last line (parsed by code):
VERDICT: PASS
VERDICT: FAIL
VERDICT: PARTIAL"""

# Counter for verification rounds (report filenames)
_verification_round = 0

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
]


async def run_verification(
    llm: LLMClient,
    domain: str,
    requirement: str,
    mark_done_reason: str,
) -> tuple[str, str]:
    """Run the Verification Subagent.

    Returns:
        (verdict, gaps) — verdict is 'PASS', 'FAIL', or 'PARTIAL'.
        gaps is the full verification report text.
    """
    global _verification_round
    _verification_round += 1
    round_num = _verification_round

    # Load current models for context
    semantic, procedural = await db.load_both_models(domain)

    workspace = Config.artifacts_for(domain) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # User message with all context
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

    max_rounds = 10
    final_content = ""

    for _ in range(max_rounds):
        response = await llm.chat_with_tools(messages, _TOOLS_SCHEMA)
        if response is None:
            break

        assistant_msg: dict[str, Any] = {"role": "assistant"}
        if response.content:
            assistant_msg["content"] = response.content
            final_content = response.content
        if response.tool_calls:
            assistant_msg["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)}}
                for tc in response.tool_calls
            ]
        messages.append(assistant_msg)

        if not response.tool_calls:
            break

        for tc in response.tool_calls:
            if tc.name == "read_world_model":
                from src.agent.tools.read_wm import handle as wm_handle

                class _Ctx:
                    _domain = domain
                result = await wm_handle(_Ctx(), **tc.arguments)
            elif tc.name == "bash":
                import asyncio
                try:
                    proc = await asyncio.create_subprocess_shell(
                        tc.arguments.get("command", ""),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        cwd=str(workspace),
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
                    result = f"{stdout.decode('utf-8', errors='replace')}\n[exit code: {proc.returncode}]"
                except Exception as e:
                    result = f"Error: {e}"
            elif tc.name == "think":
                result = tc.arguments.get("thought", "")
            else:
                result = f"Unknown tool: {tc.name}"

            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})

    # Parse verdict from last line
    verdict = _parse_verdict(final_content)

    # Save verification report
    ver_dir = Config.artifacts_for(domain) / "verification"
    ver_dir.mkdir(parents=True, exist_ok=True)
    report_path = ver_dir / f"round_{round_num}.md"
    report_path.write_text(final_content or "(no report)", encoding="utf-8")

    logger.info(
        f"Verification round {round_num}: {verdict}",
        extra={"domain": domain},
    )

    return verdict, final_content


def _parse_verdict(text: str) -> str:
    """Extract VERDICT from the last lines of the verification report."""
    if not text:
        return "FAIL"

    for line in reversed(text.strip().split("\n")):
        line = line.strip()
        if line.startswith("VERDICT:"):
            verdict = line.split(":", 1)[1].strip().upper()
            if verdict in ("PASS", "FAIL", "PARTIAL"):
                return verdict

    # No verdict found — default to FAIL (err on the side of caution)
    logger.warning("No VERDICT line found in verification report, defaulting to FAIL")
    return "FAIL"
