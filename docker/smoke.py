"""Container smoke test — NOT a mission (no DB / LLM needed).

Launches headed Camoufox on the container's OWN Xvfb (:99), renders a page,
and holds so the operator can watch it live via the published noVNC port.
Proves the run image's browser + display stack work end to end inside a
container.

Run:
  docker run -d --rm -p 127.0.0.1:6080:6080 --name fsc-smoke \
      fsc-run:dev python /smoke/smoke.py
Then open http://localhost:6080/vnc.html and click Connect.
"""
import asyncio
import os
import sys

PAGE = (
    "data:text/html;charset=utf-8,"
    "<body style='margin:0;background:%230b3d4a;color:%237fffd4;font-family:sans-serif;text-align:center'>"
    "<h1 style='font-size:64px;margin-top:140px'>容器里的浏览器 OK</h1>"
    "<p style='font-size:28px;color:%23e0e0e0'>Camoufox is rendering inside the container — live via VNC</p>"
    "</body>"
)


async def main() -> None:
    print(f"[smoke] DISPLAY={os.environ.get('DISPLAY')!r}", flush=True)
    from camoufox.async_api import AsyncCamoufox

    print("[smoke] launching headed Camoufox ...", flush=True)
    async with AsyncCamoufox(headless=False) as b:
        # AsyncCamoufox yields a Browser (non-persistent) — new_page() works.
        page = await b.new_page()
        await page.goto(PAGE)
        print("[smoke] OK — Camoufox launched and rendered a page.", flush=True)
        print("[smoke] holding 600s for VNC viewing; `docker stop fsc-smoke` to end.", flush=True)
        await asyncio.sleep(600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:  # noqa: BLE001
        import traceback
        print(f"[smoke] FAILED: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)
