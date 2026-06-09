"""intake — compile the raw requirement into a pinned intent kernel.

The intent kernel is the ONE defined + protected object (CLAUDE.md §六 — free
prose, NOT a typed schema). It is WANT + evidence-standard + qualitative
boundaries, pinned to disk BEFORE recon and later used as the auditor's
measuring stick and the gate's calibration target.

Judgment-driven clarification: after a compile, the LLM judges whether a genuine
semantic ambiguity remains that would change WHAT COUNTS AS THE REAL DELIVERABLE
(the kungal trap: "剧本 = real dialogue, or a synopsis?"). If so, it asks the
human one specific question via the gateway, folds the answer in, and recompiles.
There is NO fixed round cap — the human in the loop is the throttle (each round
blocks on a real answer). FAIL-CLOSED: if a clarification is genuinely needed but
no human answers, we hard-stop rather than run on a guessed requirement.

Models on src/harvest/checklist.py:compile_checklist (one generate() call →
markdown), but for the pre-recon intent (no samples/catalog yet) and fail-CLOSED
(the harvest checklist's stub-fallback is deliberately NOT copied here).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from src.config import Config
from src.utils.logging import get_logger

logger = get_logger(__name__)

_CLARIFY_MARKER = "---CLARIFY---"


class IntentIntakeError(RuntimeError):
    """Raised when an intent kernel cannot be established — an empty/garbage
    compile, or a clarification is needed but no human is available. Fail-closed:
    the run must NOT proceed on a guessed requirement."""


_COMPILE_SYSTEM_PROMPT = """你把一个自然语言的采集需求,编译成「意图内核」——用户真正要什么的稳定契约。后续的探索(recon)和完成核验(审计)都拿它当尺子,所以它必须把「什么算真东西」讲死。

意图内核是三段自由散文(不是字段表,不是 JSON,不要 target_fields 这类 schema):

# 意图内核

## 终态
跑完之后世界该是什么样——最终交付物到底是什么。

## 证据标准
一份「真东西」具体长什么样;以及哪些**廉价替代品必须拒**——那些满足表面、能骗过粗略检查、但其实不是用户要的东西。这是整个内核的钉子。讲得越具体越好:真东西的可识别特征 + 明确点名要拒的假货。
举例:需求是"采游戏剧本(玩家读到的真实对白)",证据标准该写:"样本 = 玩家真正读到的对白行(通常有 角色名:台词 的交替结构、是对话体);剧情梗概、一句话总结、标题列表、API 元数据、URL、计数——都不算真东西,审计要打开文件按内容判;若对白以二进制/编码形式存储,必须解码后才算。"

## 边界与约束
范围、硬规矩。**只定性,绝不写数量**——"该采多少"由 recon 探索时从站点发现(catalog),不在这里拍一个数字。

硬规则:
- 全程自由散文。不要字段名 / typed schema / target_fields。
- 数量永远不进内核。
- 证据标准必须显式列出要拒的廉价替代——这是阻止系统把目标偷偷缩水成更省事版本的唯一防线。缺了它,内核就是钝的。

编译完,再判断:这个需求在语义上,是否还有**会改变「什么算真东西」的真实歧义**?——不是鸡毛蒜皮、不是你能合理默认的小事,而是那种"理解错了整轮就白跑"的根本歧义(比如"剧本"到底指真对白还是剧情梗概)。

在 ---CLARIFY--- 这一行之后输出:
- 若已经够清楚(证据标准能笃定写出):输出 CLEAR
- 若存在一个会改变交付物判定的真实歧义:输出一个**具体、自包含**的问题问人(一次只问最关键那一个;能自己合理默认的别问)

严格按这个格式输出(先内核 markdown,然后单独一行 ---CLARIFY---,然后判定):

