"""maintain_model — LLM function that updates Semantic + Procedural Models.

NOT an agent. Single LLM call: current models + new observations → new models.
Auto-triggered by Python code after each spawn_execution session ends.

See: docs/Planner设计.md §五, docs/SystemPrompts设计.md §六
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.config import Config
from src.llm.client import LLMClient
from src.utils.logging import get_logger
from src.world_model import db

logger = get_logger(__name__)

# ── Prompt (from docs/SystemPrompts设计.md §六) ──────────

_SYSTEM_PROMPT = """You update a site's knowledge models by incorporating new observations.

## Input

You receive:
- Current Semantic Model (site structure, data distribution, relationships)
- Current Procedural Model (extraction methods, access patterns, tools used)
- New observations from the latest session

## Task

Rewrite BOTH models to incorporate the new observations.

For the Semantic Model:
- Add newly discovered locations, data fields, relationships
- Update quantities, patterns, or structures that changed
- Resolve contradictions (new evidence supersedes old assumptions)
- Keep within ~8000 characters

For the Procedural Model:
- Add successful extraction methods with specifics (endpoint, params, script)
- Record failed approaches so they aren't retried
- Update access patterns (auth requirements, rate limits, pagination)
- Keep within ~6000 characters

## Rules

- MERGE, don't append. Rewrite the full model, integrating old and new.
- When space is tight, compress older/less important details.
  Recent findings and working methods get priority.
- Preserve specific numbers, URLs, and field names — these are
  high-value facts that can't be recovered from summaries.
- If new observations contradict the existing model, trust the
  new observations and note the change.

## Output

Return your output in EXACTLY this format:

===SEMANTIC_MODEL===
(full rewritten Semantic Model here)
===END_SEMANTIC===

===PROCEDURAL_MODEL===
(full rewritten Procedural Model here)
===END_PROCEDURAL===

===SESSION_SUMMARY===
(2-3 sentence summary of what this session discovered/accomplished)
===END_SUMMARY===

===MODEL_DIFF===
(brief list of what changed in each model)
===END_DIFF==="""


async def maintain_and_summarize(
    llm: LLMClient,
    domain: str,
    session_id: str,
) -> dict[str, Any]:
    """Update models and generate session summary.

    Called automatically after each execution session ends.
    NOT a tool — Python code triggers this.

    Returns:
        {summary, model_diff, new_obs_count, semantic_version, procedural_version}
    """
    # Load current models
    current_semantic, current_procedural = await db.load_both_models(domain)

    # Load new observations from this session's locations
    all_observations = await db.list_observations_by_domain(domain)
    new_obs_text = _format_observations(all_observations)
    new_obs_count = len(all_observations)

    # Build prompt
    prompt_parts = []

    if current_semantic:
        prompt_parts.append(f"## Current Semantic Model\n\n{current_semantic}")
    else:
        prompt_parts.append("## Current Semantic Model\n\n(empty — first session)")

    prompt_parts.append("")

    if current_procedural:
        prompt_parts.append(f"## Current Procedural Model\n\n{current_procedural}")
    else:
        prompt_parts.append("## Current Procedural Model\n\n(empty — first session)")

    prompt_parts.append(f"\n## New Observations ({new_obs_count} total)\n\n{new_obs_text}")

    user_prompt = "\n".join(prompt_parts)

    # Single LLM call
    logger.info(
        f"maintain_model: updating models for {domain} (session {session_id}, {new_obs_count} observations)",
        extra={"domain": domain, "session_id": session_id},
    )

    result = await llm.generate(user_prompt, system=_SYSTEM_PROMPT)
    if not result:
        logger.error("maintain_model: LLM returned empty")
        return {
            "summary": "Model update failed (empty LLM response)",
            "model_diff": "",
            "new_obs_count": new_obs_count,
        }

    # Parse structured output
    parsed = _parse_output(result)

    # Write models to DB
    if parsed["semantic"]:
        await db.upsert_model(domain, "semantic", parsed["semantic"])
        logger.info(f"Semantic Model updated ({len(parsed['semantic'])} chars)")

    if parsed["procedural"]:
        await db.upsert_model(domain, "procedural", parsed["procedural"])
        logger.info(f"Procedural Model updated ({len(parsed['procedural'])} chars)")

    return {
        "summary": parsed["summary"],
        "model_diff": parsed["diff"],
        "new_obs_count": new_obs_count,
    }


def _format_observations(observations: list) -> str:
    """Format observations for the maintain_model prompt."""
    if not observations:
        return "(no observations)"

    lines = []
    for obs in observations:
        loc = obs.location_id
        raw_str = json.dumps(obs.raw, ensure_ascii=False)
        # Truncate very long observations
        if len(raw_str) > 1000:
            raw_str = raw_str[:997] + "..."
        lines.append(f"[{loc}] {raw_str}")

    return "\n\n".join(lines)


def _parse_output(text: str) -> dict[str, str]:
    """Parse the structured output from the LLM."""
    def _extract(start_marker: str, end_marker: str) -> str:
        pattern = re.escape(start_marker) + r"\s*\n?(.*?)\n?\s*" + re.escape(end_marker)
        match = re.search(pattern, text, re.DOTALL)
        return match.group(1).strip() if match else ""

    return {
        "semantic": _extract("===SEMANTIC_MODEL===", "===END_SEMANTIC==="),
        "procedural": _extract("===PROCEDURAL_MODEL===", "===END_PROCEDURAL==="),
        "summary": _extract("===SESSION_SUMMARY===", "===END_SUMMARY==="),
        "diff": _extract("===MODEL_DIFF===", "===END_DIFF==="),
    }
