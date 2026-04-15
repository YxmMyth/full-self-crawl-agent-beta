"""Shared helpers for interaction tools (click/input/press_key/scroll/go_back).

Common patterns:
- Element lookup via selector_map
- URL change detection → auto-attach browse snapshot
- Occlusion pre-check via elementFromPoint
"""

from __future__ import annotations

from typing import Any

from src.browser.context import ToolContext
from src.browser.dom_settle import wait_for_content
from src.browser.page_repr import build_page_repr
from src.utils.logging import get_logger

logger = get_logger(__name__)


async def locate_element(ctx: ToolContext, target: int) -> tuple[Any, str | None]:
    """Look up an element by its index number.

    Returns:
        (locator, error_message). If error, locator is None.
    """
    selector = ctx.get_selector(target)
    if selector is None:
        return None, f"Element [{target}] not found. Use browse() to refresh the page snapshot."

    page = ctx.page
    locator = page.locator(selector)

    # Check element exists
    count = await locator.count()
    if count == 0:
        return None, f"Element [{target}] no longer exists. The page may have changed — use browse() to refresh."

    return locator, None


async def check_occlusion(ctx: ToolContext, target: int) -> str | None:
    """Pre-check if element is obscured by another element.

    Returns warning message if occluded, None if clear.
    """
    selector = ctx.get_selector(target)
    if not selector:
        return None

    try:
        result = await ctx.page.evaluate(f"""
            () => {{
                const el = document.querySelector('{selector}');
                if (!el) return {{ occluded: false }};
                const rect = el.getBoundingClientRect();
                const cx = rect.left + rect.width / 2;
                const cy = rect.top + rect.height / 2;
                const topEl = document.elementFromPoint(cx, cy);
                if (!topEl || topEl === el || el.contains(topEl) || topEl.contains(el)) {{
                    return {{ occluded: false }};
                }}
                return {{ occluded: true, by: topEl.tagName + (topEl.className ? '.' + topEl.className.split(' ')[0] : '') }};
            }}
        """)
        if result.get("occluded"):
            return f"Warning: element [{target}] may be obscured by {result['by']}"
    except Exception:
        pass
    return None


async def detect_navigation_and_snapshot(
    ctx: ToolContext,
    url_before: str,
    action_description: str,
) -> str:
    """Check if URL changed after an interaction. If so, wait and return new page snapshot.

    Returns:
        Additional text to append to the tool result (browse snapshot or empty).
    """
    page = ctx.page
    url_after = page.url

    if url_after != url_before:
        # URL changed — wait for content and return snapshot
        await wait_for_content(page)
        snapshot = await build_page_repr(page, ctx)
        return f"\n\nURL changed: {url_before} → {url_after}\n\n{snapshot}"

    return ""
