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
from src.verification.audit import run_audit
from src.world_model import db

logger = get_logger(__name__)

# ── System Prompt (from docs/SystemPrompts设计.md §三) ────

_SYSTEM_PROMPT = """You are a reconnaissance planner. You direct the systematic exploration \
of a target website to build understanding of its structure, data, and \
access methods.

You have 6 tools: spawn_execution, spawn_research, read_model, read_state, \
think, mark_done. You do NOT operate the browser, maintain observations, \
or update models — other components handle those automatically.

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
1. Read the returned summary, model_diff, AND state_delta. The state_delta \
is the FACT of what landed on disk this session — it overrides the LLM's \
narrative summary when the two disagree.
2. think() — assess: what did we learn? what's still unknown? what's \
already on disk vs what's still missing?
3. If you need the full picture → read_model() for understanding, or \
read_state() for the actual file inventory across samples/, catalog/, \
workspace/.
4. Decide next action:
   - More unknowns → spawn_execution with a focused briefing
   - Need external info (API docs, tech stack) → spawn_research
   - samples/ already has the deliverables for the requirement → mark_done. \
Do NOT spawn another execution to re-confirm what's already on disk.

## Completion Audit (mark_done discipline)

Calling mark_done is a CLAIM that the World Model + samples/catalog/scripts \
contain everything the harvest stage needs to take over without re-discovering \
basics. Treat that claim as unproven until you have verified it against the \
actual current state. Before calling mark_done(status='complete'):

- For each L1-L4 stage, identify the EVIDENCE in the Model that proves you \
reached it (a quote, a location count, a specific file under samples/).
- Treat indirect or uncertain evidence as not done — gather stronger \
evidence or spawn another session instead of marking complete.
- Do NOT mark complete merely because budget feels nearly exhausted, because \
you're stopping work, or because the planner narrative reads plausibly. \
mark_done is verified, not narrated.
- Do NOT redefine success around what already exists. If the requirement \
names data you haven't located, you are not done — keep working.

mark_done(status='complete') triggers a two-phase audit:
  Phase 1 — mechanical sanity (semantic.md > 200 chars, procedural.md > 200 \
chars, locations >= 3, samples/+catalog/ >= 1 file). Fast fail on empty WM.
  Phase 2 — LLM evidence-by-evidence check of L1/L2/L3/L4/Scalability.

If the audit blocks you, the specific gaps come back — address them and \
call mark_done again. If you are truly at an impasse and cannot make \
meaningful progress without operator input or an external state change \
(same blocker recurred 3+ goal rounds), call \
mark_done(status='blocked', reason='...') instead. Never use 'blocked' \
because the work is hard, slow, or uncertain.

## Writing Briefings

Your briefing to the execution agent should include:
- DIRECTION: what to explore or verify
- CONTEXT: relevant knowledge from the Model (the agent has no memory \
of previous sessions)
- STARTING POINT: a specific URL or approach
- COMPLETION CRITERIA: what counts as "done" for this session

## Universe is part of recon's deliverable

Recon's output to harvest is THREE things, not two:
  1. World Model (semantic + procedural) — how the site works
  2. samples/ — sketch primary data proving methods work
  3. catalog/ — the **enumerable universe** of every entity matching \
     the requirement scope (the list harvest must cover)

Without (3), harvest cannot verify "all of it" — it doesn't know what \
the universe is. Your briefings must explicitly direct an execution \
session to enumerate the universe to catalog/ before you call mark_done. \
This often takes a dedicated session: "Enumerate every pen ID under \
the webgl tag listing — paginate to the end via cursor, save to \
catalog/webgl_pens.jsonl."

If the universe is unbounded (infinite feed, no total, no exhaustion \
method), recon agent should document the boundedness in procedural \
model. Strategy report will surface it to the human gate.

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
read_model(run_id=...). You write only to your own run; you cannot modify \
other runs' artifacts.

- SESSION 0 PROTOCOL (when prior runs exist). Before your first \
spawn_execution, scan the "Prior runs of this domain" list in the user \
message and call read_model(run_id=...) on the prior runs that look \
relevant to the CURRENT requirement. Absorb URL patterns, methods that \
worked, known auth requirements, and known dead-ends. Skip details \
specific to past requirements (sample lists, progress numbers, item IDs). \
Bake the relevant prior knowledge into your first briefing's CONTEXT \
section so the execution agent doesn't waste rounds re-discovering what's \
already known. If no prior runs exist, skip this protocol."""


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
            "name": "read_state",
            "description": (
                "List actual artifact files on disk — facts, not LLM summaries.\n\n"
                "Returns size + mtime + filename for each file under the run's "
                "samples/, catalog/, workspace/. Use this to ground decisions in "
                "what's REALLY there: how many primary samples landed, what "
                "listings already exist, what debug dumps accumulated.\n\n"
                "kind='all' (default): brief overview of all three dirs (top 10 each)\n"
                "kind='samples'|'catalog'|'workspace': detailed listing of one dir (top 50)\n\n"
                "Use before deciding what to spawn next. If samples/ already has the "
                "deliverables you'd need to mark_done, don't spawn another execution."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["all", "samples", "catalog", "workspace"],
                        "description": "Which dir to list. Default 'all'.",
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
                "Terminate reconnaissance. Two valid statuses:\n"
                "  - 'complete': you believe the World Model + samples/catalog "
                "satisfy the requirement. Triggers a two-phase audit "
                "(mechanical sanity + LLM evidence-by-evidence check of L1-L4 "
                "stages + scalability). PASS → recon ends. BLOCKED → "
                "specific gaps returned, you continue.\n"
                "  - 'blocked': you are at a true impasse and cannot make "
                "meaningful progress without operator input or an external "
                "state change. Use sparingly — only when the same blocker has "
                "recurred for 3+ goal rounds. NEVER use 'blocked' merely "
                "because the work is slow, uncertain, or incomplete."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["complete", "blocked"],
                        "description": (
                            "'complete' = objective achieved (will be audited). "
                            "'blocked' = at impasse, need external help."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "For 'complete': what concretely got done — quote "
                            "evidence (samples/X has Y files, semantic.md says Z, "
                            "scripts/foo.py replicates the extraction). "
                            "For 'blocked': what specifically you cannot move "
                            "past and why."
                        ),
                    },
                },
                "required": ["status", "reason"],
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

        # Inject prior runs' run_id list (cross-run inheritance entrypoint).
        # Planner's prompt has a Session-0 Protocol that tells it to read the
        # relevant ones via read_model(run_id=...).
        await self._inject_prior_runs_menu()

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

                # Check for DONE — parse the JSON result rather than substring
                # so 'audit_blocked' can't accidentally match 'DONE'.
                if tc.name == "mark_done" and isinstance(result, str):
                    try:
                        parsed = json.loads(result)
                        if parsed.get("status") == "DONE":
                            return f"done:{parsed.get('outcome', 'complete')}"
                    except json.JSONDecodeError:
                        pass

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
            elif name == "read_state":
                return self._read_state(args.get("kind", "all"))
            elif name == "think":
                return json.dumps({"thought": args.get("thought", "")})
            elif name == "mark_done":
                return await self._mark_done(
                    args.get("status", "complete"),
                    args.get("reason", ""),
                )
            else:
                return f"Unknown tool: {name}"
        except Exception as e:
            logger.error(f"Planner tool {name} error: {e}")
            return f"Error: {e}"

    async def _spawn_execution(self, briefing: str) -> str:
        """Full pipeline: session → recording → maintain_model → return summary.

        Captures artifact filesystem state before and after the session, and
        includes a `state_delta` block in the result so the Planner sees the
        FACT of what landed on disk — not just the LLM's narrative summary.
        This breaks the "Planner only sees LLM-described progress" problem.
        """
        self.session_count += 1
        session_id = f"s{self.session_count:03d}_{uuid.uuid4().hex[:6]}"

        logger.info(f"Spawning execution session {session_id}")

        # Snapshot artifact dirs + max obs id BEFORE the session runs.
        # The obs-id snapshot lets maintain_model see exactly which obs ids
        # this session produced (id > snapshot) — required for incremental
        # patch mode instead of full rewrite of the procedural/semantic model.
        before = self._snapshot_artifacts()
        since_obs_id = await db.max_observation_id_for_domain(self.domain)

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

        # maintain_model: incremental update of Semantic + Procedural via tool-use agent.
        # Compaction round every 5 sessions to consolidate Working Notes → Verified
        # and drop stale entries (otherwise model file grows unbounded under
        # incremental-only mode).
        is_compaction = self.session_count > 0 and self.session_count % 5 == 0
        model_result = await maintain_and_summarize(
            self.llm,
            self.domain,
            session_id,
            since_obs_id=since_obs_id,
            compaction_mode=is_compaction,
        )

        # Snapshot AFTER and diff
        after = self._snapshot_artifacts()
        delta = self._diff_artifacts(before, after)

        result = {
            "summary": model_result.get("summary", ""),
            "model_diff": model_result.get("model_diff", ""),
            "new_obs_count": model_result.get("new_obs_count", 0),
            "session_id": session_id,
            "session_outcome": session_result["outcome"],
            "session_steps": session_result["steps_taken"],
            "state_delta": delta,
        }

        return json.dumps(result, ensure_ascii=False, indent=2)

    async def _spawn_research(self, topic: str, questions: str) -> str:
        result = await run_research(self.llm, self.domain, topic, questions)
        return json.dumps(result, ensure_ascii=False, indent=2)

    # ── Artifact state visibility (samples/ catalog/ workspace/) ──

    _ARTIFACT_DIRS = ("samples", "catalog", "workspace")

    def _snapshot_artifacts(self) -> dict[str, dict[str, dict]]:
        """Snapshot the run's artifact dirs as {dir: {filename: {size, mtime}}}.

        Plain os.listdir + stat. No recursion (top-level only — agents
        normally save flat). Cheap to call before/after each session.
        """
        run_dir = Config.run_dir(self.domain)
        snap: dict[str, dict[str, dict]] = {}
        for d in self._ARTIFACT_DIRS:
            entry: dict[str, dict] = {}
            full = run_dir / d
            if full.exists() and full.is_dir():
                for p in full.iterdir():
                    if p.is_file():
                        try:
                            st = p.stat()
                            entry[p.name] = {"size": st.st_size, "mtime": st.st_mtime}
                        except Exception:
                            pass
            snap[d] = entry
        return snap

    @staticmethod
    def _human_size(n: int) -> str:
        if n < 1024:
            return f"{n}B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f}KB"
        if n < 1024**3:
            return f"{n / (1024**2):.1f}MB"
        return f"{n / (1024**3):.2f}GB"

    def _diff_artifacts(
        self,
        before: dict[str, dict[str, dict]],
        after: dict[str, dict[str, dict]],
    ) -> dict[str, Any]:
        """Compute what was added per dir during a session.

        Returns a dict with per-dir 'added' filename lists and 'totals_now'.
        Modifications (same name, larger size) count as added too — agents
        sometimes overwrite a file across sessions.
        """
        delta: dict[str, Any] = {"totals_now": {}}
        for d in self._ARTIFACT_DIRS:
            b = before.get(d, {})
            a = after.get(d, {})
            added: list[str] = []
            for name, meta in a.items():
                if name not in b or b[name].get("size") != meta.get("size"):
                    added.append(f"{name} ({self._human_size(meta['size'])})")
            # Stable order: by mtime desc
            added.sort(key=lambda s: a.get(s.split(" (", 1)[0], {}).get("mtime", 0), reverse=True)
            delta[f"{d}_added"] = added
            delta["totals_now"][d] = len(a)
        return delta

    def _read_state(self, kind: str = "all") -> str:
        """Render artifact filesystem state as text for the Planner.

        Output format inspired by Claude Code's GlobTool: relative names,
        sizes, mtime — no raw bytes, no recursion. Caps:
          - 'all' mode shows up to 10 newest files per dir + totals
          - per-dir mode shows up to 50 newest files
        """
        kind = (kind or "all").strip().lower()
        if kind not in ("all", "samples", "catalog", "workspace"):
            return f"Error: kind must be one of all/samples/catalog/workspace, got '{kind}'."

        snap = self._snapshot_artifacts()

        def fmt_dir(d: str, cap: int) -> str:
            entries = snap.get(d, {})
            count = len(entries)
            total_bytes = sum(m.get("size", 0) for m in entries.values())
            header = f"=== {d}/ ({count} file{'s' if count != 1 else ''}, {self._human_size(total_bytes)}) ==="
            if not entries:
                return f"{header}\n  (empty)"
            # Sort by mtime desc
            sorted_items = sorted(entries.items(), key=lambda kv: kv[1].get("mtime", 0), reverse=True)
            lines = [header]
            shown = sorted_items[:cap]
            # Compute mtime relative format
            from datetime import datetime
            for name, meta in shown:
                size = self._human_size(meta.get("size", 0))
                try:
                    ts = datetime.fromtimestamp(meta.get("mtime", 0)).strftime("%H:%M")
                except Exception:
                    ts = "?"
                lines.append(f"  {size:>8}  {ts}  {name}")
            if count > cap:
                lines.append(f"  ... ({count - cap} more — call read_state(kind='{d}') for full list)")
            return "\n".join(lines)

        if kind == "all":
            return "\n".join(fmt_dir(d, cap=10) for d in self._ARTIFACT_DIRS)
        return fmt_dir(kind, cap=50)

    async def _inject_prior_runs_menu(self) -> None:
        """Append a list of prior run_ids to the initial user message.

        Cross-run inheritance entrypoint. The Planner's prompt instructs it
        (via the SESSION 0 PROTOCOL line) to read the relevant ones via
        read_model(run_id=...). We only provide the list — the Planner LLM
        decides which prior runs are worth reading for the current
        requirement.
        """
        try:
            prior_runs = await db.list_runs(self.domain)
        except Exception as e:
            logger.warning(f"Could not list prior runs: {e}")
            return
        # Exclude self
        others = [r for r in prior_runs if r.get("run_id") and r["run_id"] != Config.RUN_ID]
        if not others:
            return
        # Cap at 5 most recent (already sorted DESC by last_obs)
        others = others[:5]
        lines: list[str] = []
        for r in others:
            last = r.get("last_obs")
            last_str = last.isoformat(timespec="minutes") if last else "(no observations)"
            tag = " [has_models]" if r.get("has_models") else ""
            lines.append(f"  - {r['run_id']}  (last touched: {last_str}){tag}")
        block = (
            "\n\n## Prior runs of this domain (read-only)\n"
            + "\n".join(lines)
        )
        # Append to existing user message (index 1; index 0 is system)
        if len(self.messages) >= 2 and self.messages[1].get("role") == "user":
            self.messages[1]["content"] = self.messages[1]["content"] + block
        logger.info(f"Injected {len(others)} prior runs into Planner initial context")

    async def _read_model(self, run_id: str | None = None) -> str:
        semantic, procedural = await db.load_both_models(self.domain, run_id=run_id)
        result = {
            "run_id": run_id or Config.RUN_ID,
            "semantic_model": semantic or "(empty)",
            "procedural_model": procedural or "(empty)",
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    async def _mark_done(self, status: str, reason: str) -> str:
        status = (status or "complete").strip().lower()

        # 'blocked' is the explicit "I'm at an impasse" escape hatch — borrow
        # from codex /goal. Recon ends without claiming success.
        if status == "blocked":
            logger.warning(f"Planner declared BLOCKED: {reason[:200]}")
            await self._emit_strategy_report(audit_result=None, blocked_reason=reason)
            return json.dumps({
                "status": "DONE",
                "outcome": "blocked",
                "reason": reason,
            })

        # 'complete' (default) — run the two-phase audit (mechanical → auditor agent).
        result = await run_audit(
            self.llm, self.domain, self.requirement, reason,
        )
        if result.overall == "PASS":
            await self._emit_strategy_report(audit_result=result, blocked_reason=None)
            return json.dumps({"status": "DONE", "outcome": "complete"})

        if result.overall == "UNCERTAIN":
            # The auditor genuinely cannot tell. HOLD for a human (never guess
            # PASS) — an unprovable result must not slip through.
            return await self._adjudicate_uncertain(result)

        # FAIL — feed the specific gaps back to the planner to fix.
        return json.dumps({
            "status": "audit_blocked",
            "phase": result.phase,
            "feedback": result.feedback()[:3000],
            "message": (
                "Audit found gaps. Address them and call mark_done again, or use "
                "mark_done(status='blocked', ...) if you're truly at an impasse."
            ),
        })

    async def _adjudicate_uncertain(self, result: Any) -> str:
        """Auditor returned UNCERTAIN → escalate to a human and HOLD. The human's
        typed answer is authoritative: an explicit accept → DONE; anything else →
        feed it back as guidance and let recon continue. No human present →
        gateway.ask holds with no timeout (we never auto-PASS an unproven result;
        an unattended run parks here by design)."""
        import re

        gateway = getattr(self.browser_manager, "gateway", None)
        report = (getattr(result, "report", "") or result.feedback())[:1500]
        question = (
            "审计员拿不准 recon 是否真的达标(很可能样本种类 / 内容存疑)。它的判定:\n\n"
            f"{report}\n\n"
            "你来定:回复「通过」让它进入 gate;或说明哪里不对,让 agent 继续补。"
        )
        answer = ""
        if gateway is not None:
            try:
                resp = await gateway.ask(question=question, timeout_s=None)  # hold
                answer = (resp.message or "").strip() if resp else ""
            except Exception as e:  # noqa: BLE001
                logger.warning(f"UNCERTAIN escalation ask failed: {e}")
        if answer and re.search(r"通过|pass|accept|\bok\b|可以|没问题|算过", answer, re.IGNORECASE):
            logger.info(f"Human adjudicated UNCERTAIN as PASS: {answer[:120]}")
            await self._emit_strategy_report(audit_result=result, blocked_reason=None)
            return json.dumps({
                "status": "DONE", "outcome": "complete",
                "note": f"human-adjudicated UNCERTAIN->pass: {answer[:200]}",
            })
        feedback = answer or "(审计 UNCERTAIN,且无人裁定)"
        return json.dumps({
            "status": "audit_uncertain",
            "feedback": (result.feedback()[:2400] + f"\n\n[人工裁定] {feedback}")[:3000],
            "message": (
                "Audit was UNCERTAIN and a human weighed in. Address the human's "
                "note and re-mark_done, or mark_done(status='blocked', ...)."
            ),
        })

    async def _emit_strategy_report(
        self,
        audit_result: Any = None,
        blocked_reason: str | None = None,
    ) -> None:
        """Write a human-readable strategy report after recon ends.

        Read by the gate (CLI prompt between recon and harvest) so the
        operator can decide: continue / narrow scope / stop. Failure to
        generate the LLM portion does NOT block DONE — recon is complete
        regardless.

        The universe section at the top is the gate's primary decision
        input: bounded vs unbounded, size estimate, catalog pointer.
        """
        run_dir = Config.run_dir(self.domain)
        try:
            semantic, procedural = await db.load_both_models(self.domain)

            samples_dir = run_dir / "samples"
            sample_count = 0
            sample_listing = "(none)"
            if samples_dir.is_dir():
                files = sorted(p for p in samples_dir.iterdir() if p.is_file())
                sample_count = len(files)
                if files:
                    sample_listing = "\n".join(
                        f"- {p.name} ({p.stat().st_size} bytes)" for p in files[:20]
                    )
                    if sample_count > 20:
                        sample_listing += f"\n... ({sample_count - 20} more)"

            catalog_dir = run_dir / "catalog"
            catalog_count = 0
            catalog_listing = "(none)"
            if catalog_dir.is_dir():
                cfiles = sorted(p for p in catalog_dir.iterdir() if p.is_file())
                catalog_count = len(cfiles)
                if cfiles:
                    catalog_listing = "\n".join(
                        f"- {p.name} ({p.stat().st_size} bytes)" for p in cfiles[:20]
                    )
                    if catalog_count > 20:
                        catalog_listing += f"\n... ({catalog_count - 20} more)"

            # Universe header — sourced from audit if available, else
            # heuristic from disk. This is the FIRST thing the human reads.
            if audit_result is not None and getattr(audit_result, "universe", None):
                u = audit_result.universe
                universe_header = (
                    f"- Bounded: {'yes' if u.bounded else 'NO (gate decision needed)'}\n"
                    f"- Size estimate: {u.size_estimate}\n"
                    f"- Catalog pointer: {u.catalog_pointer}\n"
                )
            else:
                universe_header = (
                    f"- Bounded: unknown (no audit data)\n"
                    f"- Size estimate: unknown\n"
                    f"- Catalog pointer: see catalog/ listing below\n"
                )

            blocked_header = ""
            if blocked_reason:
                blocked_header = (
                    f"## ⚠ Planner declared BLOCKED\n\n{blocked_reason}\n\n"
                    "Reconnaissance ended without a clean audit. Review the WM "
                    "and decide whether to launch harvest anyway, retry recon, "
                    "or narrow the requirement.\n\n"
                )

            system_prompt = (
                "You are summarizing reconnaissance results for a human operator "
                "who is about to decide whether to start the harvest stage. Be "
                "concrete and brief — at most 30 lines of markdown. Answer:\n"
                "1. What is the primary data on this site?\n"
                "2. What access methods were discovered (rank from easiest to hardest)?\n"
                "3. What auth is required (none / cookies / login / 2FA)?\n"
                "4. What anti-bot defenses were observed?\n"
                "5. How many samples are on disk? In what format?\n"
                "6. Universe enumeration: is catalog/ complete? bounded? how does\n"
                "   the procedural model say to enumerate it?\n"
                "7. Any specific concerns the operator should know before harvest.\n\n"
                "Do NOT recommend a harvest tier or specific tools — the harvest "
                "agent decides its own approach. Stick to facts and observations."
            )
            user_msg = (
                f"## Requirement\n{self.requirement}\n\n"
                f"## Semantic Model\n{semantic or '(empty)'}\n\n"
                f"## Procedural Model\n{procedural or '(empty)'}\n\n"
                f"## Samples ({sample_count})\n{sample_listing}\n\n"
                f"## Catalog ({catalog_count} files)\n{catalog_listing}"
            )

            response_text = await self.llm.generate(prompt=user_msg, system=system_prompt)
            llm_section = response_text or "(LLM produced empty report)"

            report_text = (
                "# Strategy Report\n\n"
                f"{blocked_header}"
                "## Universe (read FIRST — drives the gate decision)\n\n"
                f"{universe_header}\n"
                "## LLM Summary\n\n"
                f"{llm_section}\n"
            )
        except Exception as e:
            logger.warning(f"strategy_report generation failed: {e}")
            report_text = (
                f"# Strategy Report (generation failed)\n\n"
                f"Recon completed but the LLM call to summarize failed: {e}\n\n"
                f"Read the World Model directly via "
                f"`read_world_model` or query the DB for context."
            )

        report_path = run_dir / "strategy_report.md"
        report_path.write_text(report_text, encoding="utf-8")
        logger.info(f"Strategy report written to {report_path}")

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
