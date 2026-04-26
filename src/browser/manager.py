"""Browser lifecycle management.

Connection priority (in order):
  1. AsyncCamoufox(persistent_context=True, user_data_dir=per-domain) — default
  2. BROWSER_WS_URL → playwright.firefox.connect(ws_url) — remote Camoufox
  3. BROWSER_CDP_URL → playwright.connect_over_cdp(url) — remote Chromium
  4. playwright.chromium.launch() — local Chromium fallback (no persistence)

One BrowserManager per domain — its profile is persisted at
`artifacts/_profiles/{domain}/` across runs (cookies, localStorage, IndexedDB).
Within same engine, headed↔headless can switch freely without losing state.

See: 架构共识文档.md §六 浏览器环境与策略
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.browser.context import ToolContext
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
        """
        self._browser_type = browser_type or "camoufox"
        self._headed = headed
        self._proxy = proxy

        page = None
        pw_context = None

        # Priority 1: Remote Camoufox via WebSocket (no per-domain profile)
        if Config.BROWSER_WS_URL and not browser_type:
            try:
                page, pw_context = await self._connect_ws(Config.BROWSER_WS_URL)
                logger.info("Connected via WebSocket", extra={"url": Config.BROWSER_WS_URL})
            except Exception as e:
                logger.warning(f"WS connection failed, trying next: {e}")

        # Priority 2: Remote Chromium via CDP (no per-domain profile)
        if page is None and Config.BROWSER_CDP_URL and not browser_type:
            try:
                page, pw_context = await self._connect_cdp(Config.BROWSER_CDP_URL)
                logger.info("Connected via CDP", extra={"url": Config.BROWSER_CDP_URL})
            except Exception as e:
                logger.warning(f"CDP connection failed, trying next: {e}")

        # Priority 3: Local Camoufox with persistent context (default)
        if page is None and self._browser_type == "camoufox":
            try:
                page, pw_context = await self._launch_camoufox()
                logger.info(
                    f"Launched Camoufox (headed={self._headed}, "
                    f"profile={self.profile_dir})"
                )
            except Exception as e:
                logger.warning(f"Camoufox launch failed, falling back to Chromium: {e}")
                self._browser_type = "chromium"

        # Priority 4: Local Chromium fallback (no persistent context — emergency)
        if page is None:
            page, pw_context = await self._launch_chromium()
            logger.warning(
                "Launched Chromium fallback — no profile persistence"
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

        Same engine + same profile → cookies/state preserved.
        Switching engine on same domain risks "new device" re-challenge.
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
            self._pw_context = None  # Owned by camoufox_ctx, already cleaned
        elif self._pw_context:
            # Standalone context (chromium fallback / WS / CDP)
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

        persistent_context=True → AsyncCamoufox returns BrowserContext directly,
        no separate Browser object.
        """
        from camoufox.async_api import AsyncCamoufox

        kwargs: dict[str, Any] = {
            "headless": not self._headed,
            "window": DEFAULT_WINDOW,
            "os": "windows",
            "humanize": True,
            "persistent_context": True,
            "user_data_dir": str(self.profile_dir),
            "viewport": DEFAULT_VIEWPORT,
        }
        if self._proxy:
            kwargs["proxy"] = {"server": self._proxy}

        self._camoufox_ctx = AsyncCamoufox(**kwargs)
        pw_context = await self._camoufox_ctx.__aenter__()
        self._pw_context = pw_context
        page = pw_context.pages[0] if pw_context.pages else await pw_context.new_page()

        return page, pw_context

    async def _launch_chromium(self) -> tuple:
        """Launch local Chromium as fallback (non-persistent — emergency mode)."""
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()

        launch_kwargs: dict[str, Any] = {
            "headless": not self._headed,
        }
        if self._proxy:
            launch_kwargs["proxy"] = {"server": self._proxy}

        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        pw_context = await self._browser.new_context(
            viewport=DEFAULT_VIEWPORT,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        self._pw_context = pw_context
        page = await pw_context.new_page()

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
