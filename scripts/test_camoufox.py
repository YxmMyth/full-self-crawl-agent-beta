"""One-off Camoufox verification.

Launches Camoufox headed with our proposed kwargs and reports:
  - window / viewport / screen sizes as seen by JS
  - bot.sannysoft detection signals (screenshot)
  - persistent context round-trip (set cookie → close → reopen → verify cookie)

Run: python scripts/test_camoufox.py
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

OUT = Path(__file__).parent.parent / "artifacts" / "_camoufox_test"
OUT.mkdir(parents=True, exist_ok=True)


async def report_sizes(page):
    data = await page.evaluate("""() => ({
        innerW: window.innerWidth,
        innerH: window.innerHeight,
        outerW: window.outerWidth,
        outerH: window.outerHeight,
        screenW: screen.width,
        screenH: screen.height,
        dpr: window.devicePixelRatio,
        ua: navigator.userAgent,
        webdriver: navigator.webdriver,
        platform: navigator.platform,
    })""")
    print("  JS-reported sizes:")
    for k, v in data.items():
        print(f"    {k}: {v}")
    return data


async def phase1_display():
    print("\n[Phase 1] Display check — window=(1440,900), os=windows, humanize=True")
    from camoufox.async_api import AsyncCamoufox

    async with AsyncCamoufox(
        headless=False,
        window=(1440, 900),
        os="windows",
        humanize=True,
    ) as browser:
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        print("\n  → navigating to https://bot.sannysoft.com/")
        await page.goto("https://bot.sannysoft.com/", wait_until="load", timeout=60000)
        await asyncio.sleep(3)
        await report_sizes(page)
        shot1 = OUT / "sannysoft.png"
        await page.screenshot(path=str(shot1), full_page=False)  # viewport only
        await page.screenshot(path=str(OUT / "sannysoft_full.png"), full_page=True)
        print(f"  ✓ saved: {shot1.name} (viewport) + sannysoft_full.png (full page)")

        print("\n  → navigating to https://abrahamjuliot.github.io/creepjs/")
        await page.goto("https://abrahamjuliot.github.io/creepjs/", wait_until="load", timeout=60000)
        await asyncio.sleep(5)
        shot2 = OUT / "creepjs.png"
        await page.screenshot(path=str(shot2), full_page=False)
        print(f"  ✓ saved: {shot2.name}")

        print("\n  Keeping window open for 15 seconds — observe the display.")
        await asyncio.sleep(15)


async def phase2_persistence():
    print("\n[Phase 2] Persistent context — set cookie, reopen, verify")
    from camoufox.async_api import AsyncCamoufox

    profile_dir = OUT / "profile"
    profile_dir.mkdir(exist_ok=True)

    # First launch — set cookie
    print("  → first launch (set cookie)")
    async with AsyncCamoufox(
        headless=False,
        window=(1440, 900),
        os="windows",
        humanize=True,
        persistent_context=True,
        user_data_dir=str(profile_dir),
    ) as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://example.com/", wait_until="load")
        await ctx.add_cookies([{
            "name": "camoufox_test_cookie",
            "value": "hello_from_phase2",
            "domain": "example.com",
            "path": "/",
        }])
        cookies_before = await ctx.cookies()
        print(f"    cookies set: {len(cookies_before)} total")
        await asyncio.sleep(2)

    # Second launch — verify cookie persists
    print("  → second launch (verify cookie)")
    async with AsyncCamoufox(
        headless=False,
        window=(1440, 900),
        os="windows",
        humanize=True,
        persistent_context=True,
        user_data_dir=str(profile_dir),
    ) as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://example.com/", wait_until="load")
        cookies_after = await ctx.cookies()
        matching = [c for c in cookies_after if c.get("name") == "camoufox_test_cookie"]
        if matching:
            print(f"    ✓ cookie persisted across restart: {matching[0]}")
        else:
            print(f"    ✗ cookie NOT found. total cookies: {len(cookies_after)}")
        await asyncio.sleep(3)


async def main():
    try:
        await phase1_display()
    except Exception as e:
        print(f"\nPhase 1 ERROR: {type(e).__name__}: {e}")
    try:
        await phase2_persistence()
    except Exception as e:
        print(f"\nPhase 2 ERROR: {type(e).__name__}: {e}")
    print(f"\nDone. Output dir: {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
