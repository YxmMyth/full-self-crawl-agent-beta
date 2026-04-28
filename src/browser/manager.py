"""Browser lifecycle management.

Connection priority (in order):
  1. BROWSER_WS_URL → playwright.firefox.connect(ws_url) — remote Camoufox
  2. BROWSER_CDP_URL → playwright.connect_over_cdp(url) — remote Chromium
  3. AsyncCamoufox(persistent_context=True, user_data_dir=per-domain) — default

If all three fail, launch raises RuntimeError. There is intentionally no
silent fallback to vanilla Playwright Chromium — it would have
`navigator.webdriver=true` and trigger Cloudflare instantly, masking the
real failure and pulling the agent into a reset loop.
See: docs/postmortem-2026-04-27-deferred.md, docs/fix-plan-2026-04-27-P0.md

One BrowserManager per domain — its profile is persisted at
`artifacts/_profiles/{domain}/` across runs (cookies, localStorage, IndexedDB).
Within same engine, headed↔headless can switch freely without losing state.

See: 架构共识文档.md §六 浏览器环境与策略
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.browser.context import ToolContext
from src.browser.firefox_prefs import (
    SESSION_RESTORE_OFF,
    sanitize_profile,
    write_user_js,
)
from src.browser.network_capture import setup_network_capture
from src.config import Config
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Sized for Windows 11 @ 150% scaling (WorkingArea ≈ 1707×1019).
# Viewport accounts for ~84px of Firefox chrome (tab bar + address bar).
DEFAULT_WINDOW = (1280, 900)
DEFAULT_VIEWPORT = {"width": 1280, "height": 816}


class BrowserManager:
    """Manages browser lifecycle: launch, reset, close.

    Creates and maintains the ToolContext shared by all tools.
    Tied to a single domain — profile persists per-domain across runs.
    """

    def __init__(self, domain: str) -> None:
        self.domain = domain
        self._playwright: Any = None
        self._browser: Any = None  # Set only for non-persistent (Browser type)
        self._pw_context: Any = None  # The active BrowserContext (always set after launch)
        self._camoufox_ctx: Any = None  # AsyncCamoufox context manager (for cleanup)
        self._browser_type: str = "camoufox"
        self._headed: bool = True
        self._proxy: str | None = None
        self.ctx: ToolContext | None = None
        # Gateway is set by main.py once; auto-attached to ctx on every launch
        # so browser_reset doesn't lose the assist channel.
        self.gateway: Any = None

    @property
    def profile_dir(self) -> Path:
        """Per-domain persistent profile dir. Auto-created on access."""
        d = Config.ARTIFACTS_DIR / "_profiles" / self.domain
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def launch(
        self,
        browser_type: str | None = None,
        headed: bool = True,
        proxy: str | None = None,
    ) -> ToolContext:
        """Launch browser and create ToolContext.

        Defaults to headed so human_assist can surface a visible window.

        Only `camoufox` is supported. If launch fails, raises RuntimeError —
        we do NOT silently fall back to vanilla Chromium because that would
        be detected by Cloudflare instantly, masking the real failure and
        sending the agent into a reset loop.
        """
        if browser_type and browser_type != "camoufox":
            raise ValueError(
                f"browser_type='{browser_type}' is not supported. "
                f"Only 'camoufox' is available. (Vanilla Chromium fallback was "
                f"removed on 2026-04-28 — see docs/fix-plan-2026-04-27-P0.md.)"
            )
        self._browser_type = "camoufox"
        self._headed = headed
        self._proxy = proxy

        page = None
        pw_context = None

        # Priority 1: Remote Camoufox via WebSocket (no per-domain profile)
        if Config.BROWSER_WS_URL:
            try:
                page, pw_context = await self._connect_ws(Config.BROWSER_WS_URL)
                logger.info("Connected via WebSocket", extra={"url": Config.BROWSER_WS_URL})
            except Exception as e:
                logger.warning(f"WS connection failed, trying next: {e}")

        # Priority 2: Remote Chromium via CDP (no per-domain profile)
        if page is None and Config.BROWSER_CDP_URL:
            try:
                page, pw_context = await self._connect_cdp(Config.BROWSER_CDP_URL)
                logger.info("Connected via CDP", extra={"url": Config.BROWSER_CDP_URL})
            except Exception as e:
                logger.warning(f"CDP connection failed, trying next: {e}")

        # Priority 3: Local Camoufox with persistent context (default)
        if page is None:
            page, pw_context = await self._launch_camoufox()
            logger.info(
                f"Launched Camoufox (headed={self._headed}, "
                f"profile={self.profile_dir})"
            )

        if page is None:
            raise RuntimeError(
                "All browser engines failed to launch. Check Camoufox install "
                "(`pip show camoufox`), profile dir permissions, or disk space."
            )

        self.ctx = ToolContext(pw_context=pw_context, tabs=[page])
        # Re-attach human_assist gateway across launches/resets
        if self.gateway is not None:
            self.ctx.human_assist = self.gateway

        self.ctx.setup_new_tab_listener()
        setup_network_capture(page, self.ctx)
        self._setup_dialog_handler(page)

        return self.ctx

    async def reset(
        self,
        browser_type: str | None = None,
        headed: bool | None = None,
        proxy: str | None = None,
    ) -> ToolContext:
        """Close current browser and relaunch with new config.

        Same profile → cookies/state preserved across reset.

        Closes non-active tabs first to keep sessionstore.jsonlz4 small —
        even though we disable session restore via prefs, smaller writes
        mean faster close.
        """
        bt = browser_type or self._browser_type
        hd = headed if headed is not None else self._headed
        px = proxy if proxy is not None else self._proxy

        # Close non-active tabs to minimize what gets written to sessionstore.
        if self.ctx and len(self.ctx.tabs) > 1:
            keep = self.ctx.tabs[self.ctx.active_tab_idx]
            for tab in list(self.ctx.tabs):
                if tab is not keep:
                    try:
                        await tab.close()
                    except Exception:
                        pass

        await self.close()
        return await self.launch(browser_type=bt, headed=hd, proxy=px)

    async def close(self) -> None:
        """Close browser and clean up all resources."""
        if self.ctx:
            for tab in self.ctx.tabs:
                try:
                    await tab.close()
                except Exception:
                    pass
            self.ctx = None

        if self._camoufox_ctx:
            try:
                await self._camoufox_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._camoufox_ctx = None
            self._pw_context = None  # Owned by camoufox_ctx, already cleaned
        elif self._pw_context:
            # Standalone context (WS / CDP) — Camoufox path goes through _camoufox_ctx above.
            try:
                await self._pw_context.close()
            except Exception:
                pass
            self._pw_context = None

        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        logger.info("Browser closed")

    def setup_page_listeners(self, page: Any) -> None:
        """Setup network capture and dialog handling on a new page/tab."""
        if self.ctx:
            setup_network_capture(page, self.ctx)
        self._setup_dialog_handler(page)

    # ── Private launch methods ───────────────────────────

    async def _launch_camoufox(self) -> tuple:
        """Launch local Camoufox with per-domain persistent context.

        Profile preparation (Tier 3 sanitize + Tier 2 user.js) happens before
        AsyncCamoufox — see firefox_prefs.py for the layered model.

        persistent_context=True → AsyncCamoufox returns BrowserContext directly,
        no separate Browser object.
        """
        from camoufox.async_api import AsyncCamoufox

        # Tier 3: clean stale volatile state (parent.lock, sessionstore-*) so a
        # prior unclean shutdown can't make this launch hang at session restore.
        sanitize_profile(self.profile_dir)

        # Tier 2: write user.js so Firefox reads our prefs at the very start
        # of boot, BEFORE it decides whether to restore. firefox_user_prefs
        # below is a defense-in-depth re-injection at runtime.
        write_user_js(self.profile_dir, SESSION_RESTORE_OFF)

        kwargs: dict[str, Any] = {
            "headless": not self._headed,
            "window": DEFAULT_WINDOW,
            "os": "windows",
            "humanize": True,
            "persistent_context": True,
            "user_data_dir": str(self.profile_dir),
            "viewport": DEFAULT_VIEWPORT,
            "firefox_user_prefs": SESSION_RESTORE_OFF,
        }
        if self._proxy:
            kwargs["proxy"] = {"server": self._proxy}

        self._camoufox_ctx = AsyncCamoufox(**kwargs)
        pw_context = await self._camoufox_ctx.__aenter__()
        self._pw_context = pw_context
        page = pw_context.pages[0] if pw_context.pages else await pw_context.new_page()

        return page, pw_context

    async def _connect_ws(self, ws_url: str) -> tuple:
        """Connect to remote browser via WebSocket (no profile persistence)."""
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.firefox.connect(ws_url)

        pw_context = await self._browser.new_context(viewport=DEFAULT_VIEWPORT)
        self._pw_context = pw_context
        page = await pw_context.new_page()

        return page, pw_context

    async def _connect_cdp(self, cdp_url: str) -> tuple:
        """Connect to remote Chromium via CDP."""
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(cdp_url)

        pw_context = (
            self._browser.contexts[0]
            if self._browser.contexts
            else await self._browser.new_context(viewport=DEFAULT_VIEWPORT)
        )
        self._pw_context = pw_context
        page = pw_context.pages[0] if pw_context.pages else await pw_context.new_page()

        return page, pw_context

    @staticmethod
    def _setup_dialog_handler(page: Any) -> None:
        """Auto-accept dialogs (alert/confirm/prompt). Transparent to agent."""
        async def handle_dialog(dialog: Any) -> None:
            logger.debug(f"Dialog auto-accepted: {dialog.type} '{dialog.message[:80]}'")
            await dialog.accept()

        page.on("dialog", handle_dialog)
