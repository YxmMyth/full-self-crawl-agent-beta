"""ReconPlanner — the strategic decision layer.

Top-level tool-use agent that manages reconnaissance of a target site.
5 tools: spawn_execution, spawn_research, read_model, think, mark_done.

Does NOT directly operate browser, maintain observations, or update models.

See: docs/Planner设计.md, docs/SystemPrompts设计.md §三
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from src.agent.session import AgentSession
from src.agent.tools.registry import ToolRegistry
from src.browser.context import ToolContext
from src.browser.manager import BrowserManager
from src.config import Config
from src.llm.client import LLMClient
from src.llm.maintain_model import maintain_and_summarize
from src.recording.agent import RecordingAgent
from src.research.agent import run_research
from src.utils.logging import get_logger
from src.verification.agent import run_verification
from src.world_model import db

logger = get_logger(__name__)

# ── System Prompt (from docs/SystemPrompts设计.md §三) ────

_SYSTEM_PROMPT = """You are a reconnaissance planner. You direct the systematic exploration \
of a target website to build understanding of its structure, data, and \
access methods.

You have 5 tools: spawn_execution, spawn_research, read_model, think, \
mark_done. You do NOT operate the browser, maintain observations, or \
update models — other components handle those automatically.

## Reconnaissance Stages

Reconnaissance progresses through four levels:
- L1 Site Structure: URL patterns and how they connect (broad exploration)
- L2 Data Distribution: what data exists at each pattern, format, volume
- L3 Requirement Mapping: which patterns serve the requirement
- L4 Sample Collection: extract samples to prove methods work

Gauge the current level by reading the Model. Early sessions need broad \
exploration (L1-L2). Later sessions target specific gaps (L3-L4).

## How to Decide

After each spawn_execution or spawn_research returns:
1. Read the returned summary and model_diff
2. think() — assess: what did we learn? what's still unknown?
3. If you need the full picture → read_model()
4. Decide next action:
   - More unknowns → spawn_execution with a focused briefing
   - Need external info (API docs, tech stack) → spawn_research
   - Model looks complete against requirement → mark_done

mark_done may be rejected with specific gaps. Address those gaps \
and continue.

## Writing Briefings

Your briefing to the execution agent should include:
- DIRECTION: what to explore or verify
- CONTEXT: relevant knowledge from the Model (the agent has no memory \
of previous sessions)
- STARTING POINT: a specific URL or approach
- COMPLETION CRITERIA: what counts as "done" for this session

## Principles

- DIRECT, DON'T MICROMANAGE. Set direction, not step-by-step instructions. \
The execution agent decides how to explore.
- REQUIREMENT EXPOSURE IS STAGED. L1-L2 briefings focus on site structure. \
L3-L4 briefings include requirement details. Don't reveal the full \
requirement too early — it biases exploration.
- PREFER DEPTH OVER BREADTH when the Model has obvious gaps at known \
locations. Prefer breadth when large parts of the site are unexplored.
- RESEARCH BEFORE GUESSING. If you don't know the site's technology, \
spawn_research to find out before sending the execution agent blindly.
- PRIOR RUNS ARE READ-ONLY. Other runs on this domain live at \
`artifacts/{domain}/runs/*/`. They have their own samples/, sessions/, \
verification/ etc., and their Models are accessible via \
read_model(run_id=...). Borrow context if useful, but you write only to \
your own run; you cannot modify other runs' artifacts."""


# ── Tool schemas ─────────────────────────────────────────

_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "spawn_execution",
            "description": (
                "Send the execution agent to explore the target site. "
                "Returns a summary of what was discovered and how the Model changed. "
                "The agent runs autonomously — just provide direction via the briefing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "briefing": {
                        "type": "string",
                        "description": "Natural language task: direction + context + starting point + completion criteria.",
                    },
                },
                "required": ["briefing"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_research",
            "description": (
                "Send a research agent to investigate via web search and HTTP. "
                "Use when you need external information: API docs, technology details, community knowledge."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Research topic."},
                    "questions": {"type": "string", "description": "Specific questions to answer."},
                },
                "required": ["topic", "questions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_model",
            "description": (
                "Read the Semantic + Procedural Model. "
                "No args = current run's Model. "
                "With run_id = read another run's Model (read-only borrow). "
                "Find available run_ids by listing artifacts/{domain}/runs/."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "Optional. Read another run's Model (read-only).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "think",
            "description": "Reason about strategy before deciding the next action.",
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
            "name": "mark_done",
            "description": (
                "Mark reconnaissance as complete. May be rejected if verification "
                "finds gaps — in that case, address the gaps and continue."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Why you believe reconnaissance is complete."},
                },
                "required": ["reason"],
            },
        },
    },
]

