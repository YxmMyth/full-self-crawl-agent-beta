"""Offline test for the intent-kernel intake compiler.

Fakes llm.generate and the gateway. Covers: CLEAR (pins kernel),
clarify-then-clear (human answer folded into the next compile), fail-closed when
a clarification is needed but no human answers, and fail-closed on empty compile.

Run: python scripts/test_intent_kernel.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.intake.kernel import compile_intent_kernel, IntentIntakeError  # noqa: E402

KERNEL = "# 意图内核\n## 终态\nx\n## 证据标准\ny(拒梗概)\n## 边界与约束\nz"


class FakeLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def generate(self, prompt, system=None, model=None):
        self.calls.append(prompt)
        return self.outputs.pop(0)


class Resp:
    def __init__(self, status, message=None):
        self.status = status
        self.message = message


class FakeGateway:
    def __init__(self, resp):
        self.resp = resp
        self.asked = []

    async def ask(self, question, timeout_s=None):
        self.asked.append(question)
        return self.resp


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="intent_"))
    failures = 0

    # 1) CLEAR → pins kernel
    llm = FakeLLM([KERNEL + "\n---CLARIFY---\nCLEAR"])
    await compile_intent_kernel(llm, "采剧本", tmp / "r1", FakeGateway(Resp("completed", "x")))
    ok = (tmp / "r1" / "intent_kernel.md").read_text(encoding="utf-8").startswith("# 意图内核") \
        and (tmp / "r1" / "intent_kernel.sha256").exists()
    print(f"  [{'PASS' if ok else 'FAIL'}] CLEAR → kernel + sha pinned")
    failures += not ok

    # 2) clarify → answer folded into the recompile → CLEAR
    llm = FakeLLM([
        KERNEL + "\n---CLARIFY---\n剧本指真对白还是剧情梗概?",
        KERNEL + "\n---CLARIFY---\nCLEAR",
    ])
    gw = FakeGateway(Resp("completed", "玩家读到的真对白"))
    await compile_intent_kernel(llm, "采剧本", tmp / "r2", gw)
    ok = gw.asked and "剧本指真对白" in gw.asked[0] and "真对白" in llm.calls[1]
    print(f"  [{'PASS' if ok else 'FAIL'}] clarify → answer folded into recompile")
    failures += not ok

    # 3) clarification needed but no human → fail-closed
    llm = FakeLLM([KERNEL + "\n---CLARIFY---\n这里有歧义?"])
    try:
        await compile_intent_kernel(llm, "采剧本", tmp / "r3", FakeGateway(Resp("timeout", None)))
        print("  [FAIL] no-human → should have raised")
        failures += 1
    except IntentIntakeError:
        print("  [PASS] no-human clarification → fail-closed (raised)")

    # 4) empty compile → fail-closed
    llm = FakeLLM([""])
    try:
        await compile_intent_kernel(llm, "采剧本", tmp / "r4", FakeGateway(Resp("completed", "x")))
        print("  [FAIL] empty compile → should have raised")
        failures += 1
    except IntentIntakeError:
        print("  [PASS] empty compile → fail-closed (raised)")

    print("\n" + ("ALL PASS" if failures == 0 else f"{failures} FAILURE(S)"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
