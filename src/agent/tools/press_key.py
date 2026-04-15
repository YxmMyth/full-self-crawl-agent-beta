"""press_key — send keyboard input.

Supports single keys and combinations: "Enter", "Escape", "Ctrl+A", "Shift+Tab".
Optional target element; otherwise sends to the focused element / page.

See: docs/工具重新设计共识.md §2.2
"""

from __future__ import annotations

from typing import Any

from src.agent.tools._interact_helpers import (
    locate_element, detect_navigation_and_snapshot,
)
from src.browser.context import ToolContext
from src.utils.logging import get_logger

logger = get_logger(__name__)

TOOL_NAME = "press_key"
TOOL_DESCRIPTION = (
    "Press a key or key combination.\n\n"
    "Examples: 'Enter', 'Escape', 'Tab', 'Ctrl+A', 'Shift+Tab', 'ArrowDown'\n\n"
    "With target: presses the key on that specific element.\n"
    "Without target: presses the key globally (affects focused element)."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "key": {
            "type": "string",
            "description": "Key to press (e.g. 'Enter', 'Escape', 'Ctrl+A').",
        },
        "target": {
            "type": "integer",
            "description": "Optional element number to focus before pressing.",
        },
    },
    "required": ["key"],
}


async def handle(ctx: ToolContext, **kwargs: Any) -> str:
    key: str = kwargs.get("key", "")
    target: int | None = kwargs.get("target")

    page = ctx.page
    url_before = page.url

    try:
        if target is not None:
            locator, error = await locate_element(ctx, target)
            if error:
                return error
            await locator.press(key, timeout=5000)
            result = f"Pressed '{key}' on [{target}]"
        else:
            await page.keyboard.press(key)
            result = f"Pressed '{key}'"

    except Exception as e:
        return f"Failed to press '{key}': {e}"

    nav_result = await detect_navigation_and_snapshot(ctx, url_before, f"press_key '{key}'")
    if nav_result:
        result += nav_result
    return result
