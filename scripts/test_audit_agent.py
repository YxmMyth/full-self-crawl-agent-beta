"""Offline test for the read-only auditor agent (src/audit/agent.py).

Fakes the LLM. Verifies: sandboxed read-only tools (list_dir / read_file / grep,
escape refused), verdict parse (defaults UNCERTAIN, never PASS), the full
investigate→verdict loop, and the round-cap → UNCERTAIN backstop.

Run: python scripts/test_audit_agent.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.audit import agent as A  # noqa: E402


class FakeTC:
    def __init__(self, _id, name, args):
        self.id = _id
        self.name = name
        self.arguments = args


class FakeResp:
    def __init__(self, tool_calls=None, text=""):
        self.tool_calls = tool_calls or []
        self._text = text

    @property
    def text(self):
        return self._text

    def to_assistant_message(self):
        return {"role": "assistant", "content": self._text or "(tool round)"}


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)

    async def chat_with_tools(self, messages, tools):
        return self.responses.pop(0) if self.responses else FakeResp(text="OVERALL: UNCERTAIN")


def _setup(tmp: Path):
    (tmp / "samples").mkdir(parents=True, exist_ok=True)
    (tmp / "samples" / "synopsis.txt").write_text(
        "这是一个关于勇者拯救世界的剧情概要,讲述他如何打败魔王。", encoding="utf-8"
    )
    (tmp / "samples" / "script.txt").write_text(
        "勇者:我们终于到了。\n魔王:你来得太晚了。\n勇者:这次不会让你得逞。", encoding="utf-8"
    )


async def main() -> int:
    failures = 0

    # 1) parse_overall — lenient + defaults UNCERTAIN, never PASS
    cases = [
        ("blah\nOVERALL: FAIL", "FAIL"),
        ("OVERALL: pass", "PASS"),
        ("**OVERALL:** UNCERTAIN", "UNCERTAIN"),
        ("VERDICT = FAIL", "FAIL"),
        ("no verdict here", "UNCERTAIN"),
        ("", "UNCERTAIN"),
        ("OVERALL: PASS\n... then OVERALL: FAIL", "FAIL"),  # last wins
    ]
    for text, want in cases:
        got = A.parse_overall(text)
        ok = got == want
        failures += not ok
        if not ok:
            print(f"  [FAIL] parse_overall({text!r}) = {got} want {want}")
    print(f"  [{'PASS' if all(A.parse_overall(t)==w for t,w in cases) else 'FAIL'}] parse_overall ({len(cases)} cases)")

    # 2) sandboxed tools
    tmp = Path(tempfile.mkdtemp(prefix="audit_"))
    _setup(tmp)
    listing = A._handle_list_dir(tmp, "samples")
    ok = "synopsis.txt" in listing and "script.txt" in listing
    print(f"  [{'PASS' if ok else 'FAIL'}] list_dir → {listing.strip().splitlines()[:1]}")
    failures += not ok

    content = A._handle_read_file(tmp, "samples/script.txt")
    ok = "魔王:你来得太晚了" in content
    print(f"  [{'PASS' if ok else 'FAIL'}] read_file returns real content")
    failures += not ok

    escape = A._handle_read_file(tmp, "../../../etc/passwd")
    ok = escape.startswith("[refused]") or escape.startswith("(not a file")
    print(f"  [{'PASS' if ok else 'FAIL'}] sandbox escape refused → {escape[:40]!r}")
    failures += not ok

    hits = A._handle_grep(tmp, r"^魔王[:：]", "samples")
    ok = "script.txt" in hits and "synopsis.txt" not in hits
    print(f"  [{'PASS' if ok else 'FAIL'}] grep finds dialogue tell only in script")
    failures += not ok

    # 3) full loop: investigate then FAIL
    llm = FakeLLM([
        FakeResp(tool_calls=[FakeTC("1", "list_dir", {"path": "samples"})]),
        FakeResp(tool_calls=[FakeTC("2", "read_file", {"path": "samples/synopsis.txt"})]),
        FakeResp(text="C2: synopsis.txt 是第三人称剧情概要,不是对白。\nOVERALL: FAIL"),
    ])
    overall, report = await A.run_audit_agent(llm, tmp, "audit this")
    ok = overall == "FAIL" and "概要" in report
    print(f"  [{'PASS' if ok else 'FAIL'}] full loop → {overall}")
    failures += not ok

    # 4) round-cap backstop → UNCERTAIN (never PASS)
    spin = [FakeResp(tool_calls=[FakeTC("x", "list_dir", {"path": "."})]) for _ in range(5)]
    spin.append(FakeResp(text="still not sure"))  # forced-synth turn, no OVERALL
    overall, _ = await A.run_audit_agent(llm.__class__(spin), tmp, "audit", max_rounds=2)
    ok = overall == "UNCERTAIN"
    print(f"  [{'PASS' if ok else 'FAIL'}] round-cap → {overall} (never PASS)")
    failures += not ok

    print("\n" + ("ALL PASS" if failures == 0 else f"{failures} FAILURE(S)"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
