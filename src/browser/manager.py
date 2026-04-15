"""Browser lifecycle management.

4-level connection priority:
  1. AsyncCamoufox(headless) — default, anti-detection Firefox
  2. BROWSER_WS_URL → playwright.firefox.connect(ws_url) — remote Camoufox
  3. BROWSER_CDP_URL → playwright.connect_over_cdp(url) — remote Chromium
  4. playwright.chromium.launch() — local Chromium fallback

See: 架构共识文档.md §六 浏览器环境与策略
"""

from __future__ import annotations

from typing import Any

from src.browser.context import ToolContext
from src.browser.network_capture import setup_network_capture
from src.config import Config
from src.utils.logging import get_logger

logger = get_logger(__name__)


class BrowserManager:
    """Manages browser lifecycle: launch, reset, close.

    Creates and maintains the ToolContext shared by all tools.
    """

    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None
        self._camoufox_ctx: Any = None  # AsyncCamoufox context manager
        self._browser_type: str = "camoufox"  # camoufox / chromium
        self._headed: bool = False
        self._proxy: str | None = None
        self.ctx: ToolContext | None = None

    async def launch(
        self,
        browser_type: str | None = None,
        headed: bool = False,
        proxy: str | None = None,
    ) -> ToolContext:
        """Launch browser and create ToolContext.

        Follows 4-level priority chain from config/params.
        """
        self._browser_type = browser_type or "camoufox"
        self._headed = headed
        self._proxy = proxy

        # Try connection methods in priority order
        page = None
        pw_context = None

        # Priority 1: Remote Camoufox via WebSocket
        if Config.BROWSER_WS_URL and not browser_type:
            try:
                page, pw_context = await self._connect_ws(Config.BROWSER_WS_URL)
                logger.info("Connected via WebSocket", extra={"url": Config.BROWSER_WS_URL})
            except Exception as e:
                logger.warning(f"WS connection failed, trying next: {e}")

        # Priority 2: Remote Chromium via CDP
        if page is None and Config.BROWSER_CDP_URL and not browser_type:
            try:
                page, pw_context = await self._connect_cdp(Config.BROWSER_CDP_URL)
                logger.info("Connected via CDP", extra={"url": Config.BROWSER_CDP_URL})
            except Exception as e:
                logger.warning(f"CDP connection failed, trying next: {e}")

        # Priority 3: Local Camoufox (default)
        if page is None and self._browser_type == "camoufox":
            try:
                page, pw_context = await self._launch_camoufox()
                logger.info(f"Launched Camoufox (headed={self._headed})")
            except Exception as e:
                logger.warning(f"Camoufox launch failed, falling back to Chromium: {e}")
                self._browser_type = "chromium"

        # Priority 4: Local Chromium fallback
        if page is None:
            page, pw_context = await self._launch_chromium()
            logger.info(f"Launched Chromium fallback (headed={self._headed})")

        # Create ToolContext
        self.ctx = ToolContext(pw_context=pw_context, tabs=[page])

        # Setup infrastructure
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

        All params optional — omitted params keep current values.
        Bare call (no params) = clean restart same config.
        """
        bt = browser_type or self._browser_type
        hd = headed if headed is not None else self._headed
        px = proxy if proxy is not None else self._proxy

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
        """Launch local Camoufox browser."""
        from camoufox.async_api import AsyncCamoufox

        kwargs: dict[str, Any] = {
            "headless": not self._headed,
        }
        if self._proxy:
            kwargs["proxy"] = {"server": self._proxy}

        self._camoufox_ctx = AsyncCamoufox(**kwargs)
        browser = await self._camoufox_ctx.__aenter__()
        self._browser = browser

        # Camoufox returns a browser; create a context and page
        pw_context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = pw_context.pages[0] if pw_context.pages else await pw_context.new_page()

        return page, pw_context

    async def _launch_chromium(self) -> tuple:
        """Launch local Chromium as fallback."""
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()

        launch_kwargs: dict[str, Any] = {
            "headless": not self._headed,
        }
        if self._proxy:
            launch_kwargs["proxy"] = {"server": self._proxy}

        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        pw_context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = await pw_context.new_page()

        return page, pw_context

    async def _connect_ws(self, ws_url: str) -> tuple:
        """Connect to remote browser via WebSocket."""
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.firefox.connect(ws_url)

        pw_context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
        )
        page = await pw_context.new_page()

        return page, pw_context

    async def _connect_cdp(self, cdp_url: str) -> tuple:
        """Connect to remote Chromium via CDP."""
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(cdp_url)

        pw_context = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
        )
        page = pw_context.pages[0] if pw_context.pages else await pw_context.new_page()

        return page, pw_context

    @staticmethod
    def _setup_dialog_handler(page: Any) -> None:
        """Auto-accept dialogs (alert/confirm/prompt). Transparent to agent."""
        async def handle_dialog(dialog: Any) -> None:
            logger.debug(f"Dialog auto-accepted: {dialog.type} '{dialog.message[:80]}'")
            await dialog.accept()

        page.on("dialog", handle_dialog)
