"""Agent Session — the execution layer's core loop.

Receives a briefing from the Planner, runs a tool-use loop until natural
stop / context exhaustion / consecutive errors. Produces observations via
the Recording Agent (parallel), trace files for debugging, and a transcript.

See: docs/AgentSession设计.md, docs/SystemPrompts设计.md §一
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agent.tools.registry import ToolRegistry
from src.browser.context import ToolContext
from src.config import Config
from src.llm.client import LLMClient, LLMResponse
from src.utils.logging import get_logger
from src.world_model import db

logger = get_logger(__name__)


# ── System Prompt (from docs/SystemPrompts设计.md §一) ────

SYSTEM_PROMPT = """You are a web reconnaissance agent. You explore websites to understand \
their structure, discover data sources, and collect representative samples. \
You don't extract everything — you build understanding.

A Recording Agent works alongside you in real-time, capturing your actions \
and reasoning into the World Model. Think out loud — your reasoning is \
as valuable as your actions.

## How to Think

1. OBSERVE BEFORE ACTING. Always browse() first. Read the page content, \
Data Signals, and Network sections before deciding your next move.

2. FOLLOW THE DATA SIGNALS. browse() tells you what data sources exist:
   - Script tags with embedded JSON → browser_eval() to extract
   - API calls captured → read_network() for details, bash() curl to replay
   - Only rendered DOM → browser_eval() with selectors, last resort

3. BE SKEPTICAL OF NUMBERS. "1847 items" — verify it. Cross-check across \
sources. Does page count × items per page = stated total? Does the API \
total match the page display?

4. EXPLORE MULTIPLE PATHS TO THE SAME DATA. The same entity often appears \
via different routes (tag page, search, API, detail page). Map these \
paths — their differences are valuable intelligence.

5. NOTICE RELATIONSHIPS. When you discover a connection between locations, \
state it explicitly: "tag page links to detail pages", "API returns \
same data as page but with extra fields."

6. THINK BEFORE COMPLEX DECISIONS. Use think() when changing direction, \
when data patterns are unclear, or when comparing multiple findings.

7. WHEN STUCK, CHANGE APPROACH. If a method fails twice, try something \
different. Check read_world_model() for what's already been tried.

8. WHEN A HUMAN-ONLY GATE BLOCKS YOU, ASK FOR HELP. Login forms, CAPTCHAs \
(Turnstile/hCaptcha/FunCaptcha), 2FA/SMS/email codes, device verification \
— call request_human_assist(reason="<be specific>"). Do NOT try to fill \
credentials or solve puzzles yourself. After it returns, call browse() to \
re-observe — the tool does NOT auto-confirm "login successful". You judge \
from the new page state. Reserve this for true human-only gates, not for \
"I haven't found X yet" exploration or pages that just need scrolling.

9. PRIMARY DATA — FIRST SAMPLE FAST, THEN STOP. When the requirement asks \
for samples (Layer-3 primary data: zip / pdf / figma / image bytes / full \
text), the goal is ONE proven sample on disk EARLY, to validate the method \
works — before broadening.
   - To download files, use the `fetch` tool (it sends browser cookies & auth \
automatically). Don't loop on browser_eval+fetch in JS — that path can't \
write binaries to disk and is a dead end.
   - If `fetch` returns reason='auth_required' or 'got_html_likely_login_redirect', \
the path is RIGHT but the session needs login → browse() to verify, then \
request_human_assist if needed. Don't switch to a different product hoping \
auth will magically appear there.
   - If one path fails, try a DIFFERENT path (different URL discovered via \
browse, click the Download button and inspect, fetch the file via API). \
Method failure ≠ product failure.
   - Once one sample succeeds: STOP downloading. Verify (`bash`: `file ../samples/X`, \
`unzip -l ../samples/X` for archives, byte size > 1KB). Record the method \
in your reasoning. Sample 1-2 more products only to confirm the method generalizes, \
not to "complete coverage".
   - 100 metadata JSON files do NOT substitute for 1 binary sample. If you \
notice yourself describing a third product without a real sample on disk, \
stop and ask: "have I actually downloaded anything?"

## Data Hierarchy (what you're actually hunting)

