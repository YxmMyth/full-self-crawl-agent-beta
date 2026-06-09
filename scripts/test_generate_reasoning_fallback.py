"""Unit check: LLMClient.generate() no longer drops reasoning_content.

deepseek thinking-mode models often leave `content` empty and put the real
answer in `reasoning_content`. Before the fix, generate() read
`choice.message.content or None` directly and silently returned None. The fix
routes generate() through _parse_response() + LLMResponse.text (the centralized
content->reasoning fallback), the same path chat_with_tools already uses.

Offline: bypasses __init__ (no API key needed) and monkeypatches
_call_with_retry to feed fake raw responses. No network.

Run: python scripts/test_generate_reasoning_fallback.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.llm.client import LLMClient  # noqa: E402


def _fake_raw(content, reasoning):
    """Mimic an OpenAI ChatCompletion enough for _parse_response."""
    msg = SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=None)


async def main() -> int:
    # __new__ skips __init__ (which would require LLM_API_KEY / LLM_BASE_URL).
    client = LLMClient.__new__(LLMClient)

    cases = [
        # (label, content, reasoning, expected)
        ("content empty, reasoning present -> reasoning", "", "THE ANSWER", "THE ANSWER"),
        ("content None, reasoning present  -> reasoning", None, "THE ANSWER", "THE ANSWER"),
        ("content present                  -> content",   "REAL", "thinking", "REAL"),
        ("both empty                       -> None",       "", None, None),
        ("both None                        -> None",       None, None, None),
    ]

    failures = 0
    for label, content, reasoning, expected in cases:
        async def fake_call(_payload, _c=content, _r=reasoning):
            return _fake_raw(_c, _r)
        client._call_with_retry = fake_call
        got = await client.generate("prompt")
        ok = got == expected
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}  (got={got!r})")
        if not ok:
            failures += 1

    # raw is None -> None (retry exhausted)
    async def fake_none(_payload):
        return None
    client._call_with_retry = fake_none
    got = await client.generate("prompt")
    ok = got is None
    print(f"  [{'PASS' if ok else 'FAIL'}] raw None -> None  (got={got!r})")
    if not ok:
        failures += 1

    print("\n" + ("ALL PASS" if failures == 0 else f"{failures} FAILURE(S)"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
