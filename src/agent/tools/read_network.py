"""read_network — network layer information.

Shows captured API requests with response body previews, cookies, and curl replay tips.
Provides data that JS cannot access: HttpOnly cookies, response bodies, full headers.

See: docs/工具重新设计共识.md §2.2, docs/browse工具深度设计报告.md §三
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from src.browser.context import ToolContext
from src.utils.logging import get_logger

logger = get_logger(__name__)

TOOL_NAME = "read_network"
TOOL_DESCRIPTION = (
    "View captured network requests with detailed response data.\n\n"
    "Shows what browse's Network summary hinted at — full details:\n"
    "- Response body preview (first 1000 chars per request)\n"
    "- POST request bodies (for API replay)\n"
    "- Cookies (including HttpOnly ones invisible to JS)\n\n"
    "Use filter to narrow results (e.g. filter='/api/' or filter='graphql').\n"
    "Use clear=true to reset the buffer so next read only shows new requests.\n\n"
    "Tip: After finding an API endpoint here, use bash with curl/curl_cffi to replay it "
    "with modified parameters (pagination, different queries, etc.)."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "filter": {
            "type": "string",
            "description": "Filter requests by URL substring (e.g. '/api/', 'graphql').",
        },
        "clear": {
            "type": "boolean",
            "description": "Clear the capture buffer after reading (default false).",
        },
    },
    "required": [],
}


async def handle(ctx: ToolContext, **kwargs: Any) -> str:
    filter_str: str | None = kwargs.get("filter")
    clear: bool = kwargs.get("clear", False)

    captures = ctx.network_captures
    if filter_str:
        captures = [c for c in captures if filter_str.lower() in c.url.lower()]

    lines: list[str] = []

    if not captures:
        lines.append("No API requests captured" + (f" matching '{filter_str}'" if filter_str else "") + ".")
        lines.append("")
        lines.append("Requests are captured passively as you browse. Try:")
        lines.append("- browse() a page that loads data via API")
        lines.append("- Click/scroll to trigger API calls, then read_network() again")
    else:
        lines.append(f"## Captured API Requests ({len(captures)})")
        lines.append("")

        for i, cap in enumerate(captures, 1):
            lines.append(f"### [{i}] {cap.method} {cap.path}")
            lines.append(f"Status: {cap.status} | Size: {_human_size(cap.response_size)}"
                        + (f" | Items: {cap.item_count}" if cap.item_count else ""))

            # POST request body
            if cap.request_body:
                lines.append(f"Request body: {cap.request_body[:500]}")

            # Response preview
            if cap.response_preview:
                preview = cap.response_preview[:1000]
                # Try to pretty-print JSON
                try:
                    parsed = json.loads(preview)
                    preview = json.dumps(parsed, indent=2, ensure_ascii=False)[:1000]
                except (json.JSONDecodeError, TypeError):
                    pass
                lines.append(f"Response preview:\n```\n{preview}\n```")

            lines.append("")

    # Cookies section
    try:
        cookies = await ctx.pw_context.cookies()
        if cookies:
            lines.append("## Cookies")
            for c in cookies[:20]:  # limit display
                flags = []
                if c.get("httpOnly"):
                    flags.append("HttpOnly")
                if c.get("secure"):
                    flags.append("Secure")
                flag_str = f" [{', '.join(flags)}]" if flags else ""
                domain = c.get("domain", "")
                lines.append(f"- {c['name']}{flag_str} (domain: {domain})")
            if len(cookies) > 20:
                lines.append(f"... ({len(cookies) - 20} more)")
            lines.append("")
    except Exception:
        pass

    # Curl replay tip
    if captures:
        cap = captures[0]
        domain = urlparse(cap.url).netloc
        lines.append("## Replay Tip")
        if cap.method == "GET":
            lines.append(f'`bash: python -c "from curl_cffi import requests; r = requests.get(\'{cap.url}\', impersonate=\'chrome\'); print(r.text[:500])"`')
        elif cap.method == "POST" and cap.request_body:
            lines.append(f"Use bash with curl_cffi to POST to {cap.path} with the request body above.")
        lines.append("")

    # Filtered counts
    tracking = ctx.network_filtered_count.get("tracking", 0)
    static = ctx.network_filtered_count.get("static", 0)
    if tracking or static:
        lines.append(f"(Filtered out: {tracking} tracking, {static} static asset requests)")

    if clear:
        ctx.clear_network()
        lines.append("\n(Buffer cleared — next read_network will only show new requests)")

    return "\n".join(lines)


def _human_size(nbytes: int) -> str:
    if nbytes < 1024:
        return f"{nbytes}B"
    elif nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.0f}KB"
    else:
        return f"{nbytes / (1024 * 1024):.1f}MB"