# Microcompact: tools whose results are large
_LARGE_RESULT_THRESHOLD = 2000
_RECENT_ROUNDS_KEEP = 5
_CLEARED_PLACEHOLDER = "[已清除，调 read_model 查看当前 Model]"


class ReconPlanner:
    """Top-level reconnaissance planner — manages the entire recon process."""

    def __init__(
        self,
        domain: str,
        requirement: str,
        llm: LLMClient,
        browser_manager: BrowserManager,
        recording_agent: RecordingAgent,
        execution_registry: ToolRegistry,
    ) -> None:
        self.domain = domain
        self.requirement = requirement
        self.llm = llm
        self.browser_manager = browser_manager
        self.recording_agent = recording_agent
        self.execution_registry = execution_registry

        # Planner's own conversation
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Domain: {domain}\nRequirement: {requirement}"},
        ]

        # Counters for safety net
        self.total_tool_calls = 0
        self.session_count = 0
        self.consecutive_same_tool: dict[str, int] = {}
        self._last_tool: str = ""
        self.round = 0

    async def run(self) -> dict[str, Any]:
        """Run the Planner loop until DONE, safety net, or context exhaustion."""
        logger.info(
            f"ReconPlanner started for {self.domain}",
            extra={"domain": self.domain},
        )

        outcome = "unknown"
        try:
            outcome = await self._main_loop()
        except Exception as e:
            logger.error(f"ReconPlanner crashed: {e}")
            outcome = "crash"

        logger.info(
            f"ReconPlanner finished: {outcome}, "
            f"{self.session_count} sessions, {self.total_tool_calls} tool calls",
            extra={"domain": self.domain, "outcome": outcome},
        )
        return {
            "outcome": outcome,
            "sessions": self.session_count,
            "total_tool_calls": self.total_tool_calls,
        }

    async def _main_loop(self) -> str:
        while True:
            self.round += 1

            # Microcompact
            compact = self._apply_microcompact()

            # LLM call
            response = await self.llm.chat_with_tools(compact, _TOOLS_SCHEMA)
            if response is None:
                return "context_exhausted"

            # Append assistant message (echoes reasoning_content for thinking models)
            self.messages.append(response.to_assistant_message())

            # No tool calls → done (shouldn't normally happen)
            if not response.tool_calls:
                return "natural_stop"

            # Execute tools
            for tc in response.tool_calls:
                self.total_tool_calls += 1

                # Safety net checks
                safety = self._check_safety_net(tc.name)
                if safety:
                    self.messages.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": f"SAFETY NET: {safety}. You must mark_done now.",
                    })
                    continue

                result = await self._execute_tool(tc.name, tc.arguments)

                self.messages.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
                })

                # Check for DONE
                if tc.name == "mark_done" and isinstance(result, str) and '"status": "DONE"' in result:
                    return "done"

            # Context size check
            total_chars = sum(len(m.get("content", "") or "") for m in self.messages)
            if total_chars > 400_000:
                return "context_exhausted"

    # ── Tool execution ───────────────────────────────────

    async def _execute_tool(self, name: str, args: dict) -> str:
        try:
            if name == "spawn_execution":
                return await self._spawn_execution(args.get("briefing", ""))
            elif name == "spawn_research":
                return await self._spawn_research(args.get("topic", ""), args.get("questions", ""))
            elif name == "read_model":
                return await self._read_model(args.get("run_id"))
            elif name == "think":
                return json.dumps({"thought": args.get("thought", "")})
            elif name == "mark_done":
                return await self._mark_done(args.get("reason", ""))
            else:
                return f"Unknown tool: {name}"
        except Exception as e:
            logger.error(f"Planner tool {name} error: {e}")
            return f"Error: {e}"

    async def _spawn_execution(self, briefing: str) -> str:
        """Full pipeline: session → recording → maintain_model → return summary."""
        self.session_count += 1
        session_id = f"s{self.session_count:03d}_{uuid.uuid4().hex[:6]}"

        logger.info(f"Spawning execution session {session_id}")

        # Create and run session
        session = AgentSession(
            session_id=session_id,
            run_id=f"run_{self.domain}",
            domain=self.domain,
            briefing=briefing,
            ctx=self.browser_manager.ctx,
            llm=self.llm,
            registry=self.execution_registry,
            browser_manager=self.browser_manager,
            recording_agent=self.recording_agent,
        )

        session_result = await session.run()

        # maintain_model: update Semantic + Procedural models
        model_result = await maintain_and_summarize(self.llm, self.domain, session_id)

        result = {
            "summary": model_result.get("summary", ""),
            "model_diff": model_result.get("model_diff", ""),
            "new_obs_count": model_result.get("new_obs_count", 0),
            "session_id": session_id,
            "session_outcome": session_result["outcome"],
            "session_steps": session_result["steps_taken"],
        }

        return json.dumps(result, ensure_ascii=False, indent=2)

    async def _spawn_research(self, topic: str, questions: str) -> str:
        result = await run_research(self.llm, self.domain, topic, questions)
        return json.dumps(result, ensure_ascii=False, indent=2)

    async def _read_model(self, run_id: str | None = None) -> str:
        semantic, procedural = await db.load_both_models(self.domain, run_id=run_id)
        result = {
            "run_id": run_id or Config.RUN_ID,
            "semantic_model": semantic or "(empty)",
            "procedural_model": procedural or "(empty)",
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    async def _mark_done(self, reason: str) -> str:
        if Config.VERIFICATION_SUBAGENT_ENABLED:
            verdict, gaps = await run_verification(
                self.llm, self.domain, self.requirement, reason,
            )
            if verdict == "PASS":
                return json.dumps({"status": "DONE"})
            else:
                return json.dumps({
                    "status": "blocked",
                    "verdict": verdict,
                    "gaps": gaps[:2000],
                    "message": "Verification found gaps. Address them and try again.",
                })
        else:
            return json.dumps({"status": "DONE"})

    # ── Microcompact ─────────────────────────────────────

    def _apply_microcompact(self) -> list[dict[str, Any]]:
        """Same strategy as execution agent: by actual result size."""
        if len(self.messages) <= 2:
            return list(self.messages)

        prefix = self.messages[:2]
        conversation = self.messages[2:]

        rounds: list[list[dict]] = []
        current: list[dict] = []
        for msg in conversation:
            if msg["role"] == "assistant":
                if current:
                    rounds.append(current)
                current = [msg]
            else:
                current.append(msg)
        if current:
            rounds.append(current)

        n = len(rounds)
        result = list(prefix)
        for i, round_msgs in enumerate(rounds):
            keep_full = (n - i) <= _RECENT_ROUNDS_KEEP
            for msg in round_msgs:
                if msg["role"] == "tool" and not keep_full:
                    content = msg.get("content", "")
                    if len(content) > _LARGE_RESULT_THRESHOLD:
                        result.append({**msg, "content": _CLEARED_PLACEHOLDER})
                        continue
                result.append(msg)

        return result

    # ── Safety net ───────────────────────────────────────

    def _check_safety_net(self, tool_name: str) -> str | None:
        if self.total_tool_calls >= Config.MAX_PLANNER_TOOL_CALLS:
            return f"Maximum tool calls ({Config.MAX_PLANNER_TOOL_CALLS}) reached"

        if tool_name == "spawn_execution" and self.session_count >= Config.MAX_SESSIONS:
            return f"Maximum sessions ({Config.MAX_SESSIONS}) reached"

        # Consecutive same tool
        if tool_name == self._last_tool:
            self.consecutive_same_tool[tool_name] = self.consecutive_same_tool.get(tool_name, 0) + 1
        else:
            self.consecutive_same_tool = {tool_name: 1}
        self._last_tool = tool_name

        if self.consecutive_same_tool.get(tool_name, 0) >= Config.MAX_CONSECUTIVE_SAME_TOOL:
            return f"Same tool ({tool_name}) called {Config.MAX_CONSECUTIVE_SAME_TOOL}+ times consecutively"

        return None
