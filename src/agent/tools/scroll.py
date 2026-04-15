"""scroll — scroll the page or a specific container.

Supports vertical and horizontal scrolling, by screen-heights or custom amount.
Returns scroll position percentage.

See: docs/工具重新设计共识.md §2.2
"""

from __future__ import annotations

from typing import Any

from src.browser.context import ToolContext
from src.utils.logging import get_logger

logger = get_logger(__name__)

TOOL_NAME = "scroll"
TOOL_DESCRIPTION = (
    "Scroll the page or a specific container element.\n\n"
    "Default: scroll down by 1 screen height.\n"
    "Returns the scroll position so you know how much content is left.\n\n"
    "Use browse() after scrolling to see the newly visible content."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "direction": {
            "type": "string",
            "enum": ["up", "down", "left", "right"],
            "description": "Scroll direction (default 'down').",
        },
        "amount": {
            "type": "number",
            "description": "Number of screen-heights to scroll (default 1). Use 0.5 for half-screen.",
        },
        "target": {
            "type": "integer",
            "description": "Element number of a scrollable container. Omit to scroll the whole page.",
        },
    },
    "required": [],
}


async def handle(ctx: ToolContext, **kwargs: Any) -> str:
    direction: str = kwargs.get("direction", "down")
    amount: float = kwargs.get("amount", 1.0)
    target: int | None = kwargs.get("target")

    page = ctx.page

    # Calculate scroll delta
    viewport = page.viewport_size or {"width": 1280, "height": 800}
    if direction in ("up", "down"):
        delta = int(viewport["height"] * amount)
        if direction == "up":
            delta = -delta
        scroll_x, scroll_y = 0, delta
    else:
        delta = int(viewport["width"] * amount)
        if direction == "left":
            delta = -delta
        scroll_x, scroll_y = delta, 0

    try:
        if target is not None:
            # Scroll within a container element
            selector = ctx.get_selector(target)
            if not selector:
                return f"Element [{target}] not found. Use browse() to refresh."

            await page.evaluate(f"""
                () => {{
                    const el = document.querySelector('{selector}');
                    if (el) el.scrollBy({scroll_x}, {scroll_y});
                }}
            """)
        else:
            # Scroll the page
            await page.evaluate(f"window.scrollBy({scroll_x}, {scroll_y})")

    except Exception as e:
        return f"Scroll failed: {e}"

    # Get position info
    try:
        pos = await page.evaluate("""
            () => {
                const scrollY = window.scrollY;
                const viewportH = window.innerHeight;
                const totalH = document.documentElement.scrollHeight;
                const percent = totalH > viewportH
                    ? Math.round(scrollY / (totalH - viewportH) * 100)
                    : 0;
                return { scrollY, viewportH, totalH, percent };
            }
        """)
        position_str = (
            f"Position: ~{pos['percent']}% "
            f"({pos['scrollY']}px of ~{pos['totalH']}px)"
        )
    except Exception:
        position_str = "Position: unknown"

    container = f" in [{target}]" if target else ""
    return f"Scrolled {direction} {amount}x{container}. {position_str}\nUse browse() to see the updated content."
