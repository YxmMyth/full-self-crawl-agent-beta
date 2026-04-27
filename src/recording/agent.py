"""Recording Agent — singleton observer that maintains Observations.

Architecture: Producer-Consumer with asyncio.Queue.
- Execution sessions push transcript increments (producer)
- Recording agent processes them via LLM tool-use loop (consumer)
- Writes structured observations to DB

Same LLM loop pattern as execution agent, different input and tools.

See: 架构共识文档.md §7.4, docs/SystemPrompts设计.md §二
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from src.config import Config
from src.llm.client import LLMClient, LLMResponse
from src.recording.tools import build_recording_registry, recording_tool_schemas
from src.utils.logging import get_logger

logger = get_logger(__name__)

# ── System Prompt (from docs/SystemPrompts设计.md §二) ────

RECORDING_SYSTEM_PROMPT = """You are an observation specialist. You watch an execution agent explore \
a website and maintain a structured knowledge base (Observations) of \
what it discovers.

You receive transcript increments — the agent's tool calls, results, \
and reasoning. Your job: distill these into precise, reusable Observations.

## Your Tools (ONLY these 4)

You can call exactly 4 tools:
  - read_observations(location)
  - create_observation(location_id, raw)
  - edit_observation(observation_id, raw)
  - delete_observation(observation_id)

The transcript you read contains tool names like `browser_eval`, `bash`, \
`browse`, `click`, `read_network`, `scroll`, etc. These are the EXECUTION \
agent's tools — they are CONTENT you describe in observation text, NOT tools \
you can call. Calling them WILL FAIL. When you reference them, write them as \
quoted strings or wrap them in backticks; do not put them in tool_calls.

## What to Record

Record FINDINGS, not actions. Not "agent browsed the tag page" but:
- "Location /tag/{tag}: paginated list, 20 items per page, sorted by \
recency. Fields: title, author, views, likes, thumbnail."
- "API /api/v2/items?tag={tag}: returns JSON with same items + \
created_at, updated_at. Pagination via ?page=N."

Record RELATIONSHIPS between locations:
- "Tag page data is a subset of the API response"
- "Detail page /item/{id} has fields not in list view"

Record METHODS that worked (or failed):
- "Method: agent used `browser_eval` to extract 20 items from embedded JSON. \
Saved to samples/."
- "FAILED: API without auth returns 401."

(Notice these reference `browser_eval` as a NAMED METHOD in the description \
text, not as a tool you call.)

## How to Work

1. READ BEFORE WRITE. Always read_observations() for the relevant location \
before creating or editing. Avoid duplicates.

2. MERGE, DON'T DUPLICATE. If a new finding extends an existing observation, \
edit it. If it's genuinely new, create. If old info is superseded, delete.

3. ONE OBSERVATION = ONE FACT. Don't stuff multiple unrelated findings into \
one observation. Keep them atomic and location-specific.

4. USE THE AGENT'S OWN WORDS. When the agent reasons about patterns or \
relationships, capture that reasoning — it's often the most valuable part.

5. SKIP NOISE. Not every tool call deserves an observation. Navigation, \
failed clicks, retries — these are process, not knowledge. \
Record only what adds to understanding of the site.

## Output Quality

Good observation: specific, has evidence, reusable by future sessions.
Bad observation: vague ("this page has data"), redundant, or action-focused.

Ask yourself: if a new agent reads this observation with no other context, \
can it understand what's at this location and how to get the data?"""


# Tools whose results contain knowledge worth recording
_KNOWLEDGE_TOOLS = {"browse", "bash", "browser_eval", "read_network"}

# Microcompact: keep last N batches in full
_KEEP_RECENT_BATCHES = 3
_BATCH_CLEARED = "[transcript batch {n} — 已处理，产出 {obs_count} 条 observations]"


