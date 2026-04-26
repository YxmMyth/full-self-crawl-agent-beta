"""Quick visual test for BrowserOverlayGateway.

What you'll see:
  1. Camoufox opens, navigates to example.com
  2. A yellow overlay appears top-right with reason and 完成/取消 buttons
  3. (auto mode) After 6s, JS clicks 完成 simulating a user
  4. Overlay disappears, agent receives status='completed'
  5. Test re-fires assist, this time auto-clicks 取消 to verify cancel path
  6. Browser closes

Run: python scripts/test_overlay_gateway.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


async def main() -> None:
    from src.utils.logging import setup
    setup(level="INFO")

    from src.browser.manager import BrowserManager
    from src.runtime.human_assist import BrowserOverlayGateway
    from src.agent.tools import human_assist as ha_tool

    bm = BrowserManager(domain="overlay-test.local")
    bm.gateway = BrowserOverlayGateway()
    ctx = await bm.launch()
    print(f"✓ browser up, ctx.human_assist = {type(ctx.human_assist).__name__}")

    print("\n--- Navigate to example.com ---")
    await ctx.page.goto("https://example.com/", wait_until="load", timeout=30000)
    await asyncio.sleep(1)

    # ── Test 1: completed path ──
    print("\n--- Test 1: fire assist, auto-click 完成 in 6s ---")

    async def auto_click_done():
        await asyncio.sleep(6)
        print("  [test] simulating user click on 完成")
        try:
            await ctx.page.evaluate(
                "() => document.getElementById('__claude_assist_overlay_done').click()"
            )
        except Exception as e:
            print(f"  [test] click failed: {e}")

    asyncio.create_task(auto_click_done())
    result = await ha_tool.handle(
        ctx,
        reason="测试 1:浮层显示这段文字,你应该看到右上角的黄色卡片,6 秒后会自动点完成。",
    )
    print(f"  result: status={result.get('status')}")
    assert result.get("status") == "completed", f"expected completed, got {result}"
    print("  ✓ completed path OK")

    await asyncio.sleep(1)

    # ── Test 2: cancelled path ──
    print("\n--- Test 2: fire assist, auto-click 取消 in 4s ---")

    async def auto_click_cancel():
        await asyncio.sleep(4)
        print("  [test] simulating user click on 取消")
        try:
            await ctx.page.evaluate(
                "() => document.getElementById('__claude_assist_overlay_cancel').click()"
            )
        except Exception as e:
            print(f"  [test] click failed: {e}")

    asyncio.create_task(auto_click_cancel())
    result = await ha_tool.handle(ctx, reason="测试 2:这次会自动点取消。")
    print(f"  result: status={result.get('status')}")
    assert result.get("status") == "cancelled", f"expected cancelled, got {result}"
    print("  ✓ cancelled path OK")

    await asyncio.sleep(1)

    # ── Test 3: navigation persistence ──
    print("\n--- Test 3: fire assist, navigate to new page mid-assist, ensure overlay re-renders ---")

    async def navigate_then_click():
        await asyncio.sleep(3)
        print("  [test] navigating to https://www.iana.org/ during assist")
        try:
            await ctx.page.goto("https://www.iana.org/", wait_until="load", timeout=20000)
            await asyncio.sleep(2)
            # Verify overlay is back
            visible = await ctx.page.evaluate(
                "() => !!document.getElementById('__claude_assist_overlay')"
            )
            print(f"  [test] overlay still visible after nav: {visible}")
            await asyncio.sleep(1)
            print("  [test] simulating user click on 完成")
            await ctx.page.evaluate(
                "() => document.getElementById('__claude_assist_overlay_done').click()"
            )
        except Exception as e:
            print(f"  [test] nav step failed: {e}")

    asyncio.create_task(navigate_then_click())
    result = await ha_tool.handle(
        ctx,
        reason="测试 3:中途会导航到 iana.org,浮层应该在新页面也出现。",
    )
    print(f"  result: status={result.get('status')}")
    assert result.get("status") == "completed", f"expected completed, got {result}"
    print("  ✓ navigation persistence OK")

    print("\n--- Cleanup ---")
    await bm.close()
    print("\n" + "=" * 60)
    print("✅ ALL OVERLAY GATEWAY TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
    except AssertionError as e:
        print(f"\n❌ {e}")
        raise
