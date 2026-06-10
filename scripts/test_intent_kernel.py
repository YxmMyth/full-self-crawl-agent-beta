"""Offline test for the intent-kernel intake compiler.

Fakes llm.generate and the gateway. Covers:
  1) CLEAR + human confirms → pins kernel
  2) CLEAR + human adjusts → re-compile with feedback
  3) clarify → answer folded → CLEAR → human confirms
  4) fail-closed when human doesn't answer clarification
  5) fail-closed when human doesn't confirm compiled kernel
  6) fail-closed on empty compile

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
KERNEL_V2 = "# 意图内核\n## 终态\nx2\n## 证据标准\ny2(拒梗概+拒计数)\n## 边界与约束\nz2"


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
    def __init__(self, responses):
        self.responses = list(responses) if isinstance(responses, list) else [responses]
        self.asked = []

    async def ask(self, question, timeout_s=None, options=None, cancellable=True):
        self.asked.append((question, options))
        return self.responses.pop(0) if self.responses else Resp("timeout")


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="intent_"))
    failures = 0

    # 1) CLEAR + human confirms → pins kernel
    llm = FakeLLM([
        KERNEL + "\n---CLARIFY---\nCLEAR",   # compile
        "CONFIRMED",                           # interpret confirmation
    ])
    gw = FakeGateway([Resp("completed", "确认,没问题")])
    await compile_intent_kernel(llm, "采剧本", tmp / "r1", gw)
    ok = (
        (tmp / "r1" / "intent_kernel.md").read_text(encoding="utf-8").startswith("# 意图内核")
        and (tmp / "r1" / "intent_kernel.sha256").exists()
        and len(gw.asked) == 1  # confirmation ask
        and gw.asked[0][1] is not None  # had options
    )
    print(f"  [{'PASS' if ok else 'FAIL'}] CLEAR + confirm → kernel pinned")
    failures += not ok

    # 2) CLEAR + human adjusts → re-compile with feedback → confirm
    llm = FakeLLM([
        KERNEL + "\n---CLARIFY---\nCLEAR",    # first compile
        "ADJUST",                              # interpret: wants adjustment
        KERNEL_V2 + "\n---CLARIFY---\nCLEAR",  # re-compile with feedback
        "CONFIRMED",                            # interpret second confirmation
    ])
    gw = FakeGateway([
        Resp("completed", "要加上拒绝计数数据"),  # adjustment feedback
        Resp("completed", "确认,没问题"),         # second confirm
    ])
    await compile_intent_kernel(llm, "采剧本", tmp / "r2", gw)
    text = (tmp / "r2" / "intent_kernel.md").read_text(encoding="utf-8")
    ok = "x2" in text and len(gw.asked) == 2 and "拒绝计数" in llm.calls[2]
    print(f"  [{'PASS' if ok else 'FAIL'}] CLEAR + adjust → re-compile → confirm")
    failures += not ok

    # 3) clarify → answer folded → CLEAR → human confirms
    llm = FakeLLM([
        KERNEL + "\n---CLARIFY---\n剧本指真对白还是剧情梗概?",  # needs clarification
        KERNEL + "\n---CLARIFY---\nCLEAR",                      # re-compile CLEAR
        "CONFIRMED",                                            # interpret confirmation
    ])
    gw = FakeGateway([
        Resp("completed", "玩家读到的真对白"),     # answer clarification
        Resp("completed", "确认,没问题"),          # confirm kernel
    ])
    await compile_intent_kernel(llm, "采剧本", tmp / "r3", gw)
    ok = (
        len(gw.asked) == 2
        and "剧本指真对白" in gw.asked[0][0]
        and gw.asked[0][1] is None            # clarification has no options
        and gw.asked[1][1] is not None        # confirmation has options
        and "真对白" in llm.calls[1]          # answer folded into re-compile
    )
    print(f"  [{'PASS' if ok else 'FAIL'}] clarify → answer folded → confirm")
    failures += not ok

    # 4) clarification needed but no human → fail-closed
    llm = FakeLLM([KERNEL + "\n---CLARIFY---\n这里有歧义?"])
    try:
        await compile_intent_kernel(llm, "采剧本", tmp / "r4", FakeGateway([Resp("timeout", None)]))
        print("  [FAIL] no-human clarification → should have raised")
        failures += 1
    except IntentIntakeError:
        print("  [PASS] no-human clarification → fail-closed (raised)")

    # 5) CLEAR but no human confirms → fail-closed
    llm = FakeLLM([KERNEL + "\n---CLARIFY---\nCLEAR"])
    try:
        await compile_intent_kernel(llm, "采剧本", tmp / "r5", FakeGateway([Resp("timeout", None)]))
        print("  [FAIL] no-human confirmation → should have raised")
        failures += 1
    except IntentIntakeError:
        print("  [PASS] no-human confirmation → fail-closed (raised)")

    # 6) empty compile → fail-closed
    llm = FakeLLM([""])
    try:
        await compile_intent_kernel(llm, "采剧本", tmp / "r6", FakeGateway([Resp("completed", "x")]))
        print("  [FAIL] empty compile → should have raised")
        failures += 1
    except IntentIntakeError:
        print("  [PASS] empty compile → fail-closed (raised)")

    print("\n" + ("ALL PASS" if failures == 0 else f"{failures} FAILURE(S)"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
