"""browse — page content snapshot + multi-tab navigation.

Navigate to a URL (or refresh current), wait for content, return Markdown+HTML
hybrid representation with Data Signals and Network summary sections.

Parameters:
  url:     optional — navigate to this URL. Omit to snapshot current page.
  new_tab: optional — open url in a new tab (default false).
  tab:     optional — switch to tab N (1-based) before snapshot.
  visual:  optional — take screenshot + vision LLM description (default false).

See: docs/工具重新设计共识.md §2.2, docs/browse工具深度设计报告.md
"""

from __future__ import annotations

import base64
from typing import Any

from src.browser.context import ToolContext
from src.browser.dom_settle import wait_for_content
from src.browser.network_capture import setup_network_capture
from src.browser.page_repr import build_page_repr
from src.utils.logging import get_logger

logger = get_logger(__name__)

TOOL_NAME = "browse"
TOOL_DESCRIPTION = (
    "Navigate to a URL and get a structured snapshot of the page content.\n\n"
    "Returns Markdown+HTML hybrid: static text as Markdown, interactive elements as "
    "numbered [N]<tag>text</tag>. Use the numbers with click/input/scroll.\n\n"
    "Also shows:\n"
    "- Data Signals: embedded JSON, framework data objects (clues for browser_eval)\n"
    "- Network Requests: captured API calls (clues for bash curl replay)\n"
    "- Scroll Position: how much content is above/below\n\n"
    "Tab management:\n"
    "- new_tab=true: open URL in a new tab\n"
    "- tab=N: switch to tab N before taking snapshot\n"
    "- Omit url: refresh snapshot of current page (after interactions)\n\n"
    "visual=true: take a screenshot and get a vision-AI description of what's "
    "visible. Use when you need to understand visual layout or non-text content."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "URL to navigate to. Omit to snapshot current page.",
        },
        "new_tab": {
            "type": "boolean",
            "description": "Open URL in a new tab (default false).",
        },
        "tab": {
            "type": "integer",
            "description": "Switch to tab N (1-based) before snapshot.",
        },
        "visual": {
            "type": "boolean",
            "description": "Take screenshot + vision AI description (default false).",
        },
    },
    "required": [],
}


async def handle(ctx: ToolContext, **kwargs: Any) -> str:
    url: str | None = kwargs.get("url")
    new_tab: bool = kwargs.get("new_tab", False)
    tab: int | None = kwargs.get("tab")
    visual: bool = kwargs.get("visual", False)

    try:
        # Tab management
        if tab is not None:
            try:
                ctx.switch_tab(tab)
            except ValueError as e:
                return str(e)

        if new_tab and url:
            page = await ctx.new_tab()
            # Setup network capture on new tab
            from src.browser.network_capture import setup_network_capture as setup_nc
            setup_nc(page, ctx)
            # Setup dialog handler
            async def handle_dialog(dialog):
                await dialog.accept()
            page.on("dialog", handle_dialog)

        page = ctx.page

        # Navigate if URL provided
        if url:
            ctx.clear_network()  # fresh captures for new navigation
            try:
                await page.goto(url, wait_until="load", timeout=30000)
            except Exception as e:
                error_msg = str(e)
                if "timeout" in error_msg.lower():
                    # Page partially loaded — still try to extract
                    logger.warning(f"Navigation timeout for {url}, extracting partial content")
                else:
                    return f"Navigation failed: {error_msg}"

            # Wait for content readiness
            settle = await wait_for_content(page)
            if settle["status"] == "thin_content":
                logger.warning("Page has thin content after waiting")

        # Build page representation
        repr_text = await build_page_repr(page, ctx)

        # Tab info
        if ctx.tab_count > 1:
            tabs_info = f"\n--- Tabs ({ctx.tab_count}) ---\n"
            for t in ctx.tab_list():
                marker = "→" if t["active"] else " "
                tabs_info += f" {marker} Tab {t['tab']}: {t['url'][:80]}\n"
            repr_text += tabs_info

        # Visual mode: screenshot + vision LLM description
        if visual:
            visual_text = await _handle_visual(page)
            repr_text += f"\n\n--- Visual Description ---\n{visual_text}"

        return repr_text

    except Exception as e:
        logger.error(f"browse error: {e}", extra={"url": url, "error": str(e)})
        return f"browse error: {e}"


async def _handle_visual(page: Any) -> str:
    """Take screenshot and get vision LLM description."""
    try:
        screenshot_bytes = await page.screenshot(type="jpeg", quality=80)
        image_b64 = base64.b64encode(screenshot_bytes).decode("ascii")

        from src.llm.client import LLMClient
        client = LLMClient()
        description = await client.describe_image(image_b64)
        await client.close()

        if description:
            return description
        return "(Vision model returned empty description)"

    except Exception as e:
        logger.warning(f"Visual mode failed: {e}")
        return f"(Visual mode failed: {e})"
