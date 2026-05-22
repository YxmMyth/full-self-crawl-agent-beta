"""Harvest Verification Subagent — DONE gatekeeper for the harvest stage.

Triggered inside the `mark_done` tool (not by the agent directly).
Feature-gated via Config.VERIFICATION_SUBAGENT_ENABLED.

Same 4-tool shape as recon's verification (read_world_model, bash, think,
submit_verdict) — only the system prompt and user message differ. The
focus shifts from "is recon complete?" (WM coverage + samples present)
to "is harvest complete?" (workspace dataset count, shape, integrity,
reproducibility vs requirement boundary).

See: docs/工具重新设计共识.md §2.2c
"""

from __future__ import annotations

from typing import Any

from src.config import Config
from src.llm.client import LLMClient
from src.utils.logging import get_logger
from src.world_model import db

logger = get_logger(__name__)

_SYSTEM_PROMPT = """You are a harvest verification specialist. The harvest agent \
claims it has finished extracting the full dataset for the mission. Your job \
is to find out if that's true — or if it's satisficing.

You have 4 tools: read_world_model, bash, think, submit_verdict.

## What to check

1. COUNT COVERAGE.
   Read `../requirement.txt` to know the boundary. Use bash to count records \
   in workspace output files (e.g., `wc -l data/*.jsonl`, `ls -la data/`, \
   `jq -s 'length' data/records.jsonl`).
   Is the count in the expected range given the requirement? Significantly \
   short = FAIL. "I got some" is not done.

2. SHAPE INTEGRITY.
   Compare workspace output to `../samples/`. Recon's samples define the \
   field contract. Use bash to:
     - `head -1 data/records.jsonl` → compare field set vs a sample
     - `cat ../samples/<file>` → see what fields should be there
     - `jq -r 'keys[]' data/records.jsonl | sort -u` → all fields seen
   Output schema must match the sample schema. Missing fields = FAIL.

3. CONTENT INTEGRITY (random sampling).
   Pick 3-5 random records from the workspace output. Are they real content \
   or stub IDs / placeholder strings / login-redirect HTML?
     - `jq -c '.' data/records.jsonl | shuf -n 5` (or `head -5` if no shuf)
     - For binary samples: `file data/<sample>` — confirms real format
     - Sanity: content field non-empty, byte size > 1KB for binaries
   Stub records masquerading as data = FAIL.

4. REPRODUCIBILITY.
   Is there a `crawl.py` (or equivalent code) in the workspace? Can it \
   plausibly be rerun? Check it exists; if the run was expected to be \
   resumable (>500 records), look for state.json or similar.
   No crawler code, only hand-extracted data = PARTIAL at minimum.

5. SCOPE.
   The boundary is `requirement.txt`. Crawling extra unrelated data is \
   waste but not a failure. But if PRIMARY data is missing because the \
   agent got sidetracked, that's FAIL with a specific gap.

## Rules

- The agent WANTS to stop. Your job is to find reasons it shouldn't.
- "We have some data" is not done — does it match the requirement boundary?
- Workspace cwd from bash = `artifacts/{domain}/runs/{RUN_ID}/workspace/`.
- `../samples/` is recon's read-only sample dir (shape reference).
- `../requirement.txt` is the boundary spec (human-aligned).
- When in doubt, FAIL. One more harvest cycle costs less than incomplete results.

## How to terminate

There is NO "natural stop". You MUST call `submit_verdict` to terminate.

Typical flow: 3-6 rounds of read_world_model / bash / think to gather \
evidence, then `submit_verdict(verdict, gaps, evidence)`.

- PASS: full dataset present, shape matches samples, count satisfies boundary.
- FAIL: significant gaps; another harvest round needed.
- PARTIAL: dataset usable but with named limitations (e.g., 90% of records, \
  one field missing).

When you are about to write 'VERDICT:' as text — STOP and call submit_verdict instead. \
Plain text verdicts are not parsed."""


