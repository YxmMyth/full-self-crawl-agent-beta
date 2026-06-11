"""Offline test for the checklist CONFLICT protocol (v3 intent chain).

Fakes llm.generate and the gateway. Covers:
  1) normal compile → no human interaction
  2) CONFLICT → human accepts → kernel v2 re-pinned (with .prev) → recompile OK
  3) CONFLICT → human stops → fail-closed (raised)
  4) CONFLICT → no human answers → fail-closed (raised)
  5) LLM failure twice → fail-closed (raised, no stub written)

Run: python scripts/test_checklist_conflict.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.harvest.launcher import _compile_checklist_fail_closed  # noqa: E402

CHECKLIST = (
    "# Acceptance Checklist\n\n## C1. Coverage\n"
    "**criterion**: data/ has a record for every catalog entry.\n"
)
KERNEL_V2 = "# 意图内核\n## 终态\n接受金句替代\n## 证据标准\n金句即可\n## 边界与约束\nz"
CONFLICT = (
    "CONFLICT: 内核要求完整对白剧本并点名拒收金句汇编;"
    "samples/ 实际是社区短金句(样本 v4.json 内容为单句引语)。"
)


class FakeLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def generate(self, prompt, system=None, model=None):
        self.calls.append(prompt)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class Resp:
    def __init__(self, status, message=None):
        self.status = status
        self.message = message


class FakeGateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.asked = []

    async def ask(self, question, timeout_s=None, options=None, cancellable=True):
        self.asked.append((question, options))
        return self.responses.pop(0) if self.responses else Resp("timeout")


def _mkdirs(tmp: Path) -> tuple[Path, Path, Path, Path]:
    run_dir = tmp
    samples = tmp / "samples"; samples.mkdir(exist_ok=True)
    catalog = tmp / "catalog"; catalog.mkdir(exist_ok=True)
    (tmp / "workspace").mkdir(exist_ok=True)
    return run_dir, samples, catalog, tmp / "workspace" / "checklist.md"


async def main() -> int:
    failures = 0

    # 1) normal compile → no asks
    tmp = Path(tempfile.mkdtemp(prefix="clc1_"))
    run_dir, samples, catalog, cl_path = _mkdirs(tmp)
    llm = FakeLLM([CHECKLIST])
    gw = FakeGateway([])
    await _compile_checklist_fail_closed(
        llm, "采剧本", samples, cl_path,
        catalog_dir=catalog, procedural_model=None, intent_kernel="# 内核",
        run_dir=run_dir, domain="x.com", gateway=gw,
    )
    ok = cl_path.is_file() and not gw.asked
    print(f"  [{'PASS' if ok else 'FAIL'}] normal compile → no human interaction")
    failures += not ok

    # 2) CONFLICT → accept → kernel v2 re-pinned → recompile OK
    tmp = Path(tempfile.mkdtemp(prefix="clc2_"))
    run_dir, samples, catalog, cl_path = _mkdirs(tmp)
    (run_dir / "intent_kernel.md").write_text("# 旧内核 v1", encoding="utf-8")
    llm = FakeLLM([
        CONFLICT,                              # compile #1 → conflict
        "ACCEPT",                              # interpret human reply
        KERNEL_V2 + "\n---CLARIFY---\nCLEAR",  # kernel v2 compile
        "CONFIRMED",                           # v2 confirmation interpret
        CHECKLIST,                             # compile #2 → ok
    ])
    gw = FakeGateway([
        Resp("completed", "接受替代,继续"),   # conflict decision
        Resp("completed", "确认,没问题"),      # v2 kernel confirm
    ])
    await _compile_checklist_fail_closed(
        llm, "采剧本", samples, cl_path,
        catalog_dir=catalog, procedural_model=None, intent_kernel="# 旧内核 v1",
        run_dir=run_dir, domain="x.com", gateway=gw,
    )
    kernel_now = (run_dir / "intent_kernel.md").read_text(encoding="utf-8")
    prev = (run_dir / "intent_kernel.prev.md")
    ok = (
        cl_path.is_file()
        and len(gw.asked) == 2
        and "金句" in kernel_now                     # compromise in v2
        and prev.is_file() and "旧内核" in prev.read_text(encoding="utf-8")
        and "操作员批准接受替代" in llm.calls[2]      # approval seeded into recompile
    )
    print(f"  [{'PASS' if ok else 'FAIL'}] CONFLICT → accept → v2 re-pinned (.prev archived) → recompiled")
    failures += not ok

    # 3) CONFLICT → human stops → fail-closed
    tmp = Path(tempfile.mkdtemp(prefix="clc3_"))
    run_dir, samples, catalog, cl_path = _mkdirs(tmp)
    llm = FakeLLM([CONFLICT, "STOP"])
    gw = FakeGateway([Resp("completed", "停止任务")])
    try:
        await _compile_checklist_fail_closed(
            llm, "采剧本", samples, cl_path,
            catalog_dir=catalog, procedural_model=None, intent_kernel="# 内核",
            run_dir=run_dir, domain="x.com", gateway=gw,
        )
        print("  [FAIL] human stop → should have raised")
        failures += 1
    except RuntimeError:
        ok = not cl_path.is_file()
        print(f"  [{'PASS' if ok else 'FAIL'}] CONFLICT → stop → fail-closed, no checklist written")
        failures += not ok

    # 4) CONFLICT → no human → fail-closed
    tmp = Path(tempfile.mkdtemp(prefix="clc4_"))
    run_dir, samples, catalog, cl_path = _mkdirs(tmp)
    llm = FakeLLM([CONFLICT])
    try:
        await _compile_checklist_fail_closed(
            llm, "采剧本", samples, cl_path,
            catalog_dir=catalog, procedural_model=None, intent_kernel="# 内核",
            run_dir=run_dir, domain="x.com", gateway=FakeGateway([Resp("timeout")]),
        )
        print("  [FAIL] no human → should have raised")
        failures += 1
    except RuntimeError:
        print("  [PASS] CONFLICT → no human → fail-closed (raised)")

    # 5) LLM failure twice → propagates, no stub on disk
    tmp = Path(tempfile.mkdtemp(prefix="clc5_"))
    run_dir, samples, catalog, cl_path = _mkdirs(tmp)
    llm = FakeLLM(["", ""])  # empty → ChecklistCompileError, twice
    try:
        await _compile_checklist_fail_closed(
            llm, "采剧本", samples, cl_path,
            catalog_dir=catalog, procedural_model=None, intent_kernel="# 内核",
            run_dir=run_dir, domain="x.com", gateway=FakeGateway([]),
        )
        print("  [FAIL] double LLM failure → should have raised")
        failures += 1
    except Exception:
        ok = not cl_path.is_file()
        print(f"  [{'PASS' if ok else 'FAIL'}] double LLM failure → raised, no stub written")
        failures += not ok

    print("\n" + ("ALL PASS" if failures == 0 else f"{failures} FAILURE(S)"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
