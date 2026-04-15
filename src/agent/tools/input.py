"""input — type text or select an option in a form element.

Auto-detects element type (text input → fill, select → choose option).
Autocomplete detection: delays 400ms for suggestion list on combobox fields.
Value verification: checks actual value after input, warns on mismatch.

See: docs/工具重新设计共识.md §2.2
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.agent.tools._interact_helpers import (
    locate_element, detect_navigation_and_snapshot,
)
from src.browser.context import ToolContext
from src.utils.logging import get_logger

logger = get_logger(__name__)

TOOL_NAME = "input"
TOOL_DESCRIPTION = (
    "Type text into an input field or select an option from a dropdown.\n\n"
    "Automatically detects the element type:\n"
    "- Text input/textarea: clears existing value, types the new value\n"
    "- Select dropdown: selects the matching option\n"
    "- Autocomplete field: types and waits 400ms for suggestion list to appear\n\n"
    "After input, verifies the actual value matches what you typed. "
    "If the site reformatted or rejected your input, you'll see a warning."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "target": {
            "type": "integer",
            "description": "Element number [N] from the page snapshot.",
        },
        "value": {
            "type": "string",
            "description": "Text to type or option to select.",
        },
    },
    "required": ["target", "value"],
}


async def handle(ctx: ToolContext, **kwargs: Any) -> str:
    target: int = kwargs.get("target", 0)
    value: str = kwargs.get("value", "")

    locator, error = await locate_element(ctx, target)
    if error:
        return error

    page = ctx.page
    url_before = page.url

    # Detect element type
    elem_info = await locator.evaluate("""el => ({
        tag: el.tagName,
        type: el.type || '',
        role: el.getAttribute('role') || '',
        ariaAutocomplete: el.getAttribute('aria-autocomplete') || '',
        isCombobox: el.getAttribute('role') === 'combobox',
    })""")

    tag = elem_info["tag"]
    is_autocomplete = (
        elem_info["isCombobox"]
        or elem_info["ariaAutocomplete"]
        or elem_info["role"] == "combobox"
    )

    try:
        if tag == "SELECT":
            # Select dropdown — choose by label or value
            try:
                await locator.select_option(label=value, timeout=5000)
            except Exception:
                await locator.select_option(value=value, timeout=5000)
            return f"Selected '{value}' in [{target}]"

        else:
            # Text input — clear and type
            await locator.click(timeout=3000)
            await locator.fill("", timeout=3000)  # clear
            await locator.fill(value, timeout=5000)

            # Autocomplete: wait for suggestion list
            if is_autocomplete:
                await asyncio.sleep(0.4)
                result = f"Typed '{value}' in [{target}] (autocomplete field — suggestions may have appeared, use browse() to see)"
            else:
                result = f"Typed '{value}' in [{target}]"

    except Exception as e:
        return f"Failed to input into [{target}]: {e}"

    # Value verification
    try:
        actual = await locator.evaluate("el => el.value")
        if actual and actual != value:
            result += f"\n⚠ Warning: actual value is '{actual}' (site may have reformatted)"
    except Exception:
        pass

    # Navigation check
    nav_result = await detect_navigation_and_snapshot(ctx, url_before, f"input [{target}]")
    if nav_result:
        result += nav_result

    return result