Every site has a 3-layer data hierarchy. Recon means mapping ALL three. \
Most agents fail because they stop at Layer 1.

  Layer 1 — INDEX
    Lists, catalogs, search results, sitemaps, category pages, API listing endpoints.
    Each row is a POINTER to an entity: name + summary + IDs/URLs/file refs.
    For new domains, check `/sitemap.xml` and `/robots.txt` first — they often \
expose the entire URL inventory and API hints in one curl, much faster than crawling.

  Layer 2 — ENTITY
    The detail page for ONE thing: /products/{slug}, /article/{id}, etc.
    Full record, more fields, links to actual content/assets, download buttons.

  Layer 3 — CONTENT
    The actual bytes — image files, downloadable archives, full article text, \
media. The thing a real USER would consume.

**Critical: index records are NOT data. They are POINTERS to data.**

A listing record like `{name: "Apple Watch UI Kit", files: [{name: "kit.zip", size: 38MB}]}` \
tells you an entity exists and has a 38MB file SOMEWHERE — but not what's IN the file, \
and often not even the download URL (which may only appear on the detail page after a click).

If you only see Layer 1, you have a TABLE OF CONTENTS, not the book.

## Recon Completion Rule

First, identify the site's PRIMARY data — what would a real user come here \
to get? This determines what counts as "real samples":

  • News / blog → article full text (+ critical inline media)
  • UI Kit / template marketplace → the design files (Figma/Sketch/ZIP)
  • Image / stock asset library → image files themselves
  • Forum / Q&A → posts and replies
  • Video site → video files or full transcripts
  • E-commerce → product specs + reviews
  • Documentation → full doc content (markdown / rendered text)

That's Layer-3 PRIMARY content. Drill all the way down for AT LEAST ONE \
representative entity. Get the actual bytes/text to disk.

Secondary content (card thumbnails, preview images, blurbs, marketing copy, \
listing metadata) is supporting context — capture it if useful, but it \
does NOT substitute for primary data. A samples/ folder full of card \
thumbnails or listing JSON is **not done** when the user came for the \
underlying product.

Practical drill:
  • Found a list of entities? Open ≥ 1 detail page (Layer 2).
  • Detail page reveals access path to PRIMARY data (download URL, \
    embedded text, asset endpoint)? Follow it. Get the actual file/text \
    to `samples/{run_dir}/`. NO URL strings — bytes.
  • If the primary data is gated (pay / login / DRM), at least \
    document how the access mechanism works on one accessible representative \
    (e.g., one freebie / public sample).

You've validated a data access path only when you can show: \
"I traversed Layer 1 → 2 → 3 for at least one entity. The PRIMARY \
deliverable for this site is on disk in my run's samples/."

## Extraction Techniques (per page)

For any page you're extracting from, prefer:

1. EMBEDDED JSON (script tags, JSON-LD) — richest, one browser_eval() call.
2. API ENDPOINTS (from Network section / read_network()) — structured, paginated. \
   Replay with bash() curl to confirm.
3. DOM PARSING (browser_eval with selectors) — last resort, most fragile.

browse() Data Signals section shows which paths exist on the current page. \
Follow the signals.

## Boundaries

