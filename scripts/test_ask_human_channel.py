"""Offline round-trip test for the ask_human channel (WebGateway.ask).

Mirrors the file-signal contract the console uses: WebGateway.ask writes
ask_request_{uuid}.json + sets status flags, then polls for
ask_response_{uuid}.json. Here a fake "console" task writes the response. No
web server, no network.

Covers: answered (text comes back), cancelled (no message), timeout.

Run: python scripts/test_ask_human_channel.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.runtime.human_assist import WebGateway  # noqa: E402


def _latest_uuid(workspace: Path) -> str:
    reqs = sorted(workspace.glob("ask_request_*.json"))
    assert reqs, "no ask_request_*.json was written by WebGateway.ask"
    return json.loads(reqs[-1].read_text(encoding="utf-8"))["uuid"]


async def _fake_console_write(workspace: Path, delay: float, payload: dict) -> None:
    await asyncio.sleep(delay)
    uuid = _latest_uuid(workspace)
    (workspace / f"ask_response_{uuid}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


async def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="ask_test_"))
    gw = WebGateway(workspace)
    failures = 0

    # 1) answered → text comes back in .message
    asyncio.create_task(
        _fake_console_write(workspace, 0.2, {"status": "completed", "message": "真实对白,不是梗概"})
    )
    r = await gw.ask("剧本指玩家读到的真实对白,还是剧情梗概?", timeout_s=10)
    ok = r.status == "completed" and r.message == "真实对白,不是梗概"
    print(f"  [{'PASS' if ok else 'FAIL'}] answered → status={r.status!r} message={r.message!r}")
    failures += not ok

    # 2) cancelled → no message
    asyncio.create_task(
        _fake_console_write(workspace, 0.2, {"status": "cancelled"})
    )
    r = await gw.ask("随便问问", timeout_s=10)
    ok = r.status == "cancelled" and r.message is None
    print(f"  [{'PASS' if ok else 'FAIL'}] cancelled → status={r.status!r} message={r.message!r}")
    failures += not ok

    # 3) timeout → nobody answers
    r = await gw.ask("无人应答", timeout_s=0.4)
    ok = r.status == "timeout" and r.message is None
    print(f"  [{'PASS' if ok else 'FAIL'}] timeout → status={r.status!r}")
    failures += not ok

    # 4) request/response files cleaned up afterward
    leftover = list(workspace.glob("ask_*.json"))
    ok = not leftover
    print(f"  [{'PASS' if ok else 'FAIL'}] cleanup → leftover={[p.name for p in leftover]}")
    failures += not ok

    print("\n" + ("ALL PASS" if failures == 0 else f"{failures} FAILURE(S)"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
