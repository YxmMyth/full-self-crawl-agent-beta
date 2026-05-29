"""maintain_model — multi-step tool-use agent that updates models incrementally.

Replaces the single-LLM-call rewrite mode. Reasons:
  - Full rewrite per session telephone-games over many sessions → drift
  - Single call paraphrases technical syntax (e.g. textarea.value → element.innerHTML)
  - 121K input context dilutes attention on key obs

New model: spawn an agent with tools to read individual obs, read current
model text, apply patches (Codex DSL). Agent references obs ids instead of
paraphrasing technique — eliminates drift at the root.

Auto-triggered by Python code after each spawn_execution session ends.
Caller passes since_obs_id snapshot so the agent knows which obs ids are new.
Caller may pass compaction_mode=True (periodically) for a cleanup pass.

See: docs/SystemPrompts设计.md §六 (deprecated single-call mode)
"""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools.apply_patch import PatchParseError, apply_patch_to_text
from src.agent.tools.registry import ToolRegistry
from src.llm.client import LLMClient
from src.utils.logging import get_logger
from src.world_model import db

logger = get_logger(__name__)


# ── Prompts ──────────────────────────────────────────────


_MAINTAIN_SYSTEM_PROMPT = """You are a knowledge-base maintainer. You incrementally update two markdown files capturing what we know about a target site:

- `procedural.md` — how to extract data (methods, scripts, anti-bot, auth)
- `semantic.md` — what data exists (locations, fields, relationships)

After each crawl session, you receive the list of NEW observation ids
written by that session. Use your tools to update the two files to reflect
what was just learned.

## Core principles

1. **Reference, don't paraphrase technical detail.**
   When recording a method with a verbatim script in observations, write a
   short description + "see obs #N for verbatim". Do NOT rewrite the script
   in prose — LLMs paraphrase code WRONG systematically.

   GOOD:
     ### extract_pen_source [verified by obs #1065]
     Brief: query textareas, filter by .value length > 30000, double JSON.parse on result.
     See obs #1065 for verbatim browser_eval script.

   BAD:
     ### extract_pen_source
     Use document.getElementById('init-data').innerHTML and parse __item.html...
     (paraphrasing — likely wrong)

2. **Two sections in procedural.md.**
   - `## Verified Methods` — precious. Append-only unless replacing with stronger
     evidence. Every entry carries `[verified by obs #N]`.
   - `## Working Notes` — hypotheses, partial findings, can be consolidated or
     dropped later.

   New methods start in Working Notes. When a later obs verifies a Working
   Note method, MOVE it to Verified (delete from working, add to verified) in
   the same patch.

3. **Patch, don't rewrite.**
   Use `apply_patch` with `*** Update File: procedural.md` or `semantic.md`.
   Make MINIMAL edits with `@@ <context>` anchors. Don't rewrite a whole
   section when you only want to add one line.

4. **Add AND remove in the same patch when superseding.**
   When you add a method that supersedes an older hypothesis, also remove or
   mark the older one deprecated. Don't leave stale entries.

5. **If nothing significant changed, just mark_done.**
   No-op sessions are fine. Don't force a patch when there's genuinely no
   new information.

## Workflow

1. `read_model("procedural")` and `read_model("semantic")` to see current state.
2. Skim the new observation index (in your first user message). `read_observation(id)`
   for the ones carrying new technique / method / structure information. You
   don't need to read all of them.
3. Use `think` to plan if non-trivial.
4. `apply_patch(...)` with Codex DSL. Multiple patches per session are fine.
5. `mark_done(reason)` when both files reflect the new observations.

## apply_patch DSL reminder

```
*** Begin Patch
*** Update File: procedural.md
@@ ## Verified Methods
 ### existing_method [verified by obs #100]
 Brief desc here.
+
+### new_method [verified by obs #1234]
+Brief: <one sentence>.
+See obs #1234 for verbatim script.
*** End Patch
```

Use `@@ <context>` to anchor the section; ` ` (space) prefix for unchanged
context, `+` for added, `-` for removed.

Allowed Update File paths: `procedural.md`, `semantic.md` only.
Add File / Delete File / Move not supported here — both files always exist
(empty if first session)."""


