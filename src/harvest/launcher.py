"""Harvest launcher — entry point for the harvest stage.

Reuses recon's AgentSession with:
  - harvest_prompt as system prompt
  - 14 recon tools + apply_patch + mark_done
  - ctx attributes wired so mark_done can call the verification subagent
  - long-run safety net watchdog (wall-clock + tool call cap)

Assumes a recon run has already produced WM + samples/ under
artifacts/{domain}/runs/{source_run_id}/. Harvest writes to that same
run_dir (workspace/, verification/), inheriting recon's artifacts.

See: CLAUDE.md §一 系统概述
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path

from src.agent.session import (
    AgentSession,
    OUTCOME_DONE,
    OUTCOME_SAFETY,
)
from src.agent.tools.registry import ToolRegistry
from src.agent.tools import (
    apply_patch as apply_patch_tool,
    bash_tool,
    browse,
    browser_eval,
    browser_reset,
    click,
    fetch,
    go_back,
    human_assist as human_assist_tool,
    input as input_tool,
    mark_done as mark_done_tool,
    press_key,
    read_network,
    read_wm,
    scroll,
    think,
)
from src.browser.manager import BrowserManager
from src.config import Config
from src.harvest.checklist import (
    ChecklistCompileError,
    compile_checklist,
    is_conflict,
)
from src.harvest.prompt import HARVEST_SYSTEM_PROMPT
from src.llm.client import LLMClient
from src.runtime import status_hook
from src.runtime.human_assist import TkinterPopupGateway, WebGateway
from src.utils.logging import get_logger
from src.world_model import db

logger = get_logger(__name__)


# ── Default safety-net thresholds ────────────────────────

_DEFAULT_MAX_WALL_SECS = 3 * 3600  # 3 hours
_DEFAULT_MAX_TOOL_CALLS = 800
_WATCHDOG_INTERVAL_SECS = 30


# ── Run-id selection ─────────────────────────────────────


def _resolve_source_run_id(domain: str, source_run_id: str | None) -> str:
    """Find the recon run to harvest from.

    If explicit run_id is given, use it. Otherwise pick the latest run dir
    under artifacts/{domain}/runs/ (lexicographic max — our run_ids are
    timestamp-prefixed YYYYMMDD-HHMM_slug, so sort is correct).
    """
    runs_dir = Config.ARTIFACTS_DIR / domain / "runs"
    if source_run_id:
        candidate = runs_dir / source_run_id
        if not candidate.is_dir():
            raise RuntimeError(
                f"Specified run_id '{source_run_id}' not found under {runs_dir}"
            )
        return source_run_id

    if not runs_dir.is_dir():
        raise RuntimeError(
            f"No runs directory for domain '{domain}' at {runs_dir}. "
            f"Run reconnaissance first."
        )
    runs = sorted([d.name for d in runs_dir.iterdir() if d.is_dir()], reverse=True)
    if not runs:
        raise RuntimeError(
            f"No recon runs found for domain '{domain}'. Run reconnaissance first."
        )
    return runs[0]


def _validate_recon_artifacts(domain: str) -> tuple[str, Path]:
    """Validate that the source run has the artifacts harvest needs.

    Returns (requirement, run_dir). Raises RuntimeError with a clear message
    if anything required is missing.
    """
    run_dir = Config.run_dir(domain)  # requires Config.RUN_ID set

    req_file = run_dir / "requirement.txt"
    if not req_file.is_file():
        raise RuntimeError(
            f"Missing {req_file}. Harvest needs requirement.txt produced by recon "
            "(or the human-edited version from the gate)."
        )
    requirement = req_file.read_text(encoding="utf-8").strip()
    if not requirement:
        raise RuntimeError(f"{req_file} is empty.")

    samples_dir = run_dir / "samples"
    if not samples_dir.is_dir() or not any(samples_dir.iterdir()):
        logger.warning(
            f"samples/ is missing or empty at {samples_dir}. Harvest can still "
            "run but has no shape contract to anchor on."
        )

    return requirement, run_dir


# ── Tool registry ────────────────────────────────────────


def build_harvest_registry() -> ToolRegistry:
    """Register the 14 recon tools + apply_patch + mark_done.

    Excludes none — harvest uses every recon tool. The recording_agent
    integration in browse/etc. is harmless when recording_agent is None
    (we don't run one during harvest — see launcher below).
    """
    registry = ToolRegistry()
    tools = [
        # Recon tool set (14)
        think, read_wm, browse, read_network, browser_eval, browser_reset,
        click, input_tool, press_key, scroll, go_back, bash_tool,
        human_assist_tool, fetch,
        # Harvest additions
        apply_patch_tool,
        mark_done_tool,
    ]
    for t in tools:
        registry.register(t.TOOL_NAME, t.TOOL_DESCRIPTION, t.TOOL_PARAMETERS, t.handle)
    return registry


# ── Initial briefing ─────────────────────────────────────


def _build_briefing(domain: str, requirement: str, run_dir: Path) -> str:
    """Construct the first user message for the harvest agent.

    Keeps it short — the system prompt already covers what to think and
    which tools to use. This is task-specific context only.
    """
    samples_dir = run_dir / "samples"
    samples_count = 0
    sample_listing = "(none)"
    if samples_dir.is_dir():
        sample_files = sorted([p for p in samples_dir.iterdir() if p.is_file()])
        samples_count = len(sample_files)
        sample_listing = (
            "\n".join(f"- {p.name} ({p.stat().st_size} bytes)" for p in sample_files[:15])
            or "(empty)"
        )
        if samples_count > 15:
            sample_listing += f"\n... ({samples_count - 15} more)"

    return (
        f"## Mission\n\n"
        f"Domain: {domain}\n"
        f"Requirement (boundary):\n{requirement}\n\n"
        f"## Existing recon artifacts\n\n"
        f"Source run: {Config.RUN_ID}\n"
        f"Workspace cwd from bash: ./ (= {run_dir / 'workspace'})\n"
        f"Samples ({samples_count} files at ../samples/):\n{sample_listing}\n\n"
        f"## Start\n\n"
        f"1. Call `read_world_model()` to see what recon discovered.\n"
        f"2. Inspect a sample with `bash cat ../samples/<one>` to lock in the shape contract.\n"
        f"3. Write your crawler with `apply_patch` (create `crawl.py`), run with `bash python crawl.py`.\n"
        f"4. Iterate until you have the full dataset that satisfies the boundary above.\n"
        f"5. Call `mark_done(reason=...)` when you believe you're complete."
    )


# ── Checklist compile with the v3 conflict protocol ─────


async def _interpret_accept_or_stop(llm, conflict: str, answer: str) -> bool:
    """LLM-semantic read of the operator's reply to a checklist conflict:
    accept-the-substitute vs stop. No regex (「不接受」must not match 接受)."""
    result = await llm.generate(
        prompt=(
            f"冲突说明:\n{conflict[:600]}\n\n用户的回复:\n{answer}\n\n"
            "判断:用户是接受替代、继续任务,还是要停止?"
        ),
        system=(
            "只输出一个词:ACCEPT(接受替代继续)或 STOP(停止)。"
            "注意「不接受」「不行」「停」是 STOP。"
        ),
    )
    return bool(result and "ACCEPT" in result.upper())


async def _compile_checklist_fail_closed(
    llm,
    requirement: str,
    samples_dir: Path,
    checklist_path: Path,
    *,
    catalog_dir: Path,
    procedural_model: str | None,
    intent_kernel: str | None,
    run_dir: Path,
    domain: str,
    gateway,
) -> None:
    """Compile the acceptance checklist; route compiler CONFLICTs to a human.

    The compiler may refuse to write a checklist when the intent kernel's
    evidence standard cannot be honestly verified from the available data
    (the vndb failure: kernel rejected quote compilations, site only had
    quotes, the compiler silently lowered the bar). On CONFLICT the human
    decides: accept the substitute — the compromise is folded into a re-pinned
    intent kernel v2 (human-confirmed) and we recompile — or stop (fail-closed).
    The loop is bounded by the human: every round requires an answer.

    LLM failure → one retry → propagate (fail-closed; no more silent stub).
    """
    timeout_s = (
        Config.HUMAN_ASSIST_TIMEOUT_S if Config.HUMAN_ASSIST_TIMEOUT_S > 0 else None
    )

    async def _do_compile() -> str:
        return await compile_checklist(
            llm, requirement, samples_dir, checklist_path,
            catalog_dir=catalog_dir,
            procedural_model=procedural_model,
            intent_kernel=intent_kernel,
        )

    conflict_round = 0
    while True:
        try:
            text = await _do_compile()
        except ChecklistCompileError as e:
            logger.warning(f"Checklist compile failed ({e}); retrying once")
            text = await _do_compile()  # 2nd failure propagates → run dies loudly

        if not is_conflict(text):
            return

        conflict_round += 1
        if gateway is None:
            raise RuntimeError(
                f"checklist conflict but no human channel available — fail closed. "
                f"Conflict: {text[:400]}"
            )
        prefix = (
            f"[验收冲突 · 第{conflict_round}次]" if conflict_round > 1 else "[验收冲突]"
        )
        resp = await gateway.ask(
            question=(
                f"{prefix} 清单编译器判断:现有数据满足不了意图内核的证据标准,"
                f"拒绝产出验收清单。\n\n{text[:1500]}\n\n"
                "你来定:接受现有数据作为替代(这个妥协会写进意图内核、"
                "出新版本后继续),还是停止任务?"
            ),
            timeout_s=timeout_s,
            options=["接受替代,继续", "停止任务"],
        )
        answer = (resp.message or "").strip() if resp is not None else ""
        if resp is None or resp.status != "completed" or not answer:
            raise RuntimeError(
                "checklist conflict needs a human decision but none arrived — "
                f"fail closed. Conflict: {text[:400]}"
            )
        if not await _interpret_accept_or_stop(llm, text, answer):
            raise RuntimeError(
                f"operator stopped at checklist conflict: {answer[:200]}"
            )

        # Accepted: fold the compromise into a re-pinned kernel v2 (the human
        # confirms the new draft inside compile_intent_kernel), then recompile.
        from src.intake.kernel import compile_intent_kernel

        intent_kernel = await compile_intent_kernel(
            llm, requirement, run_dir, gateway, domain=domain,
            seed_clarifications=[
                (f"验收清单编译冲突:{text[:600]}", f"操作员批准接受替代:{answer}"),
            ],
            pin_reason=f"checklist conflict compromise (round {conflict_round})",
        )


# ── Safety net watchdog ──────────────────────────────────


async def _safety_watchdog(
    ctx,
    session: AgentSession,
    max_wall_secs: int,
    max_tool_calls: int,
) -> None:
    """Background task that signals session to stop if limits exceeded.

    Sets ctx._safety_stop = True + ctx._safety_reason. The session's main
    loop polls this flag between rounds and exits with OUTCOME_SAFETY.
    """
    start = time.monotonic()
    while True:
        await asyncio.sleep(_WATCHDOG_INTERVAL_SECS)
        if getattr(ctx, "_mission_done", False):
            return  # session already done
        if getattr(ctx, "_safety_stop", False):
            return  # already signaled

        elapsed = time.monotonic() - start
        if elapsed > max_wall_secs:
            ctx._safety_stop = True
            ctx._safety_reason = (
                f"wall-clock timeout: {elapsed:.0f}s > {max_wall_secs}s"
            )
            logger.warning(f"Watchdog: {ctx._safety_reason}")
            return

        if session.step > max_tool_calls:
            ctx._safety_stop = True
            ctx._safety_reason = (
                f"tool-call cap: {session.step} > {max_tool_calls}"
            )
            logger.warning(f"Watchdog: {ctx._safety_reason}")
            return


# ── Main entry ───────────────────────────────────────────


async def run_harvest(
    domain: str,
    source_run_id: str | None = None,
    max_wall_secs: int = _DEFAULT_MAX_WALL_SECS,
    max_tool_calls: int = _DEFAULT_MAX_TOOL_CALLS,
) -> dict:
    """Run a harvest mission against an existing recon run.

    Args:
        domain: Target domain (e.g. "codepen.io").
        source_run_id: Recon run_id to harvest from. If None, picks the
            latest under artifacts/{domain}/runs/.
        max_wall_secs: Watchdog wall-clock timeout. Default 3 hours.
        max_tool_calls: Watchdog tool-call cap. Default 800.

    Returns:
        Session result dict: {session_id, outcome, steps_taken, ...}
        outcome is "mission_done" on a clean Verification PASS, or
        "safety_net" / "natural_stop" / "context_exhausted" / "consecutive_errors"
        on early termination.
    """
    Config.require("LLM_API_KEY", "LLM_BASE_URL", "DATABASE_URL")

    Config.RUN_ID = _resolve_source_run_id(domain, source_run_id)
    requirement, run_dir = _validate_recon_artifacts(domain)
    logger.info(f"Harvest: domain={domain} run_id={Config.RUN_ID}")
    logger.info(f"Run dir: {run_dir}")
    logger.info(f"Requirement: {requirement}")

    # Ensure subdirs exist (recon should have made them, but be defensive)
    for sub in ["workspace", "verification"]:
        (run_dir / sub).mkdir(parents=True, exist_ok=True)

    # Point the web-console status hook at this run + mark harvest phase
    # (no-op unless HELMSMAN_RUN=1).
    status_hook.configure(run_dir, domain=domain, run_id=Config.RUN_ID)
    status_hook.set_phase("harvest")

    await db.connect()
    logger.info("Database connected")

    browser_manager = BrowserManager(domain=domain)
    # Under the Helmsman web console (HELMSMAN_RUN=1) route human-assist through
    # the WebGateway (signal files under workspace/, surfaced in the web UI);
    # otherwise the always-on-top Tkinter desktop popup.
    if os.getenv("HELMSMAN_RUN") == "1":
        browser_manager.gateway = WebGateway(run_dir / "workspace")
    else:
        browser_manager.gateway = TkinterPopupGateway()
    # Harvest runs HEADED by default so request_human_assist (login / CAPTCHA)
    # actually has a visible window for the operator to act in. The earlier
    # headless default created a contradiction: the agent could pop a "log in
    # via the browser" request, but with no visible window there was nothing to
    # act in (codepen run 2026-05-30). Recon also runs headed, so this matches.
    # Override with HARVEST_HEADLESS=1 for truly unattended/no-login missions.
    # (Historical note: a Camoufox headed long-run handshake stall was seen on
    # a 2026-05-25 svg run; if it recurs, set HARVEST_HEADLESS=1 or have the
    # agent call browser_reset(headed=False) mid-mission.)
    harvest_headless = os.getenv("HARVEST_HEADLESS", "0") == "1"
    ctx = await browser_manager.launch(headed=not harvest_headless)
    logger.info(
        f"Browser launched (headless={harvest_headless}), "
        f"human_assist gateway = TkinterPopup"
    )

    llm = LLMClient()
    logger.info(f"LLM client ready (model={Config.LLM_MODEL})")

    # Compile checklist as DESCRIPTIONS ONLY (no bash check field).
    # The audit (src/harvest/audit.py) reads these descriptions at
    # mark_done time and asks an LLM to evaluate each against actual
    # on-disk evidence. This combines:
    #   - mission-specific criteria (LLM compiles per-mission, references
    #     this catalog/sample by name; avoids generic hardcoded H1-H6)
    #   - late-bound verification (no bash fortune-telling — the famous
    #     svg-run `file --mime-type | grep text/plain` bug can't recur
    #     since there are no bash checks frozen at compile time)
    #   - tamper resistance (hash-pinned below; agent can't rewrite the
    #     contract to make satisficing easy)
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    samples_dir = run_dir / "samples"
    catalog_dir = run_dir / "catalog"
    checklist_path = workspace / "checklist.md"
    _, procedural_model = await db.load_both_models(domain)
    intent_kernel_path = run_dir / "intent_kernel.md"
    intent_kernel = None
    if intent_kernel_path.is_file():
        intent_kernel = intent_kernel_path.read_text(encoding="utf-8").strip() or None
    try:
        await _compile_checklist_fail_closed(
            llm, requirement, samples_dir, checklist_path,
            catalog_dir=catalog_dir,
            procedural_model=procedural_model,
            intent_kernel=intent_kernel,
            run_dir=run_dir,
            domain=domain,
            gateway=browser_manager.gateway,
        )
    except BaseException:
        # Fail-closed exit before the session ever starts — release resources
        # (the session-level try/finally below hasn't been entered yet).
        status_hook.set_phase("blocked")
        await browser_manager.close()
        await llm.close()
        await db.close()
        raise
    logger.info(f"Checklist ready: {checklist_path}")

    # Hash-pin the compiled checklist. mark_done verifies the on-disk file
    # against this sha256 before invoking the audit — any agent edit (even
    # one byte) fails the mission. See 2026-05-25 svg run for the bypass
    # this defends against (agent rewrote section headers so the parser
    # found 0 criteria, then auto-PASS fallback fired). The current
    # design rejects parser-0-criteria too, but the hash is defense in
    # depth.
    import hashlib
    ctx._checklist_sha256 = hashlib.sha256(
        checklist_path.read_bytes()
    ).hexdigest()
    logger.info(f"Checklist sha256: {ctx._checklist_sha256[:16]}...")

    # Attach deps for mark_done (it reads these off ctx to call the audit)
    ctx._llm = llm
    ctx._requirement = requirement
    # _domain is attached by AgentSession.__init__

    registry = build_harvest_registry()
    logger.info(f"Harvest registry: {len(registry.names())} tools")

    session_id = f"harvest_{uuid.uuid4().hex[:8]}"
    briefing = _build_briefing(domain, requirement, run_dir)

    session = AgentSession(
        session_id=session_id,
        run_id=Config.RUN_ID,
        domain=domain,
        briefing=briefing,
        ctx=ctx,
        llm=llm,
        registry=registry,
        browser_manager=browser_manager,
        recording_agent=None,  # harvest does not maintain observations
        system_prompt=HARVEST_SYSTEM_PROMPT,
    )

    watchdog_task = asyncio.create_task(
        _safety_watchdog(ctx, session, max_wall_secs, max_tool_calls)
    )

    try:
        result = await session.run()
        logger.info(f"Harvest session ended: {result}")
    finally:
        watchdog_task.cancel()
        try:
            await watchdog_task
        except (asyncio.CancelledError, Exception):
            pass

        await browser_manager.close()
        await llm.close()
        await db.close()
        logger.info("All resources cleaned up")

    # Summarize outcome to caller + close out the status.json phase lifecycle.
    # Without this the console shows a finished run as forever "harvest"
    # (set_phase("done") used to exist only on the gate-stop path).
    if result["outcome"] == OUTCOME_DONE:
        status_hook.set_phase("done")
        logger.info(f"Harvest PASS: {session_id} in {result['steps_taken']} steps")
    elif result["outcome"] == OUTCOME_SAFETY:
        reason = getattr(ctx, "_safety_reason", "unknown")
        status_hook.set_phase("blocked")
        logger.warning(f"Harvest stopped by safety net: {reason}")
    else:
        status_hook.set_phase("blocked")
        logger.warning(f"Harvest ended without PASS: {result['outcome']}")

    return result
