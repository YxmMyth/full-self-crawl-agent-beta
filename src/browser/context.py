"""Shared browser context for all tools within a session.

Holds the page reference, element selector map, network captures,
and tab management. All browser tools read/write this shared state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.utils.logging import get_logger

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext as PlaywrightContext, Page
    from src.runtime.human_assist import HumanAssistGateway

logger = get_logger(__name__)


@dataclass
class ToolContext:
    """Shared state for all browser tools in a session.

    Created by BrowserManager, passed to every tool call.
    """

    # Playwright browser context (owns cookies, storage, etc.)
    pw_context: PlaywrightContext

    # Element index: number → CSS selector for re-finding elements
    # Rebuilt every browse()/interaction, not persistent across pages
    selector_map: dict[int, str] = field(default_factory=dict)

    # Previous element IDs — for marking new elements with *
    previous_element_ids: set[str] = field(default_factory=set)

    # Network captures — filled by passive page.on('response') listener
    # Import CapturedRequest from network_capture to avoid circular
    network_captures: list = field(default_factory=list)
    network_filtered_count: dict[str, int] = field(
        default_factory=lambda: {"tracking": 0, "static": 0}
    )

    # Tab management
    tabs: list[Page] = field(default_factory=list)
    active_tab_idx: int = 0

    # Human assistance gateway (set by main.py after launch). Tools that need
    # to pause for human input call ctx.human_assist.request(reason, page).
    # Optional — None means assist is unavailable in this run.
    human_assist: HumanAssistGateway | None = None

    # ── Page access ──────────────────────────────────────

    @property
    def page(self) -> Page:
        """The currently active page (tab)."""
        if not self.tabs:
            raise RuntimeError("No tabs open. Browser not initialized?")
        return self.tabs[self.active_tab_idx]

    @property
    def tab_count(self) -> int:
        return len(self.tabs)

    # ── Tab management ───────────────────────────────────

    async def new_tab(self, url: str | None = None) -> Page:
        """Open a new tab, optionally navigating to url."""
        page = await self.pw_context.new_page()
        self.tabs.append(page)
        self.active_tab_idx = len(self.tabs) - 1
        if url:
            await page.goto(url, wait_until="domcontentloaded")
        logger.debug(f"New tab #{self.active_tab_idx + 1} opened", extra={"url": url})
        return page

    def switch_tab(self, tab_number: int) -> Page:
        """Switch to tab N (1-based). Returns the page."""
        idx = tab_number - 1
        if idx < 0 or idx >= len(self.tabs):
            raise ValueError(
                f"Tab {tab_number} does not exist. "
                f"Open tabs: {len(self.tabs)}"
            )
        self.active_tab_idx = idx
        logger.debug(f"Switched to tab #{tab_number}")
        return self.tabs[idx]

    async def close_tab(self, tab_number: int | None = None) -> None:
        """Close a tab (1-based). Defaults to current tab."""
        idx = (tab_number - 1) if tab_number else self.active_tab_idx
        if idx < 0 or idx >= len(self.tabs):
            return
        page = self.tabs.pop(idx)
        await page.close()
        # Adjust active index
        if self.tabs:
            self.active_tab_idx = min(self.active_tab_idx, len(self.tabs) - 1)
        logger.debug(f"Closed tab #{idx + 1}, {len(self.tabs)} remaining")

    def tab_list(self) -> list[dict[str, str]]:
        """Return info about all open tabs."""
        result = []
        for i, page in enumerate(self.tabs):
            result.append({
                "tab": i + 1,
                "url": page.url,
                "title": page.url,  # title requires await, use url as fallback
                "active": i == self.active_tab_idx,
            })
        return result

    # ── Network capture helpers ──────────────────────────

    def clear_network(self) -> None:
        """Clear captured network requests."""
        self.network_captures.clear()
        self.network_filtered_count = {"tracking": 0, "static": 0}

    # ── Element index helpers ────────────────────────────

    def clear_selector_map(self) -> None:
        """Save current element IDs for new-element marking, then clear."""
        # Store current IDs so next indexing can mark new ones with *
        self.previous_element_ids = set(
            str(n) for n in self.selector_map.keys()
        )
        self.selector_map.clear()

    def get_selector(self, element_number: int) -> str | None:
        """Look up CSS selector for an element number."""
        return self.selector_map.get(element_number)

    # ── Popup auto-close ─────────────────────────────────

    def setup_popup_close(self) -> None:
        """Register context.on('page') to auto-close any popup the page tries to open.

        Why: agent's mental model is single-tab (1 session = 1 tab). Site
        target="_blank" links and JS-driven window.open() create extra tabs
        that agent has no use for and historically just leak. We close them
        immediately. The agent will see the click action complete but the
        active tab unchanged — which is the correct intent: navigate-the-current-tab.

        ToolContext.tabs / new_tab / switch_tab / close_tab methods stay in
        place for future concurrent-session allocators (1 session = 1 tab,
        managed by infrastructure, NOT by the agent).
        """

        async def _close_popup(page: Page) -> None:
            url = page.url or "about:blank"
            try:
                await page.close()
            except Exception as e:
                logger.warning(f"Popup auto-close failed: {e}", extra={"url": url})
                return
            logger.info(f"Popup auto-closed: {url[:120]}", extra={"url": url})

        self.pw_context.on("page", _close_popup)
