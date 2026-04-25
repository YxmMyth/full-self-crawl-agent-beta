"""End-to-end integration test for human_assist flow.

Exercises:
  - BrowserManager launches with persistent_context per domain
  - ToolContext wires human_assist gateway
  - request_human_assist tool dispatches to gateway
  - TerminalGateway: bring_to_front + prompt + signal-file wait
  - Tool returns structured result with next_step_hint

This test simulates the human by auto-creating the signal file after a
delay, so it can run unattended. Real flow uses actual human input.

Run: python scripts/test_human_assist_integration.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
# Make src/ importable when running this script directly
sys.path.insert(0, str(ROOT))

# ── Test config ──────────────────────────────────────────
DOMAIN = "codepen.io"
SIMULATED_HUMAN_DELAY_S = 5  # how long the "fake human" takes to react


async def auto_signal(signal_path: Path, delay_s: int) -> None:
    """Simulate a human by creating the signal file after a delay."""
    await asyncio.sleep(delay_s)
    print(f"  [test] simulating human: creating signal file after {delay_s}s")
    signal_path.write_text("simulated\n", encoding="utf-8")


async def main() -> None:
    from src.utils.logging import setup
    setup(level="INFO")

    from src.browser.manager import BrowserManager
    from src.runtime.human_assist import TerminalGateway
    from src.agent.tools import human_assist as human_assist_tool
    from src.config import Config

    # Make sure the per-domain workspace dir exists
    artifacts = Config.artifacts_for(DOMAIN)
    workspace = artifacts / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    print("\n=== Phase 1: Launch BrowserManager with codepen.io domain ===")
    bm = BrowserManager(domain=DOMAIN)
    print(f"  profile_dir: {bm.profile_dir}")
    expected = (ROOT / "artifacts" / "_profiles" / DOMAIN).resolve()
    actual = bm.profile_dir.resolve()
    assert actual == expected, f"profile_dir mismatch: {actual} != {expected}"
    print(f"  profile exists from prior login: {bm.profile_dir.exists()}")

    ctx = await bm.launch()
    print(f"  browser launched, tabs: {len(ctx.tabs)}")

    print("\n=== Phase 2: Wire human_assist gateway ===")
    assert ctx.human_assist is None, "human_assist should be None before wiring"
    ctx.human_assist = TerminalGateway(signal_dir=workspace)
    signal_file = ctx.human_assist.signal_file
    print(f"  gateway wired, signal file: {signal_file}")

    print("\n=== Phase 3: Navigate to a page (so bring_to_front has something) ===")
    await ctx.page.goto("https://codepen.io/your-work", wait_until="load", timeout=30000)
    await asyncio.sleep(2)
    url_before = ctx.page.url
    print(f"  current URL: {url_before}")
    if "/login" in url_before:
        print("  [warn] redirected to /login — profile may have lost login state")
    else:
        print("  ✓ still logged in (profile persistence confirmed)")

    print(f"\n=== Phase 4: Call request_human_assist tool (auto-signal in {SIMULATED_HUMAN_DELAY_S}s) ===")

    # Kick off the simulated-human task in parallel
    sim_task = asyncio.create_task(auto_signal(signal_file, SIMULATED_HUMAN_DELAY_S))

    # Call the tool exactly like the agent would
    result = await human_assist_tool.handle(
        ctx,
        reason="测试用例:验证 request_human_assist tool 接通整个 gateway 链路。这是自动测试,信号会在 5 秒后被脚本自己创建。"
    )

    await sim_task  # ensure simulator finishes cleanly

    print("\n=== Phase 5: Verify tool result ===")
    print(f"  result type: {type(result).__name__}")
    print(f"  result content:")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    assert isinstance(result, dict), "tool should return dict"
    assert result.get("status") == "completed", f"expected completed, got {result.get('status')}"
    assert "next_step_hint" in result, "next_step_hint missing"
    assert "browse" in result["next_step_hint"], "hint should tell agent to browse"
    print("  ✓ status=completed, next_step_hint present and correct")

    # Verify signal file consumed
    assert not signal_file.exists(), "signal file should be consumed"
    print("  ✓ signal file consumed")

    print("\n=== Phase 6: Verify gateway cleanup (signal file gone, no errors) ===")
    assert ctx.human_assist is not None, "gateway still wired"
    print("  ✓ gateway still active for further calls")

    print("\n=== Phase 7: Cleanup ===")
    await bm.close()
    print("  ✓ browser closed")

    print("\n" + "=" * 64)
    print("✅ ALL PASSED — human_assist integration verified end-to-end")
    print("=" * 64)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
    except AssertionError as e:
        print(f"\n❌ ASSERTION FAILED: {e}")
        raise
    except Exception as e:
        print(f"\n❌ ERROR: {type(e).__name__}: {e}")
        raise
