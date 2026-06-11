"""Gate — the human decision point between recon and harvest.

Recon writes a strategy_report.md when it finishes (see
src/planner/recon_planner.py:_emit_strategy_report). This module shows
that report AND the current intent kernel to the operator, then asks:
continue to harvest (with this intent) or adjust.

The gate is the second alignment checkpoint (after intake). Recon may have
discovered things that change the picture (paid content, binary format, etc),
so the operator reviews both the strategy report and the intent kernel before
harvest begins. If adjustments are needed, the intent is re-compiled and
re-pinned.
"""

from __future__ import annotations

import json
import time
from typing import Any

from src.config import Config
from src.runtime import status_hook
from src.utils.logging import get_logger

logger = get_logger(__name__)


_DECISION_CONTINUE = "continue"
_DECISION_STOP = "stop"


def _read_file(path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except Exception:
        return ""


async def run_gate(
    domain: str,
    requirement: str,
    llm: Any,
    gateway: Any,
) -> tuple[str, str]:
    """Show strategy report + intent kernel, let the human confirm or adjust.

    Returns (decision, requirement) where:
      - decision = 'continue' or 'stop'
      - requirement = the (possibly updated) requirement text

    If the human adjusts the intent, re-compiles and re-pins intent_kernel.md.
    """
    run_dir = Config.run_dir(domain)
    report = _read_file(run_dir / "strategy_report.md")
    intent_kernel = _read_file(run_dir / "intent_kernel.md")

    # For Helmsman (containerized) path: use gateway.ask with structured options.
    # For terminal (direct CLI) path: fall back to the console gate.
    if gateway is not None and status_hook.enabled():
        return await _gate_via_gateway(
            domain, requirement, llm, gateway, report, intent_kernel
        )

    # Terminal fallback (legacy CLI path)
    return _gate_terminal(domain, requirement, report, intent_kernel), requirement


async def _gate_via_gateway(
    domain: str,
    requirement: str,
    llm: Any,
    gateway: Any,
    report: str,
    intent_kernel: str,
) -> tuple[str, str]:
    """Gate via the web console gateway (Helmsman path). Shows strategy report +
    intent kernel; operator confirms or adjusts."""
    run_dir = Config.run_dir(domain)

    # Build the gate question showing both strategy report and intent
    parts = ["Recon 完成。请检查 recon 发现和当前意图:\n"]
    if report:
        parts.append(f"## 策略报告\n{report[:2000]}\n")
    if intent_kernel:
        parts.append(f"## 当前意图内核\n{intent_kernel[:2000]}\n")
    else:
        parts.append("(没有意图内核 — intake 可能未运行)\n")
    parts.append("确认继续 harvest,或者告诉我要调整什么。")

    # First, raise the gate flag so the console shows the gate banner
    status_hook.set_flag(gate_pending=True)

    try:
        resp = await gateway.ask(
            question="\n".join(parts),
            timeout_s=None,  # wait forever — gate is an explicit checkpoint
            options=["继续 harvest", "要调整意图"],
        )
    finally:
        status_hook.set_flag(gate_pending=False)

    answer = (resp.message or "").strip() if resp else ""
    if not answer or resp.status != "completed":
        logger.info("Gate: no human answered, defaulting to stop")
        return _DECISION_STOP, requirement

    # Use LLM to interpret: continue or adjust?
    interpret_prompt = (
        f"用户在 recon→harvest 的检查点回复了:\n{answer}\n\n"
        "判断:用户是要继续(确认当前意图没问题),还是要调整(修改意图/需求)?\n"
        "只输出一个词:CONTINUE 或 ADJUST"
    )
    interpret = await llm.generate(
        prompt=interpret_prompt,
        system="判断用户在 gate 检查点的意图:CONTINUE(确认继续)或 ADJUST(要调整)。只输出一个词。",
    )

    if interpret and "ADJUST" in interpret.upper():
        # Human wants to adjust — re-compile intent WITH their feedback seeded
        # in (a from-scratch recompile would lose the adjustment text and ask
        # them to confirm a kernel that doesn't reflect what they said).
        logger.info(f"Gate: human wants adjustment: {answer[:160]}")
        from src.intake.kernel import compile_intent_kernel

        adjusted_req = requirement  # keep original requirement
        try:
            await compile_intent_kernel(
                llm, requirement, run_dir, gateway, domain=domain,
                seed_clarifications=[("gate 检查点的人工调整", answer)],
                pin_reason="gate adjustment",
            )
            logger.info("Gate: intent kernel re-compiled after adjustment")
        except Exception as e:
            # The human asked for a change and it didn't land — don't silently
            # continue on the old kernel. Surface it; they decide.
            logger.warning(f"Gate: re-compile failed: {e}")
            resp2 = await gateway.ask(
                question=(
                    f"意图重编失败({e})。你要的调整没有写进意图内核。"
                    "用原意图继续 harvest,还是停止?"
                ),
                timeout_s=None,
                options=["用原意图继续", "停止"],
            )
            ans2 = (resp2.message or "").strip() if resp2 is not None else ""
            interp2 = ""
            if ans2 and resp2.status == "completed":
                interp2 = await llm.generate(
                    prompt=f"用户回复:{ans2}\n判断:继续还是停止?",
                    system="只输出一个词:CONTINUE 或 STOP。",
                ) or ""
            if "CONTINUE" not in interp2.upper():
                return _DECISION_STOP, requirement
        return _DECISION_CONTINUE, adjusted_req

    logger.info("Gate: human confirmed, continuing to harvest")
    return _DECISION_CONTINUE, requirement


def _gate_terminal(
    domain: str,
    requirement: str,
    report: str,
    intent_kernel: str,
) -> str:
    """Terminal fallback gate (non-Helmsman path)."""
    print("\n" + "=" * 60)
    print("  RECON COMPLETE")
    print("=" * 60)
    print(f"\nDomain:       {domain}")
    print(f"Requirement:  {requirement}")
    print(f"Run ID:       {Config.RUN_ID}")

    if report:
        print(f"\n--- Strategy Report ---\n{report}\n")
    else:
        print("\n[!] Strategy report missing.")

    if intent_kernel:
        print(f"--- Intent Kernel ---\n{intent_kernel}\n")
    else:
        print("[!] Intent kernel missing.\n")

    print("-" * 60)
    while True:
        try:
            ans = input("  Continue to harvest? [y/N]: ").strip().lower()
        except EOFError:
            print("  (no stdin — defaulting to stop)")
            return _DECISION_STOP
        if ans in ("y", "yes"):
            return _DECISION_CONTINUE
        if ans in ("", "n", "no"):
            return _DECISION_STOP
        print("  Please answer y / N.")
