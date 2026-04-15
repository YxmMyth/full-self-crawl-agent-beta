"""click — click an element by its index number.

3-level click fallback: normal → force → JS click.
Auto-detects <select> elements → shows options instead of clicking.

See: docs/工具重新设计共识.md §2.2, AgentSession设计.md §7.7
"""

from __future__ import annotations

from typing import Any

from src.agent.tools._interact_helpers import (
    locate_element, check_occlusion, detect_navigation_and_snapshot,
)
from src.browser.context import ToolContext
from src.utils.logging import get_logger

logger = get_logger(__name__)

TOOL_NAME = "click"
TOOL_DESCRIPTION = (
    "Click an interactive element by its [N] number from the page snapshot.\n\n"
    "If the click causes navigation (URL changes), you'll automatically get "
    "the new page snapshot. If not, use browse() to see what changed.\n\n"
    "If the element is a <select> dropdown, this will show the available options "
    "instead — then use input(target, value) to select one."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "target": {
            "type": "integer",
            "description": "Element number [N] from the page snapshot.",
        },
    },
    "required": ["target"],
}


async def handle(ctx: ToolContext, **kwargs: Any) -> str:
    target: int = kwargs.get("target", 0)

    locator, error = await locate_element(ctx, target)
    if error:
        return error

    page = ctx.page
    url_before = page.url

    # Check if it's a <select> element
    tag = await locator.evaluate("el => el.tagName")
    if tag == "SELECT":
        options = await locator.evaluate("""el => {
            return Array.from(el.options).map((o, i) => ({
                index: i,
                value: o.value,
                text: o.text,
                selected: o.selected,
            }));
        }""")
        lines = [f"Element [{target}] is a <select> dropdown. Options:"]
        for opt in options:
            marker = " ← selected" if opt["selected"] else ""
            lines.append(f"  {opt['text']} (value='{opt['value']}'){marker}")
        lines.append(f"\nUse input(target={target}, value='...') to select an option.")
        return "\n".join(lines)

    # Occlusion check
    warning = await check_occlusion(ctx, target)

    # 3-level click fallback
    click_method = ""
    try:
        await locator.click(timeout=5000)
        click_method = "normal"
    except Exception:
        try:
            await locator.click(force=True, timeout=5000)
            click_method = "force"
        except Exception:
            try:
                await locator.evaluate("el => el.click()")
                click_method = "js"
            except Exception as e:
                return f"Failed to click [{target}] (tried normal, force, JS click): {e}"

    # Check for navigation
    nav_result = await detect_navigation_and_snapshot(ctx, url_before, f"click [{target}]")

    result = f"Clicked [{target}]"
    if click_method != "normal":
        result += f" (via {click_method} click)"
    if warning:
        result = f"{warning}\n{result}"
    if nav_result:
        result += nav_result
    else:
        result += "\nUse browse() to see the updated page."

    return result
