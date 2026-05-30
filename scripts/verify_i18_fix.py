"""Verify the I18 driver-crash fix: on playwright 1.59.0, a page that throws
errors (including a locationless one) must NOT crash the driver.

Pass criteria: after triggering page errors, the driver is still alive and can
evaluate JS. On 1.60.0 the driver process would have died here.

Run: .venv/Scripts/python.exe scripts/verify_i18_fix.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile


async def main() -> int:
    import importlib.metadata as md

    import playwright
    from camoufox.async_api import AsyncCamoufox

    print(f"playwright bundled at: {playwright.__file__}")
    print(f"playwright version: {md.version('playwright')}")

    # persistent_context=True → AsyncCamoufox yields a BrowserContext (has .pages),
    # matching how src/browser/manager.py actually launches. Without it you get a
    # Browser object (no .pages).
    with tempfile.TemporaryDirectory() as profile:
        async with AsyncCamoufox(
            headless=True, os="windows", humanize=True,
            persistent_context=True, user_data_dir=profile,
        ) as ctx:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("about:blank")
            print("launch OK, eval 1+1 =", await page.evaluate("1 + 1"))

            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            # 1) ordinary uncaught error (has a location)
            await page.evaluate(
                "setTimeout(() => { throw new Error('normal uncaught'); }, 0)"
            )
            # 2) locationless error — an ErrorEvent with no filename/lineno, the
            #    shape that on 1.60.0 makes pageError.location undefined and
            #    crashes the driver.
            await page.evaluate(
                """
                window.dispatchEvent(new ErrorEvent('error', {
                    message: 'Script error.',
                    error: new Error('locationless'),
                    filename: '', lineno: 0, colno: 0
                }));
                """
            )
            await asyncio.sleep(1.0)  # let events flush

            # THE TEST: is the driver still alive after those page errors?
            alive = await page.evaluate("2 + 2")
            print("after page errors, eval 2+2 =", alive)
            print(f"pageerror events seen: {len(errors)} -> {errors}")

    if alive == 4:
        print("RESULT: PASS — driver survived page errors (I18 fixed by downgrade)")
        return 0
    print("RESULT: FAIL — driver did not return correct value")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as e:
        print(f"RESULT: FAIL — exception (driver likely crashed): {e!r}")
        sys.exit(1)
