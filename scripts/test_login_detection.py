"""Empirical: enumerate signals available to detect login state on codepen.io.

Reuses the persistent profile from prior login test. Reports:
  - URL behavior (redirect from auth-only paths)
  - Cookies (which ones are session/auth markers)
  - DOM signals (selectors that exist when logged in)
  - Network responses (GET /me-style endpoints)

Run: python scripts/test_login_detection.py
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
PROFILE = ROOT / "artifacts" / "_profiles" / "codepen.io"
OUT = ROOT / "artifacts" / "_camoufox_test" / "login_signals.json"


async def main():
    from camoufox.async_api import AsyncCamoufox

    findings = {}

    async with AsyncCamoufox(
        headless=False,
        window=(1280, 900),
        os="windows",
        humanize=True,
        persistent_context=True,
        user_data_dir=str(PROFILE),
        viewport={"width": 1280, "height": 816},
    ) as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # === 1. Cookies ===
        cookies = await ctx.cookies()
        codepen_cookies = [
            {"name": c["name"], "domain": c.get("domain"), "httpOnly": c.get("httpOnly"), "secure": c.get("secure")}
            for c in cookies if "codepen" in c.get("domain", "")
        ]
        findings["cookies"] = codepen_cookies
        print(f"[cookies] {len(codepen_cookies)} on codepen domain:")
        for c in codepen_cookies:
            print(f"  {c['name']}  httpOnly={c['httpOnly']} secure={c['secure']}")

        # === 2. URL behavior — auth-only path ===
        print("\n[url-behavior] navigating to /your-work (auth-only)")
        resp = await page.goto("https://codepen.io/your-work", wait_until="load", timeout=30000)
        await asyncio.sleep(2)
        findings["your_work"] = {
            "final_url": page.url,
            "status": resp.status if resp else None,
            "redirected_to_login": "/login" in page.url,
        }
        print(f"  final url: {page.url}")
        print(f"  status: {resp.status if resp else 'n/a'}")

        # === 3. DOM signals ===
        dom_data = await page.evaluate("""() => {
            const candidates = {
                'avatar img': 'img[src*="avatar"], img[alt*="avatar" i]',
                'user-menu class': '[class*="user-menu" i], [class*="UserMenu" i]',
                'profile link': 'a[href*="/your-work"], a[href*="/profile"]',
                'logout link/button': 'a[href*="/logout"], button:has-text("Log out"), [data-action*="logout"]',
                'site-header-user': '[class*="site-header-user"]',
                'login link (should be ABSENT when logged in)': 'a[href="/login"]',
                'signup link (should be ABSENT)': 'a[href*="/sign-up"]',
            };
            const out = {};
            for (const [label, sel] of Object.entries(candidates)) {
                try {
                    const el = document.querySelector(sel);
                    out[label] = el ? {exists: true, html: el.outerHTML.slice(0, 200)} : {exists: false};
                } catch (e) {
                    out[label] = {error: e.message};
                }
            }
            // Capture any text node containing the username/email pattern
            const userHints = [];
            for (const el of document.querySelectorAll('header *, nav *')) {
                const text = el.textContent?.trim();
                if (text && text.length < 50 && (text.startsWith('@') || text.includes('Hi,'))) {
                    userHints.push(text);
                }
            }
            out['_username_hints'] = userHints.slice(0, 5);
            return out;
        }""")
        findings["dom_signals"] = dom_data
        print("\n[dom-signals]")
        for k, v in dom_data.items():
            if isinstance(v, dict) and v.get("exists"):
                print(f"  ✓ {k}")
            elif isinstance(v, dict) and not v.get("exists"):
                print(f"  ✗ {k}")
            else:
                print(f"  - {k}: {v}")

        # === 4. Test API endpoints commonly used for auth check ===
        print("\n[api-probe]")
        api_results = {}
        for path in ["/api/v1/me", "/users/me", "/me", "/api/me", "/account.json"]:
            try:
                api_resp = await page.evaluate(f"""async () => {{
                    try {{
                        const r = await fetch('{path}', {{
                            credentials: 'include',
                            headers: {{ 'Accept': 'application/json' }}
                        }});
                        return {{
                            status: r.status,
                            ok: r.ok,
                            ct: r.headers.get('content-type'),
                            body: (await r.text()).slice(0, 300)
                        }};
                    }} catch (e) {{ return {{error: e.message}}; }}
                }}""")
                api_results[path] = api_resp
                print(f"  {path}: status={api_resp.get('status')}  ct={api_resp.get('ct', '')[:30]}")
            except Exception as e:
                api_results[path] = {"error": str(e)}
        findings["api_probe"] = api_results

        # === 5. Cross-check: visit /login and see if redirected away ===
        print("\n[login-page-redirect]")
        resp = await page.goto("https://codepen.io/login", wait_until="load", timeout=30000)
        await asyncio.sleep(2)
        findings["login_page_redirect"] = {
            "final_url": page.url,
            "status": resp.status if resp else None,
            "redirected_away": "/login" not in page.url,
        }
        print(f"  visiting /login → final url: {page.url}")
        print(f"  redirected away: {'/login' not in page.url}")

        # === 6. Anonymous comparison — for one signal, do an anon request via JS ===
        print("\n[anonymous-comparison]")
        anon_check = await page.evaluate("""async () => {
            // Open new fetch without cookies (sort of — fetch with credentials:'omit')
            try {
                const r = await fetch('/your-work', { credentials: 'omit', redirect: 'manual' });
                return { status: r.status, type: r.type, redirected: r.redirected };
            } catch (e) { return { error: e.message }; }
        }""")
        findings["anon_request"] = anon_check
        print(f"  /your-work without cookies: {anon_check}")

    # Write results
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(findings, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n=== full report saved to {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