class RecordingAgent:
    """Singleton recording agent that processes execution transcripts into observations.

    Usage:
        agent = RecordingAgent(llm, domain)
        await agent.start()                        # start background consumer
        await agent.push_increment(session_id, ...) # push from session
        await agent.flush()                        # drain queue at session end
        await agent.stop()                         # cleanup
    """

    def __init__(self, llm: LLMClient, domain: str) -> None:
        self.llm = llm
        self.domain = domain
        self.registry = build_recording_registry(domain)
        self.tools_schema = recording_tool_schemas()

        # LLM conversation (persistent across all sessions)
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": RECORDING_SYSTEM_PROMPT},
        ]

        # Producer-Consumer
        self.queue: asyncio.Queue = asyncio.Queue()
        self._consumer_task: asyncio.Task | None = None
        self._batch_count = 0
        self._obs_created_per_batch: dict[int, int] = {}

    async def start(self) -> None:
        """Start the background consumer loop."""
        self._consumer_task = asyncio.create_task(self._consume_loop())
        logger.info("Recording Agent started", extra={"domain": self.domain})

    async def stop(self) -> None:
        """Stop the consumer loop."""
        if self._consumer_task and not self._consumer_task.done():
            await self.queue.put(None)  # sentinel
            await self._consumer_task
        logger.info("Recording Agent stopped")

    async def push_increment(
        self,
        session_id: str,
        assistant_content: str | None,
        tool_calls: list[dict[str, Any]],
    ) -> None:
        """Push a transcript increment from an execution session.

        Only pushes if the round contains knowledge-producing tool calls.

        Args:
            session_id: Which session this came from.
            assistant_content: LLM reasoning text (valuable observation data).
            tool_calls: List of {name, arguments, result} dicts from the round.
        """
        # Filter: only push if round has knowledge-producing tools
        knowledge_calls = [
            tc for tc in tool_calls
            if tc.get("name") in _KNOWLEDGE_TOOLS
        ]
        if not knowledge_calls and not assistant_content:
            return  # skip process-only rounds

        await self.queue.put({
            "session_id": session_id,
            "reasoning": assistant_content or "",
            "tool_calls": knowledge_calls,
        })

    async def flush(self, timeout: float = 60.0) -> None:
        """Wait for all pending increments to be processed."""
        # Put a sentinel and wait for the consumer to reach it
        done_event = asyncio.Event()

        async def _sentinel_handler():
            done_event.set()

        await self.queue.put(("_flush", done_event))

        try:
            await asyncio.wait_for(done_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Recording Agent flush timed out")

    # ── Consumer loop ────────────────────────────────────

    async def _consume_loop(self) -> None:
        """Background loop: pull increments, process via LLM tool-use."""
        while True:
            item = await self.queue.get()

            # Sentinel: stop
            if item is None:
                break

            # Flush sentinel: signal completion
            if isinstance(item, tuple) and item[0] == "_flush":
                item[1].set()  # set the done_event
                continue

            try:
                await self._process_increment(item)
            except Exception as e:
                logger.error(f"Recording Agent error processing increment: {e}")

            self.queue.task_done()

    async def _process_increment(self, increment: dict) -> None:
        """Process one transcript increment via LLM tool-use loop."""
        self._batch_count += 1
        batch_num = self._batch_count

        # Format the increment as a user message
        user_msg = self._format_increment(increment, batch_num)
        self.messages.append({"role": "user", "content": user_msg})

        # Apply microcompact — also trim self.messages to prevent memory growth
        compact_messages = self._apply_microcompact()
        self.messages = compact_messages.copy()

        # Tool-use loop: LLM may call multiple tools
        obs_count = 0
        max_turns = 5  # safety limit per increment

        for _ in range(max_turns):
            response = await self.llm.chat_with_tools(compact_messages, self.tools_schema)
            if response is None:
                break

            # Append assistant message
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if response.content:
                assistant_msg["content"] = response.content
            if response.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in response.tool_calls
                ]
            self.messages.append(assistant_msg)
            compact_messages.append(assistant_msg)

            # No tool calls → done processing this increment
            if not response.tool_calls:
                break

            # Execute tool calls
            for tc in response.tool_calls:
                result = await self._execute_tool(tc)
                if "create" in tc.name.lower():
                    obs_count += 1

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                }
                self.messages.append(tool_msg)
                compact_messages.append(tool_msg)

        self._obs_created_per_batch[batch_num] = obs_count
        logger.debug(
            f"Recording Agent processed batch {batch_num}: {obs_count} observations created",
        )

    async def _execute_tool(self, tc: Any) -> str:
        """Execute a recording tool call."""
        try:
            result = await self.registry.execute(tc.name, None, **tc.arguments)
            return str(result)
        except Exception as e:
            logger.warning(f"Recording tool {tc.name} error: {e}")
            return f"Error: {e}"

    # ── Increment formatting ─────────────────────────────

    @staticmethod
    def _format_increment(increment: dict, batch_num: int) -> str:
        """Format a transcript increment as a user message for the recording agent."""
        session_id = increment["session_id"]
        reasoning = increment.get("reasoning", "")
        tool_calls = increment.get("tool_calls", [])

        parts = [f"[Execution Update — session {session_id}, batch {batch_num}]"]

        if reasoning:
            parts.append(f"\nAgent reasoning:\n{reasoning[:500]}")

        for tc in tool_calls:
            name = tc.get("name", "?")
            args = tc.get("arguments", {})
            result = tc.get("result", "")

            # Truncate result for recording context (not the full multi-KB output)
            result_preview = result[:800] if isinstance(result, str) else str(result)[:800]

            args_str = json.dumps(args, ensure_ascii=False)[:200]
            parts.append(f"\n[{name}] {args_str}")
            parts.append(f"Result: {result_preview}")

        return "\n".join(parts)

    # ── Microcompact ─────────────────────────────────────

    def _apply_microcompact(self) -> list[dict[str, Any]]:
        """Compact the recording agent's message history.

        Key insight: Recording Agent's state lives in DB (observations),
        NOT in conversation history. Old conversation rounds are valueless
        because read_observations() always fetches the latest from DB.

        Strategy: keep system prompt + last 3 batches complete, discard the rest.
        Context stays bounded regardless of how many sessions run.
        """
        if len(self.messages) <= 1:
            return list(self.messages)

        # Find batch boundaries (user messages with "[Execution Update")
        batch_starts: list[int] = []
        for i, msg in enumerate(self.messages[1:], 1):
            if msg["role"] == "user" and "[Execution Update" in msg.get("content", ""):
                batch_starts.append(i)

        if not batch_starts:
            return list(self.messages)

        # Keep only the last N batches
        if len(batch_starts) <= _KEEP_RECENT_BATCHES:
            # Few enough batches — keep everything
            return list(self.messages)

        # Find where to cut: start of the (N+1)-th-from-end batch
        keep_from = batch_starts[-_KEEP_RECENT_BATCHES]

        result = [self.messages[0]]  # system prompt
        result.extend(self.messages[keep_from:])

        return result
