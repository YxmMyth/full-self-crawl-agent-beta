"""browser_reset — restart browser with new configuration.

All parameters optional. Bare call = clean restart same config.
Use for: switching proxy, switching browser type, clearing cookies/cache,
recovering from crashes, clearing memory.

See: docs/工具重新设计共识.md §2.2
"""

from __future__ import annotations

from typing import Any

from src.utils.logging import get_logger

logger = get_logger(__name__)

TOOL_NAME = "browser_reset"
TOOL_DESCRIPTION = (
    "Restart the browser with a new configuration. All parameters optional.\n\n"
    "Bare call (no params): clean restart — clears cookies, cache, and memory "
    "while keeping the same browser type and settings.\n\n"
    "Use when:\n"
    "- Site detects/blocks you → try proxy or switch to chromium\n"
    "- Browser becomes slow or unresponsive → clean restart\n"
    "- Need to clear cookies/session state\n"
    "- Firefox doesn't work with a site → browser_type='chromium'\n\n"
    "After reset, you start fresh — no open tabs, no history."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "proxy": {
            "type": "string",
            "description": "Proxy server URL (e.g. 'socks5://user:pass@host:port').",
        },
        "browser_type": {
            "type": "string",
            "enum": ["camoufox", "chromium"],
            "description": "Browser to use. camoufox (default, anti-detection Firefox) or chromium (compatibility fallback).",
        },
        "headed": {
            "type": "boolean",
            "description": "Run with visible browser window (for debugging). Default false.",
        },
    },
    "required": [],
}


async def handle(ctx: Any, **kwargs: Any) -> str:
    """Handler receives the BrowserManager, not ToolContext."""
    # Note: this tool needs special handling in the session loop
    # because it needs access to BrowserManager, not just ToolContext.
    # The session should pass browser_manager via ctx._browser_manager
    browser_manager = getattr(ctx, "_browser_manager", None)
    if browser_manager is None:
        return "Error: browser_reset requires browser manager access (internal error)"

    proxy = kwargs.get("proxy")
    browser_type = kwargs.get("browser_type")
    headed = kwargs.get("headed")

    try:
        new_ctx = await browser_manager.reset(
            browser_type=browser_type,
            headed=headed,
            proxy=proxy,
        )

        parts = ["Browser restarted."]
        if browser_type:
            parts.append(f"Type: {browser_type}")
        if proxy:
            parts.append(f"Proxy: {proxy}")
        if headed is not None:
            parts.append(f"Headed: {headed}")
        if not any([browser_type, proxy, headed is not None]):
            parts.append("Clean restart with same configuration.")

        parts.append("All tabs, cookies, and cache cleared. Use browse() to navigate.")
        return " ".join(parts)

    except Exception as e:
        logger.error(f"browser_reset failed: {e}", extra={"error": str(e)})
        return f"Browser reset failed: {e}"
