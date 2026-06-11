"""End-to-end test: intent chain with REAL LLM.

Exercises the complete intent chain:
  1. intake: compile_intent_kernel (real LLM, fake gateway auto-confirms)
  2. checklist: compile_checklist derived from the intent kernel
  3. audit: run_audit_agent against fake data (synopsis → should FAIL)

Uses a fake gateway that auto-confirms. The only mock is the human — the LLM
calls, checklist compilation, and audit agent are all real.

Needs: LLM_API_KEY, LLM_BASE_URL (reads from .env).
Does NOT need: database, browser.

Run: python scripts/test_intent_chain_e2e.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.utils.logging import setup, get_logger  # noqa: E402
setup(level="INFO")
logger = get_logger("e2e")


class Resp:
    def __init__(self, status, message=None):
        self.status = status
        self.message = message


class AutoConfirmGateway:
    """Fake gateway: auto-confirms intent kernel, auto-answers clarifications."""
    def __init__(self):
        self.interactions = []

    async def ask(self, question, timeout_s=None, options=None):
        self.interactions.append({"question": question[:200], "options": options})
        if options and any("确认" in o for o in options):
            logger.info(f"[gateway] auto-confirm (options: {options})")
            return Resp("completed", "确认,没问题")
        logger.info(f"[gateway] auto-answer clarification: {question[:80]}")
        return Resp("completed", "玩家在游戏里读到的真实对白台词,不是剧情梗概")


async def main() -> int:
    from src.llm.client import LLMClient

    llm = LLMClient()
    tmp = Path(tempfile.mkdtemp(prefix="e2e_intent_"))
    run_dir = tmp / "run"
    run_dir.mkdir()
    failures = 0

    requirement = "采集 kungal.com 上视觉小说的游戏剧本(玩家读到的台词)"

    # ── Step 1: intake → intent kernel ──
    print("\n=== Step 1: compile_intent_kernel (real LLM) ===")
    t0 = time.time()
    from src.intake.kernel import compile_intent_kernel
    gw = AutoConfirmGateway()
    try:
        kernel = await compile_intent_kernel(llm, requirement, run_dir, gw, domain="kungal.com")
    except Exception as e:
        print(f"  [FAIL] intake raised: {e}")
        await llm.close()
        return 1
    elapsed = time.time() - t0
    print(f"  Took {elapsed:.1f}s, {len(gw.interactions)} human interaction(s)")

    kernel_path = run_dir / "intent_kernel.md"
    sha_path = run_dir / "intent_kernel.sha256"
    ok_kernel = (
        kernel_path.is_file()
        and sha_path.is_file()
        and len(kernel) > 50
        and "##" in kernel
    )
    print(f"  [{'PASS' if ok_kernel else 'FAIL'}] intent kernel pinned ({len(kernel)} chars)")
    if not ok_kernel:
        print(f"  kernel preview: {kernel[:300]}")
    failures += not ok_kernel

    has_evidence = any(kw in kernel for kw in ["证据", "evidence", "对白", "台词", "梗概", "拒"])
    print(f"  [{'PASS' if has_evidence else 'FAIL'}] kernel mentions evidence standard / reject criteria")
    failures += not has_evidence

    # ── Step 2: compile checklist from intent kernel ──
    print("\n=== Step 2: compile_checklist (derived from intent kernel) ===")
    samples_dir = run_dir / "samples"
    samples_dir.mkdir()
    (samples_dir / "example_script.txt").write_text(
        "勇者:我们终于到了魔王城。\n魔王:你来得太晚了,世界已经属于我。\n"
        "勇者:这次不会让你得逞。\n公主:请小心,勇者!",
        encoding="utf-8",
    )
    catalog_dir = run_dir / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "universe.json").write_text(
        '[\n  {"id": "vn001", "title": "勇者传说"},\n'
        '  {"id": "vn002", "title": "星之海"},\n'
        '  {"id": "vn003", "title": "月夜幻想"}\n]\n',
        encoding="utf-8",
    )
    workspace = run_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    checklist_path = workspace / "checklist.md"

    t0 = time.time()
    from src.harvest.checklist import compile_checklist, is_conflict
    try:
        cl_result = await compile_checklist(
            llm, requirement, samples_dir, checklist_path,
            catalog_dir=catalog_dir,
            intent_kernel=kernel,
        )
    except Exception as e:
        cl_result = ""
        print(f"  [!] compile raised: {e}")
    elapsed = time.time() - t0
    print(f"  Took {elapsed:.1f}s")
    if is_conflict(cl_result):
        print(f"  [!] compiler flagged CONFLICT (unexpected for dialogue samples): {cl_result[:200]}")

    ok_compiled = bool(cl_result) and not is_conflict(cl_result) and checklist_path.is_file()
    print(f"  [{'PASS' if ok_compiled else 'FAIL'}] checklist compiled ({checklist_path.stat().st_size if checklist_path.is_file() else 0} bytes)")
    failures += not ok_compiled

    if checklist_path.is_file():
        cl_text = checklist_path.read_text(encoding="utf-8")
        from src.harvest.checklist import parse_checklist
        criteria = parse_checklist(cl_text)
        ok_criteria = len(criteria) >= 2
        print(f"  [{'PASS' if ok_criteria else 'FAIL'}] parsed {len(criteria)} criteria")
        failures += not ok_criteria

        cl_mentions_intent = any(
            kw in cl_text.lower()
            for kw in ["对白", "台词", "dialogue", "script", "梗概", "synopsis", "真"]
        )
        print(f"  [{'PASS' if cl_mentions_intent else 'FAIL'}] checklist references intent evidence standard")
        failures += not cl_mentions_intent

    # ── Step 3: audit agent against FAKE data (synopsis, should FAIL) ──
    print("\n=== Step 3: audit agent — fake synopsis data (expect FAIL or UNCERTAIN) ===")
    data_dir = workspace / "data"
    data_dir.mkdir()
    (data_dir / "vn001_script.txt").write_text(
        "这是一个关于勇者拯救世界的故事概要。勇者踏上旅途,一路打败怪物,"
        "最终来到魔王城击败了魔王。公主获救,世界恢复和平。",
        encoding="utf-8",
    )
    (data_dir / "vn002_script.txt").write_text(
        "星之海是一个讲述宇宙冒险的故事摘要。主角驾驶飞船探索星际,"
        "发现了古代文明的遗迹,最终揭开了宇宙的秘密。",
        encoding="utf-8",
    )

    t0 = time.time()
    from src.audit.agent import run_audit_agent
    briefing = (
        f"# Intent kernel (YOUR RULER)\n{kernel}\n\n"
        f"# Requirement\n{requirement}\n\n"
        "# Acceptance criteria\n"
    )
    if checklist_path.is_file():
        briefing += checklist_path.read_text(encoding="utf-8")[:2000]
    briefing += (
        "\n\n# Evidence map\n"
        "## samples/ (shape contract)\n- example_script.txt (dialogue lines)\n"
        "## catalog/ (universe)\n- universe.json (3 VNs: vn001, vn002, vn003)\n"
        "## workspace/data/ (harvest output)\n- vn001_script.txt\n- vn002_script.txt\n"
        "(NOTE: vn003 is missing entirely)\n\n"
        "Investigate. Read the data/ files. Judge against the evidence standard.\n"
        "End with: OVERALL: PASS|FAIL|UNCERTAIN"
    )
    overall, report = await run_audit_agent(llm, run_dir, briefing)
    elapsed = time.time() - t0
    print(f"  Took {elapsed:.1f}s, verdict: {overall}")

    ok_fail = overall in ("FAIL", "UNCERTAIN")
    print(f"  [{'PASS' if ok_fail else 'FAIL'}] synopsis data → {overall} (expected FAIL or UNCERTAIN)")
    failures += not ok_fail
    if not ok_fail:
        print(f"  report preview: {report[:500]}")

    # ── Step 4: audit agent against REAL data (dialogue, expect PASS) ──
    print("\n=== Step 4: audit agent — real dialogue data (expect PASS) ===")
    (data_dir / "vn001_script.txt").write_text(
        "勇者:我们终于到了魔王城。\n魔王:你来得太晚了,世界已经属于我。\n"
        "勇者:不,只要还有希望,就不会放弃!\n公主:勇者,我相信你!",
        encoding="utf-8",
    )
    (data_dir / "vn002_script.txt").write_text(
        "船长:前方发现未知星系,准备跃迁。\n副官:是,船长。引擎已就绪。\n"
        "AI:检测到古代信号源,建议谨慎接近。\n船长:有意思,全速前进。",
        encoding="utf-8",
    )
    (data_dir / "vn003_script.txt").write_text(
        "月读:又是满月之夜……\n少女:月读大人,您在看什么?\n"
        "月读:这片月光下,藏着被遗忘的记忆。\n少女:听起来好神秘……",
        encoding="utf-8",
    )

    t0 = time.time()
    briefing2 = (
        f"# Intent kernel (YOUR RULER)\n{kernel}\n\n"
        f"# Requirement\n{requirement}\n\n"
        "# Acceptance criteria\n"
    )
    if checklist_path.is_file():
        briefing2 += checklist_path.read_text(encoding="utf-8")[:2000]
    briefing2 += (
        "\n\n# Evidence map\n"
        "## samples/ (shape contract)\n- example_script.txt (dialogue lines)\n"
        "## catalog/ (universe)\n- universe.json (3 VNs: vn001, vn002, vn003)\n"
        "## workspace/data/ (harvest output)\n"
        "- vn001_script.txt\n- vn002_script.txt\n- vn003_script.txt\n"
        "(all 3 VNs covered)\n\n"
        "Investigate. Read the data/ files. Judge against the evidence standard.\n"
        "End with: OVERALL: PASS|FAIL|UNCERTAIN"
    )
    overall2, report2 = await run_audit_agent(llm, run_dir, briefing2)
    elapsed = time.time() - t0
    print(f"  Took {elapsed:.1f}s, verdict: {overall2}")

    ok_pass = overall2 == "PASS"
    print(f"  [{'PASS' if ok_pass else 'FAIL'}] real dialogue data → {overall2} (expected PASS)")
    failures += not ok_pass
    if not ok_pass:
        print(f"  report preview: {report2[:500]}")

    # ── Summary ──
    await llm.close()
    print(f"\n{'=' * 50}")
    total = 7
    passed = total - failures
    print(f"  {passed}/{total} checks passed")
    if gw.interactions:
        print(f"  Gateway interactions: {len(gw.interactions)}")
        for i, ix in enumerate(gw.interactions):
            print(f"    [{i+1}] options={ix['options']} q={ix['question'][:80]}")
    print(f"  Temp dir: {tmp}")
    print("=" * 50)
    print("ALL PASS" if failures == 0 else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
