"""#4 durability: operator answers must write the response file even with NO
live RunHandle (a console restart drops every handle, but the mission keeps
polling the file on disk).

Proves RunRegistry.answer_{gate,assist,ask} write by (domain, run_id) without a
handle. WebConfig.run_dir is patched to a temp dir so no real artifacts are
touched.

Run: python scripts/test_answer_durability.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from web.services import supervisor as S  # noqa: E402


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="durab_"))
    S.WebConfig.run_dir = staticmethod(lambda domain, run_id: tmp / domain / run_id)

    reg = S.RunRegistry(db_read=None)  # zero handles — simulates post-restart console
    ws = tmp / "example.com" / "run123" / "workspace"
    failures = 0

    ok = await reg.answer_ask("example.com", "run123", "uuidA", "真实对白", "completed")
    data = json.loads((ws / "ask_response_uuidA.json").read_text(encoding="utf-8"))
    good = ok and data == {"status": "completed", "message": "真实对白"}
    print(f"  [{'PASS' if good else 'FAIL'}] answer_ask (no handle) → {data}")
    failures += not good

    ok = await reg.answer_gate("example.com", "run123", "continue")
    data = json.loads((ws / "gate_response.json").read_text(encoding="utf-8"))
    good = ok and data == {"decision": "continue"}
    print(f"  [{'PASS' if good else 'FAIL'}] answer_gate (no handle) → {data}")
    failures += not good

    ok = await reg.answer_assist("example.com", "run123", "uuidB", "cancelled")
    data = json.loads((ws / "assist_response_uuidB.json").read_text(encoding="utf-8"))
    good = ok and data == {"status": "cancelled"}
    print(f"  [{'PASS' if good else 'FAIL'}] answer_assist (no handle) → {data}")
    failures += not good

    # Missing domain → refuse (no silent write to a bogus path)
    ok = await reg.answer_ask("", "run123", "uuidC", "x", "completed")
    good = ok is False
    print(f"  [{'PASS' if good else 'FAIL'}] answer_ask('' domain) → refused ({ok})")
    failures += not good

    print("\n" + ("ALL PASS" if failures == 0 else f"{failures} FAILURE(S)"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
