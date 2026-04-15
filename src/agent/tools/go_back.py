"""go_back — navigate browser history back one step.

Auto-attaches a browse snapshot of the previous page.

See: docs/工具重新设计共识.md §2.2
"""

from __future__ import annotations

from typing import Any

from src.browser.context import ToolContext
from src.browser.dom_settle import wait_for_content
from src.browser.page_repr import build_page_repr
from src.utils.logging import get_logger

logger = get_logger(__name__)

TOOL_NAME = "go_back"
TOOL_DESCRIPTION = (
    "Go back to the previous page in browser history.\n\n"
    "Automatically returns a snapshot of the page you land on."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {},
    "required": [],
}


async def handle(ctx: ToolContext, **kwargs: Any) -> str:
    page = ctx.page

    # Check if there's history to go back to
    url_before = page.url

    try:
        response = await page.go_back(wait_until="load", timeout=15000)
    except Exception as e:
        return f"Cannot go back: {e}"

    url_after = page.url
    if url_after == url_before:
        return "Cannot go back — no history."

    await wait_for_content(page)
    snapshot = await build_page_repr(page, ctx)

    return f"Went back to: {url_after}\n\n{snapshot}"