_COMPACTION_SYSTEM_PROMPT = """You are doing periodic cleanup of the knowledge
base. Files `procedural.md` and `semantic.md` have accumulated entries over
multiple sessions and may have redundancy, stale hypotheses, or working notes
that should be promoted to verified.

## What to look for

1. **Working Notes that obs has since verified.** Promote to Verified Methods.
   Use `read_observation(id)` on the cited obs ids to confirm. Move the entry
   in the same patch (delete from Working, add to Verified).

2. **Superseded methods.** If a newer verified method replaces an older one,
   delete the older one.

3. **Redundant entries.** Multiple notes saying the same thing → keep the most
   precise one (usually the one with the most specific obs reference), drop
   the rest.

4. **Stale hypotheses.** Working Notes that haven't gained evidence in many
   sessions and aren't actionable → drop.

## Rules

- Use `apply_patch` to make minimal edits. Don't rewrite whole sections.
- Read obs before promoting Working Notes → Verified (verify the evidence is
  real, not just plausible-sounding).
- If everything looks clean, just `mark_done("no cleanup needed")`.
- Allowed Update File paths: `procedural.md`, `semantic.md`."""


# ── Tool schema (built once) ─────────────────────────────


_TOOLS_SCHEMA = [
    {
        "name": "read_observation",
        "description": (
            "Fetch one observation by id. Returns the full raw JSON — no "
            "truncation — so you can quote verbatim into model references "
            "(e.g., `see obs #N for verbatim browser_eval script`)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "Observation primary key."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "read_model",
        "description": "Read current text of one model file (procedural or semantic).",
        "parameters": {
            "type": "object",
            "properties": {
                "model_type": {"type": "string", "enum": ["procedural", "semantic"]},
            },
            "required": ["model_type"],
        },
    },
    {
        "name": "apply_patch",
        "description": (
            "Apply a Codex apply_patch DSL to model files. Allowed Update File "
            "paths: procedural.md, semantic.md only. Returns updated char counts "
            "on success or a parse/apply error message."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": (
                        "Patch text starting with '*** Begin Patch' and ending "
                        "with '*** End Patch'. Use real LF newlines."
                    ),
                },
            },
            "required": ["patch"],
        },
    },
    {
        "name": "think",
        "description": "Reason without side effects. Use to plan before complex patches.",
        "parameters": {
            "type": "object",
            "properties": {"thought": {"type": "string"}},
            "required": ["thought"],
        },
    },
    {
        "name": "mark_done",
        "description": "Signal maintenance complete. Ends the agent loop.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": [],
        },
    },
]

_ALLOWED_PATHS = {"procedural.md", "semantic.md"}
_MAX_STEPS = 30


# ── Agent loop ───────────────────────────────────────────


async def maintain_and_summarize(
    llm: LLMClient,
    domain: str,
    session_id: str,
    since_obs_id: int | None = None,
    compaction_mode: bool = False,
) -> dict[str, Any]:
    """Run the maintain agent. Replaces the old single-call rewrite.

    Args:
        since_obs_id: snapshot of max obs id BEFORE this session ran. Agent is
                      told "new obs ids are anything > since_obs_id". None →
                      treat all obs as new (first call).
        compaction_mode: if True, run periodic cleanup prompt instead of
                         incremental update prompt.

    Returns:
        {summary, model_diff, new_obs_count}
    """
    current_semantic = await db.load_model(domain, "semantic")
    current_procedural = await db.load_model(domain, "procedural")

    # In compaction mode, list ALL obs as "available for verification";
    # otherwise only obs newer than the snapshot.
    if compaction_mode:
        all_obs = await db.list_observations_by_domain(domain, run_id="*")
        new_obs = all_obs
    else:
        new_obs = await db.list_observations_since(domain, since_obs_id, run_id="*")
    new_obs_count = len(new_obs)

    state = {
        "semantic": current_semantic,
        "procedural": current_procedural,
        "edited": False,
        "done": False,
        "done_reason": "",
    }

    registry = _build_registry(state)

    new_ids_summary = _format_obs_index(new_obs)
    if compaction_mode:
        system_prompt = _COMPACTION_SYSTEM_PROMPT
        user_prompt = (
            f"Periodic cleanup round. {new_obs_count} observations exist under "
            f"this domain.\n\nObservation index (id + location):\n"
            f"{new_ids_summary}\n\nRead the current procedural.md and "
            f"semantic.md, decide what to consolidate / drop / promote, then "
            f"apply patches. mark_done when finished."
        )
    elif new_obs_count == 0:
        system_prompt = _MAINTAIN_SYSTEM_PROMPT
        user_prompt = (
            "No new observations this session (since_obs_id matches current "
            "max). Nothing to incorporate — call mark_done."
        )
    else:
        system_prompt = _MAINTAIN_SYSTEM_PROMPT
        user_prompt = (
            f"This session produced {new_obs_count} new observation(s). "
            f"Update procedural.md and semantic.md to incorporate them.\n\n"
            f"New observation index (id + location):\n{new_ids_summary}\n\n"
            f"Read the ones that look interesting, then patch. mark_done when "
            f"finished."
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    logger.info(
        f"maintain_model agent start: domain={domain}, session={session_id}, "
        f"new_obs={new_obs_count}, compaction={compaction_mode}"
    )

    steps = 0
    for _ in range(_MAX_STEPS):
        response = await llm.chat_with_tools(messages, registry.openai_schemas())
        if response is None:
            logger.warning("maintain_model: LLM returned empty")
            break

        messages.append(response.to_assistant_message())

        if not response.tool_calls:
            logger.info("maintain_model: agent stopped naturally (no tool calls)")
            break

        for tc in response.tool_calls:
            steps += 1
            try:
                result = await registry.execute(tc.name, None, **tc.arguments)
            except Exception as e:
                result = f"Tool {tc.name} crashed: {e}"
            content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": content,
            })

        if state["done"]:
            break

    if state["edited"]:
        await db.upsert_model(domain, "semantic", state["semantic"])
        await db.upsert_model(domain, "procedural", state["procedural"])
        logger.info(
            f"maintain_model: models updated "
            f"(sem={len(state['semantic'])} chars, "
            f"proc={len(state['procedural'])} chars, steps={steps})"
        )
    else:
        logger.info(f"maintain_model: no edits this round (steps={steps})")

    return {
        "summary": state["done_reason"] or "(no summary)",
        "model_diff": f"edited={state['edited']}, new_obs={new_obs_count}, steps={steps}",
        "new_obs_count": new_obs_count,
    }