<三段 markdown>
---CLARIFY---
CLEAR 或 一行问题
"""


def _split_kernel(raw: str) -> tuple[str, str]:
    """Split the compile output into (kernel_markdown, verdict).

    verdict is 'CLEAR' or a clarifying question. If the model omitted the marker
    entirely, treat the whole output as the kernel and assume CLEAR (the kernel
    was still produced; we don't block on a missing meta-marker)."""
    if _CLARIFY_MARKER in raw:
        kernel, _, verdict = raw.partition(_CLARIFY_MARKER)
        return kernel.strip(), verdict.strip()
    return raw.strip(), "CLEAR"


def _is_clear(verdict: str) -> bool:
    v = verdict.strip()
    return (not v) or v.upper().startswith("CLEAR")


def _build_user_msg(requirement: str, clarifications: list[tuple[str, str]]) -> str:
    parts = [f"原始需求(逐字):\n{requirement}"]
    if clarifications:
        lines = ["", "已与人澄清(把这些并入证据标准 / 边界):"]
        for q, a in clarifications:
            lines.append(f"- 问:{q}")
            lines.append(f"  答:{a}")
        parts.append("\n".join(lines))
    parts.append(
        "\n现在编译意图内核。三段自由散文,证据标准必须显式列出要拒的廉价替代,"
        "数量不要写。最后在 ---CLARIFY--- 后输出 CLEAR 或一个会改变交付物判定的关键问题。"
    )
    return "\n".join(parts)


def _pin(kernel: str, run_dir: Path) -> str:
    """Write intent_kernel.md + a sha256 sidecar (tamper-evidence for the
    auditor's 'verify unmodified' step) and return the kernel text."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "intent_kernel.md").write_text(kernel, encoding="utf-8")
    sha = hashlib.sha256(kernel.encode("utf-8")).hexdigest()
    (run_dir / "intent_kernel.sha256").write_text(sha, encoding="utf-8")
    logger.info(
        f"Intent kernel pinned: {run_dir / 'intent_kernel.md'} "
        f"({len(kernel)} chars, sha={sha[:12]})"
    )
    return kernel


async def compile_intent_kernel(
    llm: Any,
    requirement: str,
    run_dir: Path | str,
    gateway: Any,
    *,
    domain: str = "",
) -> str:
    """Compile `requirement` into a pinned intent kernel, clarifying with the
    human when a genuine semantic ambiguity remains. Returns the kernel text.

    Raises IntentIntakeError (fail-closed) on an empty/garbage compile, or when a
    clarification is needed but no human answers.
    """
    run_dir = Path(run_dir)
    clarifications: list[tuple[str, str]] = []
    timeout_s = (
        Config.HUMAN_ASSIST_TIMEOUT_S if Config.HUMAN_ASSIST_TIMEOUT_S > 0 else None
    )

    while True:
        user_msg = _build_user_msg(requirement, clarifications)
        raw = await llm.generate(prompt=user_msg, system=_COMPILE_SYSTEM_PROMPT)
        if not raw or not raw.strip():
            raise IntentIntakeError(
                "intent-kernel compile returned empty — refusing to run without an "
                "intent (check LLM gateway / reasoning_content handling)."
            )

        kernel, verdict = _split_kernel(raw)
        if not kernel or "## " not in kernel:
            raise IntentIntakeError(
                f"intent-kernel compile produced no usable kernel; head: {raw[:200]!r}"
            )

        if _is_clear(verdict):
            return _pin(kernel, run_dir)

        # A genuine semantic ambiguity remains → ask the human (judgment-driven,
        # no round cap; the human answering is the throttle).
        question = verdict
        logger.info(f"Intent intake needs clarification: {question[:160]}")
        if gateway is None:
            raise IntentIntakeError(
                f"intent clarification needed but no gateway is configured: {question}"
            )
        resp = await gateway.ask(question=f"[需求澄清] {question}", timeout_s=timeout_s)
        answer = (resp.message or "").strip() if resp is not None else ""
        if resp is None or resp.status != "completed" or not answer:
            # No human / dismissed / timed out → fail closed. Do NOT guess.
            raise IntentIntakeError(
                f"intent clarification needed but no human answered "
                f"(status={getattr(resp, 'status', 'none')}); refusing to run on a "
                f"guessed requirement. Question was: {question}"
            )
        clarifications.append((question, answer))
        logger.info(f"Intent clarified: {answer[:160]}")