- Every site is different. Don't assume URL patterns or data schemas.
- 'Sample' means REAL DATA ON DISK — image bytes, file content, full text — \
**not** metadata records or URL strings. Listing JSON is NOT a sample.
- Mission is done when (a) briefing objectives are addressed AND \
(b) you have Layer-3 samples for every data type discovered."""


# ── Microcompact config ──────────────────────────────────

_RECENT_ROUNDS_KEEP = 5        # keep last N rounds' tool results complete
_LARGE_RESULT_THRESHOLD = 2000  # chars — results above this get cleared in old rounds
_CLEARED_PLACEHOLDER = "[已清除，调 read_world_model 查回]"


# ── Session outcomes ─────────────────────────────────────

OUTCOME_NATURAL = "natural_stop"
OUTCOME_CONTEXT = "context_exhausted"
OUTCOME_ERRORS = "consecutive_errors"
OUTCOME_SAFETY = "safety_net"


class AgentSession:
    """A single execution session — runs the tool-use loop until stop."""

    def __init__(
        self,
        session_id: str,
        run_id: str,
        domain: str,
        briefing: str,
        ctx: ToolContext,
        llm: LLMClient,
        registry: ToolRegistry,
        browser_manager: Any = None,
        recording_agent: Any = None,
    ) -> None:
        self.session_id = session_id
        self.run_id = run_id
        self.domain = domain
        self.briefing = briefing
        self.ctx = ctx
        self.llm = llm
        self.registry = registry
        self.recording_agent = recording_agent  # singleton RecordingAgent

        # Attach domain and browser_manager to ctx for tools that need them
        self.ctx._domain = domain
        if browser_manager:
            self.ctx._browser_manager = browser_manager

        # Message array — the agent's working memory
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": briefing},
        ]

        # State tracking
        self.step = 0
        self.round = 0  # API round counter (for microcompact grouping)
        self.outcome: str = OUTCOME_NATURAL
        self.consecutive_errors = 0
        self.tool_trajectory: list[str] = []

        # Observability
        self.trace_entries: list[dict] = []
        self.session_dir: Path | None = None
        self.started_at: datetime | None = None

        # Anomaly detection state
        self._last_tool_inputs: list[tuple[str, str]] = []

    async def run(self) -> dict[str, Any]:
        """Execute the session — main loop.

        Returns:
            {session_id, outcome, steps_taken, trajectory_summary}
        """
        self.started_at = datetime.now(timezone.utc)
        self._setup_session_dir()

        # Create DB record
        await db.create_session(
            self.session_id,
            run_id=self.run_id,
            direction=self.briefing[:200],
        )

        logger.info(
            f"Session {self.session_id} started",
            extra={"session_id": self.session_id, "domain": self.domain},
        )

        try:
            await self._main_loop()
        except Exception as e:
            logger.error(f"Session crashed: {e}", extra={"session_id": self.session_id})
            self.outcome = "crash"
        finally:
            ended_at = datetime.now(timezone.utc)
            trajectory = " → ".join(self.tool_trajectory[-50:])

            # Update DB
            await db.update_session(
                self.session_id,
                ended_at=ended_at,
                outcome=self.outcome,
                steps_taken=self.step,
                trajectory_summary=trajectory,
            )

            # Flush Recording Agent — ensure all observations are written
            if self.recording_agent:
                await self.recording_agent.flush()

            # Save observability artifacts
            self._save_summary(ended_at)
            self._save_transcript()

            duration = (ended_at - self.started_at).total_seconds()
            logger.info(
                f"Session {self.session_id} ended: {self.outcome}, "
                f"{self.step} steps, {duration:.1f}s",
                extra={"session_id": self.session_id, "outcome": self.outcome},
            )

        return {
            "session_id": self.session_id,
            "outcome": self.outcome,
            "steps_taken": self.step,
            "trajectory_summary": trajectory,
        }

    # ── Main loop ────────────────────────────────────────

    async def _main_loop(self) -> None:
        while True:
            self.round += 1

            # Apply microcompact before LLM call
            compact_messages = self._apply_microcompact()

            # Call LLM
            tools_schema = self.registry.openai_schemas()
            response = await self.llm.chat_with_tools(compact_messages, tools_schema)

            # Empty response → stop
            if response is None:
                logger.warning("LLM returned empty response")
                self.outcome = OUTCOME_CONTEXT
                break

            # Append assistant message
            assistant_msg = self._build_assistant_msg(response)
            self.messages.append(assistant_msg)

            # No tool calls → natural stop
            if not response.tool_calls:
                self.outcome = OUTCOME_NATURAL
                logger.info("Agent stopped naturally (no tool calls)")
                break

            # Execute each tool call
            for tc in response.tool_calls:
                self.step += 1
                self.tool_trajectory.append(tc.name)

                t0 = time.monotonic()
                result = await self._execute_tool(tc)
                elapsed_ms = int((time.monotonic() - t0) * 1000)

                # Append tool result to messages
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
                })

                # Record trace entry
                self._record_trace(tc, result, response, elapsed_ms)

                # Take screenshot
                await self._take_screenshot()

                # Anomaly detection
                self._check_anomalies(tc, result)

            # Push transcript increment to Recording Agent (non-blocking)
            await self._push_to_recording(response)

            # Check stop conditions
            if self.consecutive_errors >= 5:
                self.outcome = OUTCOME_ERRORS
                logger.warning(f"Stopping: {self.consecutive_errors} consecutive framework errors")
                break

            # Context exhaustion check (rough estimate)
            total_chars = sum(
                len(m.get("content", "") or "") for m in self.messages
            )
            if total_chars > 400_000:  # ~100K tokens rough estimate
                self.outcome = OUTCOME_CONTEXT
                logger.warning("Stopping: estimated context limit reached")
                break

    # ── Tool execution ───────────────────────────────────

    async def _execute_tool(self, tc: Any) -> str:
        """Execute a single tool call, handling errors."""
        try:
            result = await self.registry.execute(tc.name, self.ctx, **tc.arguments)
            self.consecutive_errors = 0  # reset on success
            if isinstance(result, dict):
                return json.dumps(result, ensure_ascii=False)
            return str(result)
        except KeyError as e:
            self.consecutive_errors += 1
            return f"Unknown tool: {tc.name}"
        except Exception as e:
            self.consecutive_errors += 1
            logger.error(
                f"Tool {tc.name} error: {e}",
                extra={"tool": tc.name, "step": self.step, "error": str(e)},
            )
            return f"Error executing {tc.name}: {e}"

    # ── Microcompact ─────────────────────────────────────

    def _apply_microcompact(self) -> list[dict[str, Any]]:
        """Create a compacted copy of messages for LLM input.

        Rules:
        - System + first user message: always keep
        - Group remaining messages by API round (assistant + tool results = 1 round)
        - Recent 5 rounds: keep all tool results complete
        - Older rounds: results >2000 chars → replace with placeholder
        - All assistant messages (with tool_calls): always keep complete
        """
        if len(self.messages) <= 2:
            return list(self.messages)

        # Split: system+briefing vs conversation
        prefix = self.messages[:2]
        conversation = self.messages[2:]

        # Group into rounds: each round = 1 assistant msg + its tool results
        rounds: list[list[dict]] = []
        current_round: list[dict] = []

        for msg in conversation:
            if msg["role"] == "assistant":
                if current_round:
                    rounds.append(current_round)
                current_round = [msg]
            else:
                current_round.append(msg)
        if current_round:
            rounds.append(current_round)

        # Apply compaction
        n_rounds = len(rounds)
        compacted = list(prefix)

        for i, round_msgs in enumerate(rounds):
            rounds_from_end = n_rounds - i
            keep_full = rounds_from_end <= _RECENT_ROUNDS_KEEP

            for msg in round_msgs:
                if msg["role"] == "assistant":
                    # Always keep assistant messages with tool_calls complete
                    compacted.append(msg)
                elif msg["role"] == "tool":
                    if keep_full:
                        compacted.append(msg)
                    else:
                        content = msg.get("content", "")
                        if len(content) > _LARGE_RESULT_THRESHOLD:
                            compacted.append({
                                **msg,
                                "content": _CLEARED_PLACEHOLDER,
                            })
                        else:
                            compacted.append(msg)
                else:
                    compacted.append(msg)

        return compacted

    # ── Message building ─────────────────────────────────

    @staticmethod
    def _build_assistant_msg(response: LLMResponse) -> dict[str, Any]:
        """Build an OpenAI-format assistant message from LLM response."""
        msg: dict[str, Any] = {"role": "assistant"}

        if response.content:
            msg["content"] = response.content

        if response.tool_calls:
            msg["tool_calls"] = [
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

        return msg

    # ── Observability ────────────────────────────────────

    def _setup_session_dir(self) -> None:
        """Create session output directory under this run."""
        base = Config.run_dir(self.domain)
        self.session_dir = base / "sessions" / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)
        (self.session_dir / "screenshots").mkdir(exist_ok=True)

    def _record_trace(
        self, tc: Any, result: str, response: LLMResponse, elapsed_ms: int,
    ) -> None:
        """Record one trace entry (TAO triple: Thought-Action-Observation)."""
        # Summarize output (first 200 chars)
        output_summary = result[:200] if isinstance(result, str) else str(result)[:200]

        entry = {
            "step": self.step,
            "ts": datetime.now(timezone.utc).isoformat(),
            "reasoning": response.content or "",
            "tool": tc.name,
            "input": tc.arguments,
            "output_summary": output_summary,
            "tokens_in": response.usage.prompt_tokens if response.usage else 0,
            "tokens_out": response.usage.completion_tokens if response.usage else 0,
            "latency_ms": elapsed_ms,
            "url": self.ctx.page.url if self.ctx.tabs else "",
            "error": None if self.consecutive_errors == 0 else output_summary,
        }
        self.trace_entries.append(entry)

        # Write to trace.jsonl immediately
        if self.session_dir:
            trace_path = self.session_dir / "trace.jsonl"
            with open(trace_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def _take_screenshot(self) -> None:
        """Take a screenshot of the current page state."""
        if not self.session_dir or not self.ctx.tabs:
            return
        try:
            path = self.session_dir / "screenshots" / f"step_{self.step:03d}.jpg"
            await self.ctx.page.screenshot(path=str(path), type="jpeg", quality=60)
        except Exception:
            pass  # screenshots are best-effort

    def _save_summary(self, ended_at: datetime) -> None:
        """Save summary.txt — human-readable one-line-per-step."""
        if not self.session_dir:
            return

        duration = (ended_at - self.started_at).total_seconds() if self.started_at else 0
        minutes = int(duration // 60)
        seconds = int(duration % 60)

        lines = [
            f"Session {self.session_id} | {self.step} steps | "
            f"outcome: {self.outcome} | {minutes}m{seconds:02d}s",
            "─" * 60,
        ]

        for entry in self.trace_entries:
            tool = entry["tool"]
            summary = entry["output_summary"][:60].replace("\n", " ")
            lines.append(f" {entry['step']:>2}. {tool:<15} {summary}")

        lines.append("")
        lines.append(f"Tools: {' → '.join(self.tool_trajectory)}")

        path = self.session_dir / "summary.txt"
        path.write_text("\n".join(lines), encoding="utf-8")

    def _save_transcript(self) -> None:
        """Save the complete message array as JSONL."""
        if not self.session_dir:
            return

        # Also save to a flat transcripts/ directory under this run
        transcripts_dir = Config.run_dir(self.domain) / "transcripts"
        transcripts_dir.mkdir(parents=True, exist_ok=True)

        for dest in [self.session_dir / "transcript.jsonl",
                     transcripts_dir / f"{self.session_id}.jsonl"]:
            with open(dest, "w", encoding="utf-8") as f:
                for msg in self.messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    # ── Recording Agent integration ────────────────────────

    async def _push_to_recording(self, response: LLMResponse) -> None:
        """Push this round's transcript increment to the Recording Agent."""
        if not self.recording_agent:
            return

        # Build tool call summaries for this round
        tool_calls_data = []
        for tc in response.tool_calls:
            # Find the corresponding tool result in messages
            result_content = ""
            for msg in reversed(self.messages):
                if msg.get("role") == "tool" and msg.get("tool_call_id") == tc.id:
                    result_content = msg.get("content", "")
                    break

            tool_calls_data.append({
                "name": tc.name,
                "arguments": tc.arguments,
                "result": result_content,
            })

        await self.recording_agent.push_increment(
            session_id=self.session_id,
            assistant_content=response.content,
            tool_calls=tool_calls_data,
        )

    # ── Anomaly detection ────────────────────────────────

    def _check_anomalies(self, tc: Any, result: str) -> None:
        """Simple loop and error detection."""
        # Track tool+input for loop detection
        input_sig = json.dumps(tc.arguments, sort_keys=True)
        self._last_tool_inputs.append((tc.name, input_sig))
        if len(self._last_tool_inputs) > 5:
            self._last_tool_inputs.pop(0)

        # Same tool+input 3+ times in a row
        if len(self._last_tool_inputs) >= 3:
            recent = self._last_tool_inputs[-3:]
            if all(x == recent[0] for x in recent):
                logger.warning(
                    f"LOOP_DETECTED: {tc.name} called 3+ times with same args",
                    extra={"tool": tc.name, "session_id": self.session_id},
                )

        # Track consecutive errors
        is_error = isinstance(result, str) and result.startswith("Error")
        if is_error:
            # Check if 3+ consecutive tool results are errors
            error_count = 0
            for entry in reversed(self.trace_entries):
                if entry.get("error"):
                    error_count += 1
                else:
                    break
            if error_count >= 3:
                logger.warning(
                    f"CONSECUTIVE_ERRORS: {error_count} tool errors in a row",
                    extra={"session_id": self.session_id},
                )
