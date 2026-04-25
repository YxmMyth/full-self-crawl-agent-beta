"""End-to-end test: Camoufox window display + human-assisted login + persistence.

Phases:
  A. Launch Camoufox headed with window=(1280,900) + viewport sync.
     User manually logs in to codepen.io in the browser window.
     Press Enter in terminal when done. Script reads login state.
  B. Relaunch with same user_data_dir. Check if login persisted.

Run: python scripts/test_login.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
PROFILE = ROOT / "artifacts" / "_profiles" / "codepen.io"
PROFILE.mkdir(parents=True, exist_ok=True)
SHOTS = ROOT / "artifacts" / "_camoufox_test"
SHOTS.mkdir(parents=True, exist_ok=True)
SIGNAL = SHOTS / "LOGIN_DONE"
# Clean any stale signal from previous runs
if SIGNAL.exists():
    SIGNAL.unlink()

LOGIN_CHECK_JS = """() => {
    const menuSelectors = [
        '.site-header-user-menu',
        '[data-user-avatar]',
        'img.site-header-avatar',
        '[class*="user-menu"]',
        'a[href*="/your-work"]',
        'a[href*="/dashboard"]',
    ];
    const loggedIn = menuSelectors.some(s => document.querySelector(s));
    const loggedOut = !!document.querySelector('a[href="/login"], a[href*="/sign-up"]');
    return {
        url: location.href,
        title: document.title,
        loggedIn,
        loggedOut,
        cookieCount: document.cookie.split(';').filter(Boolean).length,
    };
}"""


async def launch_cm(user_data_dir: Path):
    from camoufox.async_api import AsyncCamoufox
    return AsyncCamoufox(
        headless=False,
        window=(1280, 900),
        os="windows",
        humanize=True,
        persistent_context=True,
        user_data_dir=str(user_data_dir),
        viewport={"width": 1280, "height": 816},
        no_viewport=False,
    )


async def report_sizes(page, label: str):
    data = await page.evaluate("""() => ({
        inner: [window.innerWidth, window.innerHeight],
        outer: [window.outerWidth, window.outerHeight],
        screen: [screen.width, screen.height],
        dpr: window.devicePixelRatio,
    })""")
    print(f"  [{label}] window sizes → {data}")


async def wait_for_signal(signal_path: Path, heartbeat_s: int = 15) -> None:
    """Poll for signal file; print heartbeat so we know the script is alive."""
    waited = 0
    while not signal_path.exists():
        await asyncio.sleep(2)
        waited += 2
        if waited % heartbeat_s == 0:
            print(f"  ... waiting for login signal ({waited}s elapsed). Create {signal_path.name} when done.", flush=True)
    signal_path.unlink()  # consume signal


async def phase_a(profile_dir: Path) -> dict:
    print("\n" + "=" * 60)
    print("[Phase A] First launch — please log in manually")
    print("=" * 60)

    async with await launch_cm(profile_dir) as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        print("  navigating to https://codepen.io/ ...")
        await page.goto("https://codepen.io/", wait_until="load", timeout=60000)
        await asyncio.sleep(2)
        await report_sizes(page, "Phase A landing")

        pre_login = await page.evaluate(LOGIN_CHECK_JS)
        print(f"  pre-login state: {pre_login}")

        await page.screenshot(path=str(SHOTS / "login_pre.png"), full_page=False)

        print("\n  >>> 浏览器窗口已打开。在窗口里完成登录。")
        print(f"  >>> 登完后,告诉 assistant 一声(它会创建信号文件 {SIGNAL.name})")
        print()
        await wait_for_signal(SIGNAL)
        print("  >>> signal received, continuing...", flush=True)

        # Navigate to dashboard to verify
        print("\n  验证登录态...")
        await page.goto("https://codepen.io/", wait_until="load", timeout=60000)
        await asyncio.sleep(2)
        post_login = await page.evaluate(LOGIN_CHECK_JS)
        print(f"  post-login state: {post_login}")

        # Try authenticated page
        await page.goto("https://codepen.io/your-work", wait_until="load", timeout=30000)
        await asyncio.sleep(2)
        auth_check = await page.evaluate(LOGIN_CHECK_JS)
        print(f"  /your-work check: {auth_check}")

        await page.screenshot(path=str(SHOTS / "login_post.png"), full_page=False)

        cookies = await ctx.cookies()
        codepen_cookies = [c for c in cookies if "codepen" in c.get("domain", "")]
        print(f"  cookies on codepen domain: {len(codepen_cookies)}")

        return {
            "loggedIn": post_login.get("loggedIn"),
            "cookie_count": len(codepen_cookies),
            "auth_url": auth_check.get("url"),
        }


async def phase_b(profile_dir: Path) -> dict:
    print("\n" + "=" * 60)
    print("[Phase B] Relaunch — verify persistence (no manual interaction)")
    print("=" * 60)

    async with await launch_cm(profile_dir) as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        print("  navigating to https://codepen.io/ (fresh process)...")
        await page.goto("https://codepen.io/", wait_until="load", timeout=60000)
        await asyncio.sleep(3)

        landing = await page.evaluate(LOGIN_CHECK_JS)
        print(f"  landing state: {landing}")

        await page.goto("https://codepen.io/your-work", wait_until="load", timeout=30000)
        await asyncio.sleep(2)
        auth_check = await page.evaluate(LOGIN_CHECK_JS)
        print(f"  /your-work check: {auth_check}")

        await page.screenshot(path=str(SHOTS / "login_relaunch.png"), full_page=False)

        cookies = await ctx.cookies()
        codepen_cookies = [c for c in cookies if "codepen" in c.get("domain", "")]
        print(f"  cookies on codepen domain: {len(codepen_cookies)}")

        print("\n  window stays open 10s — glance at it to confirm you're still logged in")
        await asyncio.sleep(10)

        return {
            "loggedIn": landing.get("loggedIn"),
            "cookie_count": len(codepen_cookies),
            "auth_url": auth_check.get("url"),
        }


async def main():
    a = await phase_a(PROFILE)
    b = await phase_b(PROFILE)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Phase A (after your login):    loggedIn={a['loggedIn']}  cookies={a['cookie_count']}  /your-work → {a['auth_url']}")
    print(f"Phase B (after relaunch):      loggedIn={b['loggedIn']}  cookies={b['cookie_count']}  /your-work → {b['auth_url']}")
    print()
    if a["loggedIn"] and b["loggedIn"]:
        print("✅ Persistence works — no fingerprint pinning needed for codepen")
    elif a["loggedIn"] and not b["loggedIn"]:
        print("⚠️  Login worked in Phase A but lost in Phase B.")
        print("    Likely cause: Camoufox fingerprint rotated → codepen treats as new device.")
        print("    Fix: pin fingerprint to profile (save Fingerprint JSON to user_data_dir).")
    else:
        print("❌ Phase A login detection failed — check screenshots in artifacts/_camoufox_test/")
    print(f"\nScreenshots: {SHOTS}")
    print(f"Profile:     {PROFILE}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(0)