# ── Tool registry construction ───────────────────────────


def _build_registry(state: dict[str, Any]) -> ToolRegistry:
    """Build a ToolRegistry with handlers closed over the agent's state dict."""

    async def read_observation_handler(_ctx, **kwargs):
        try:
            obs_id = int(kwargs.get("id", 0))
        except (TypeError, ValueError):
            return "Error: id must be an integer"
        obs = await db.get_observation_by_id(obs_id)
        if obs is None:
            return f"Error: observation #{obs_id} not found"
        return json.dumps({
            "id": obs.id,
            "location_id": obs.location_id,
            "agent_step": obs.agent_step,
            "raw": obs.raw,
        }, ensure_ascii=False)

    async def read_model_handler(_ctx, **kwargs):
        mt = kwargs.get("model_type", "")
        if mt == "procedural":
            return state["procedural"] or "(empty)"
        if mt == "semantic":
            return state["semantic"] or "(empty)"
        return f"Error: model_type must be 'procedural' or 'semantic', got {mt!r}"

    async def apply_patch_handler(_ctx, **kwargs):
        patch = kwargs.get("patch", "")
        if not patch.strip():
            return "Error: empty patch"
        files = {
            "procedural.md": state["procedural"] or "",
            "semantic.md": state["semantic"] or "",
        }
        try:
            new_files = apply_patch_to_text(files, patch, allowed_paths=_ALLOWED_PATHS)
        except PatchParseError as e:
            return f"Patch error: {e}"
        changes = []
        for path, attr in (("procedural.md", "procedural"), ("semantic.md", "semantic")):
            old = files[path]
            new = new_files.get(path, old)
            if new != old:
                state[attr] = new
                state["edited"] = True
                changes.append(f"{path}: {len(old)} -> {len(new)} chars")
        if not changes:
            return "Patch applied but produced no net change (no-op)"
        return "Patch applied:\n" + "\n".join(changes)

    async def think_handler(_ctx, **_kwargs):
        return "(thought recorded)"

    async def mark_done_handler(_ctx, **kwargs):
        state["done"] = True
        state["done_reason"] = kwargs.get("reason", "")
        return "(model maintenance complete)"

    handlers = {
        "read_observation": read_observation_handler,
        "read_model": read_model_handler,
        "apply_patch": apply_patch_handler,
        "think": think_handler,
        "mark_done": mark_done_handler,
    }

    registry = ToolRegistry()
    for tool_def in _TOOLS_SCHEMA:
        registry.register(
            tool_def["name"],
            tool_def["description"],
            tool_def["parameters"],
            handlers[tool_def["name"]],
        )
    return registry


# ── Helpers ──────────────────────────────────────────────


def _format_obs_index(obs_list) -> str:
    """One-line-per-obs index. Just id + location_id; raw fetched on demand."""
    if not obs_list:
        return "(none)"
    lines = []
    for o in obs_list[:200]:
        lines.append(f"  #{o.id} [{o.location_id}]")
    if len(obs_list) > 200:
        lines.append(f"  ... ({len(obs_list) - 200} more)")
    return "\n".join(lines)
