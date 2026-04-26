"""Real human-in-the-loop login test — you actually log in.

Default target: github.com. Override with CLI arg.
Run this YOURSELF in your terminal so you can see prompts and type Enter.

Flow:
  Phase 1: Launch BrowserManager(domain) — uses per-domain persistent profile
  Phase 2: Navigate to login URL
  Phase 3: Call request_human_assist via the real tool path
            → window pops front, prompt prints, you log in,
              press Enter in this terminal
  Phase 4: Re-observe — check if logged-in indicators appear
  Phase 5: Phase B — relaunch with same profile,
            verify login persists (auto, no human action)

Usage:
  python scripts/test_real_login.py                        # github.com (default)
  python scripts/test_real_login.py reddit.com /login      # custom
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Defaults — overridable via CLI args
DEFAULT_DOMAIN = "github.com"
DEFAULT_LOGIN_PATH = "/login"

# Heuristics: where to navigate to verify auth, and DOM cues per domain.
# Generic — works for most sites. Site-specific tuning later via WM hints.
AUTH_PROBE_PATHS = {
    "github.com": "/settings/profile",
    "reddit.com": "/settings",
    "news.ycombinator.com": "/threads",
    "producthunt.com": "/my/topics",
    "medium.com": "/me",
}


def parse_args() -> tuple[str, str]:
    domain = sys.argv[1] if len(sys.argv) >= 2 else DEFAULT_DOMAIN
    login_path = sys.argv[2] if len(sys.argv) >= 3 else DEFAULT_LOGIN_PATH
    return domain, login_path


async def detect_login_state(page, domain: str) -> dict:
    """Combine multiple signals to judge login state."""
    probe_path = AUTH_PROBE_PATHS.get(domain, "/")

    js = """async (probePath) => {
        const resp = await fetch(probePath, { credentials: 'include', redirect: 'manual' });
        return {
            url: location.href,
            title: document.title,
            cookieCount: document.cookie.split(';').filter(s => s.trim()).length,
            probeStatus: resp.status,
            probeType: resp.type,
            probeRedirected: resp.redirected,
        };
    }"""
    raw = await page.evaluate(js, probe_path)

    # Tier-3 DOM hints (generic, may be empty if site doesn't match)
    dom = await page.evaluate("""() => {
        const generic = {
            avatar: !!document.querySelector('img[alt*="avatar" i], img[src*="avatar"]'),
            user_menu: !!document.querySelector('[class*="user-menu" i], [class*="UserMenu" i], [aria-label*="user" i]'),
            login_link: !!document.querySelector('a[href="/login"], a[href*="/sign-in"], a[href*="/signin"]'),
            signup_link: !!document.querySelector('a[href*="/sign-up"], a[href*="/signup"]'),
        };
        return generic;
    }""")
    return {**raw, "dom": dom}


async def main() -> None:
    from src.utils.logging import setup
    setup(level="INFO")

    from src.browser.manager import BrowserManager
    from src.runtime.human_assist import TkinterPopupGateway
    from src.agent.tools import human_assist as ha_tool
    from src.config import Config

    domain, login_path = parse_args()
    login_url = f"https://{domain}{login_path}"
    landing_url = f"https://{domain}/"

    artifacts = Config.artifacts_for(domain)
    workspace = artifacts / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    print(f"\n{'=' * 64}")
    print(f"Real-login test: {domain}")
    print(f"Login URL:       {login_url}")
    print(f"Profile dir:     {ROOT / 'artifacts' / '_profiles' / domain}")
    print('=' * 64)

    # ── Phase 1: launch ─────────────────────────────────
    print("\n[Phase 1] Launch BrowserManager + wire human_assist gateway")
    bm = BrowserManager(domain=domain)
    bm.gateway = TkinterPopupGateway()
    ctx = await bm.launch()
    print(f"  ✓ browser up, gateway = TkinterPopupGateway (always-on-top dialog)")

    # ── Phase 2: navigate ──────────────────────────────
    print(f"\n[Phase 2] Navigate to landing page first to check current state")
    await ctx.page.goto(landing_url, wait_until="load", timeout=30000)
    await asyncio.sleep(2)
    pre = await detect_login_state(ctx.page, domain)
    print(f"  url:           {pre['url']}")
    print(f"  cookieCount:   {pre['cookieCount']}")
    print(f"  DOM:           {pre['dom']}")
    print(f"  probe status:  {pre['probeStatus']} (path: {AUTH_PROBE_PATHS.get(domain, '/')})")

    already_logged_in = (
        not pre["dom"].get("login_link")
        and pre["dom"].get("user_menu")
        and pre["probeStatus"] == 200
    )
    if already_logged_in:
        print("\n  >>> Already logged in (profile persisted). Skipping Phase 3.")
    else:
        # ── Phase 3: real human assist ─────────────
        print(f"\n[Phase 3] Navigate to login URL, then call request_human_assist")
        await ctx.page.goto(login_url, wait_until="load", timeout=30000)
        await asyncio.sleep(2)
        print(f"  current URL: {ctx.page.url}")

        # Real call through the production tool path
        result = await ha_tool.handle(
            ctx,
            reason=(
                f"请在浏览器窗口完成 {domain} 的登录(账密 / OAuth 都可以)。\n"
                f"完成后点这个对话框下方的 [完成 ✓]。\n"
                f"如果遇到 2FA / 邮箱验证码 / Cloudflare 挑战,都在浏览器里处理完再点完成。\n"
                f"如果搞不定或不想登,点 [跳过] 让 agent 跳过这个 session。"
            ),
        )
        print(f"\n  tool returned: status={result.get('status')}")
        if result.get("status") != "completed":
            print(f"  ✗ unexpected status, aborting test")
            await bm.close()
            return

    # ── Phase 4: verify login state ────────────────────
    print(f"\n[Phase 4] Re-observe to verify login state")
    await ctx.page.goto(landing_url, wait_until="load", timeout=30000)
    await asyncio.sleep(2)
    post = await detect_login_state(ctx.page, domain)
    print(f"  url:           {post['url']}")
    print(f"  cookieCount:   {post['cookieCount']}")
    print(f"  DOM:           {post['dom']}")
    print(f"  probe status:  {post['probeStatus']}")

    judged_logged_in = (
        not post["dom"].get("login_link")
        and (post["dom"].get("user_menu") or post["dom"].get("avatar"))
    )
    print(f"\n  judgement: {'logged in' if judged_logged_in else 'NOT logged in'}")

    # ── Phase 5: persistence check (relaunch) ──────────
    print(f"\n[Phase 5] Cleanly close + relaunch to check profile persistence")
    await bm.close()

    bm2 = BrowserManager(domain=domain)
    ctx2 = await bm2.launch()
    await ctx2.page.goto(landing_url, wait_until="load", timeout=30000)
    await asyncio.sleep(2)
    relaunch = await detect_login_state(ctx2.page, domain)
    print(f"  url:           {relaunch['url']}")
    print(f"  cookieCount:   {relaunch['cookieCount']}")
    print(f"  DOM:           {relaunch['dom']}")

    persisted = (
        not relaunch["dom"].get("login_link")
        and (relaunch["dom"].get("user_menu") or relaunch["dom"].get("avatar"))
    )
    print(f"  persistence:   {'OK' if persisted else 'LOST'}")

    print(f"\n  window stays open 8s — eyeball confirm")
    await asyncio.sleep(8)
    await bm2.close()

    # ── Summary ────────────────────────────────────────
    print(f"\n{'=' * 64}")
    print("SUMMARY")
    print('=' * 64)
    print(f"Phase 4 (after manual login): {'logged in' if judged_logged_in else 'NOT'}")
    print(f"Phase 5 (after relaunch):     {'persisted' if persisted else 'LOST'}")
    if judged_logged_in and persisted:
        print(f"\n✅ ALL PASSED — full human_assist + persistence flow verified on {domain}")
    elif judged_logged_in and not persisted:
        print(f"\n⚠️  Login worked but did not persist — fingerprint pinning may be needed for {domain}")
    else:
        print(f"\n❌ Login detection unclear — check screenshots / DOM for site-specific cues")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