# Tool schemas — identical to recon verification for consistency.
_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "read_world_model",
            "description": "Read the World Model. No args = full model. With location = that location's observations.",
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
            "description": "Execute a command in workspace cwd. Use to list/count/inspect output files vs samples vs requirement.",
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
            "description": "Reason about verification findings before submitting verdict.",
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
                "This is the ONLY way to end the verification round."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "verdict": {
                        "type": "string",
                        "enum": ["PASS", "FAIL", "PARTIAL"],
                        "description": (
                            "PASS: full dataset present, shape + count + content all check out. "
                            "FAIL: significant gaps require another harvest cycle. "
                            "PARTIAL: usable but with named limitations."
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
                            "reference specific files, counts, sample comparisons."
                        ),
                    },
                },
                "required": ["verdict", "gaps", "evidence"],
            },
        },
    },
]

_harvest_verification_round = 0


async def run_harvest_verification(
    llm: LLMClient,
    domain: str,
    requirement: str,
    mark_done_reason: str,
) -> tuple[str, str]:
    """Run the harvest Verification Subagent.

    Returns:
        (verdict, feedback) — verdict is 'PASS', 'FAIL', or 'PARTIAL'.
        feedback is human-readable text (gaps + evidence) for the harvest
        agent's next round when the verdict is FAIL or PARTIAL.
    """
    global _harvest_verification_round
    _harvest_verification_round += 1
    round_num = _harvest_verification_round

    semantic, procedural = await db.load_both_models(domain)

    workspace = Config.run_dir(domain) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    user_msg = (
        f"## Requirement (boundary)\n{requirement}\n\n"
        f"## Harvest agent's reason for stopping\n{mark_done_reason}\n\n"
        f"## Recon Semantic Model\n{semantic or '(empty)'}\n\n"
        f"## Recon Procedural Model\n{procedural or '(empty)'}\n\n"
        f"Domain: {domain}\n"
        f"Workspace (cwd from bash): artifacts/{domain}/runs/{Config.RUN_ID}/workspace/\n"
        f"Samples dir (shape reference): ../samples/"
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
            logger.warning(f"Harvest verification round {round_num}: LLM returned None at iter {round_idx + 1}")
            break

        if response.content:
            reasoning_chain.append(f"[iter {round_idx + 1}]\n{response.content}")

        assistant_msg = response.to_assistant_message()
        if "content" not in assistant_msg and "tool_calls" not in assistant_msg:
            logger.warning(f"Harvest verification round {round_num}: empty response at iter {round_idx + 1}")
            break
        messages.append(assistant_msg)

        if not response.tool_calls:
            if not nudged:
                logger.info(f"Harvest verification round {round_num}: agent stopped without submit_verdict, nudging")
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
            logger.warning(f"Harvest verification round {round_num}: agent stopped twice without verdict, breaking")
            break

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
        logger.warning(
            f"Harvest verification round {round_num}: NO_VERDICT — agent never called "
            f"submit_verdict in {max_rounds} iters. Treating as FAIL."
        )
        verdict = "FAIL"
        final_gaps = ["Verification agent did not produce a verdict (loop exhausted)."]
        final_evidence = "(no evidence produced)"
    else:
        verdict = final_verdict
        logger.info(f"Harvest verification round {round_num}: {verdict}")

    # Build the report
    report_parts = [
        f"# Harvest Verification Round {round_num}",
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
    (ver_dir / f"harvest_round_{round_num}.md").write_text(report_text, encoding="utf-8")

    # Build feedback for the harvest agent
    feedback_parts = [f"Verification {verdict}."]
    if final_gaps:
        feedback_parts.append("Gaps to address:")
        for g in final_gaps:
            feedback_parts.append(f"- {g}")
    if final_evidence:
        feedback_parts.append(f"Evidence reviewed: {final_evidence}")
    feedback = "\n".join(feedback_parts)

    return verdict, feedback
