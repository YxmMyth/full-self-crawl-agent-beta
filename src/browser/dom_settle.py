"""DOM settle — wait for page content to be ready for extraction.

Simple content-readiness polling:
  1. page.goto(url, wait_until='load') handles resource loading
  2. Immediately check if page has enough content
  3. If thin, poll every 0.5s until content appears or timeout

No MutationObserver needed — we care about "is there enough content",
not "has the DOM stopped changing".

See: AgentSession设计.md §7.3
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from src.utils.logging import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = get_logger(__name__)

# Lightweight JS to measure content readiness
_CONTENT_CHECK_JS = """
() => {
    const body = document.body;
    if (!body) return { textLen: 0, elementCount: 0, interactiveCount: 0 };

    const text = (body.innerText || '').trim();
    const elementCount = body.querySelectorAll('*').length;
    const interactiveCount = body.querySelectorAll(
        'a[href], button, input, textarea, select, [role="button"], [role="link"]'
    ).length;

    return {
        textLen: text.length,
        elementCount,
        interactiveCount,
    };
}
"""

# Thresholds: page is "ready" if either is met
_MIN_TEXT_LEN = 200
_MIN_INTERACTIVE = 5


async def check_content(page: Page) -> dict:
    """Check if page has enough rendered content."""
    try:
        return await page.evaluate(_CONTENT_CHECK_JS)
    except Exception:
        return {"textLen": 0, "elementCount": 0, "interactiveCount": 0}


def _content_sufficient(content: dict) -> bool:
    """Is the page content rich enough to extract?"""
    return (
        content.get("textLen", 0) >= _MIN_TEXT_LEN
        or content.get("interactiveCount", 0) >= _MIN_INTERACTIVE
    )


async def wait_for_content(
    page: Page,
    max_wait: float = 10.0,
    poll_interval: float = 0.5,
) -> dict:
    """Wait until page has sufficient content for extraction.

    For static/SSR pages: first check passes immediately (0 wait).
    For SPAs: polls until JS renders enough content.

    Returns:
        {'status': 'ready'|'thin_content', 'content': {...}, 'waited': float}
    """
    elapsed = 0.0

    # First check — immediate, covers static sites
    content = await check_content(page)
    if _content_sufficient(content):
        logger.debug(
            f"Content ready immediately (text={content['textLen']}, "
            f"interactive={content['interactiveCount']})",
        )
        return {"status": "ready", "content": content, "waited": 0.0}

    # Poll for SPA content
    logger.info(
        f"Thin content (text={content['textLen']}, "
        f"interactive={content['interactiveCount']}), polling...",
        extra={"url": page.url},
    )

    while elapsed < max_wait:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        content = await check_content(page)
        if _content_sufficient(content):
            logger.info(
                f"Content ready after {elapsed:.1f}s "
                f"(text={content['textLen']}, interactive={content['interactiveCount']})",
            )
            return {"status": "ready", "content": content, "waited": elapsed}

    logger.warning(
        f"Content still thin after {elapsed:.1f}s "
        f"(text={content['textLen']}, interactive={content['interactiveCount']})",
    )
    return {"status": "thin_content", "content": content, "waited": elapsed}
