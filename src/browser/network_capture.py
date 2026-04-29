"""Passive network request capture via page.on('response').

Captures JSON/GraphQL API responses, filters tracking and static assets.
Must use page.on('response'), NOT page.route() — the latter triggers anti-bot detection.

See: docs/browse工具深度设计报告.md §三
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from src.utils.logging import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Page, Response
    from src.browser.context import ToolContext

logger = get_logger(__name__)


@dataclass
class CapturedRequest:
    """A single captured network request/response pair."""
    method: str                     # GET / POST
    url: str                        # full URL
    path: str                       # path only (for display)
    request_body: str | None        # POST body (JSON string)
    status: int                     # HTTP status code
    content_type: str               # response Content-Type
    response_size: int              # response body size in bytes
    item_count: int | None          # if JSON array, element count
    response_preview: str           # first 1000 chars preview


# ── Tracking / analytics domains to filter ───────────────

_TRACKING_DOMAINS = {
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "facebook.com", "facebook.net", "fbcdn.net",
    "segment.io", "segment.com", "cdn.segment.com",
    "mixpanel.com", "amplitude.com", "hotjar.com",
    "sentry.io", "bugsnag.com", "datadoghq.com",
    "newrelic.com", "nr-data.net",
    "cloudflareinsights.com", "plausible.io",
    "twitter.com", "t.co", "ads-twitter.com",
    "linkedin.com", "snap.licdn.com",
    "tiktok.com", "analytics.tiktok.com",
}

# Static asset content types
_STATIC_TYPES = {
    "image/", "font/", "text/css", "application/javascript",
    "text/javascript", "application/x-javascript",
    "application/wasm", "video/", "audio/",
}

# API path patterns (positive signal)
_API_PATH_RE = re.compile(
    r"/(api|graphql|v[0-9]+|rest|_next/data|_api|ajax|rpc)/",
    re.IGNORECASE,
)


# ── Core logic ───────────────────────────────────────────


def _is_tracking(url: str) -> bool:
    """Check if URL is from a known tracking/analytics domain."""
    hostname = urlparse(url).hostname or ""
    for domain in _TRACKING_DOMAINS:
        if hostname == domain or hostname.endswith("." + domain):
            return True
    return False


def _is_static(content_type: str) -> bool:
    """Check if content type is a static asset."""
    ct = content_type.lower()
    return any(ct.startswith(prefix) for prefix in _STATIC_TYPES)


def _is_data_api(url: str, content_type: str, method: str) -> bool:
    """Determine if a response is a data API worth capturing."""
    ct = content_type.lower()

    # JSON or GraphQL responses
    if "application/json" in ct or "application/graphql" in ct:
        return True

    # URL matches API path pattern
    if _API_PATH_RE.search(urlparse(url).path):
        return True

    # POST requests with JSON body (likely API)
    if method == "POST" and "json" in ct:
        return True

    return False


def _count_items(body_bytes: bytes) -> int | None:
    """If the response is a JSON array, return its length."""
    try:
        data = json.loads(body_bytes)
        if isinstance(data, list):
            return len(data)
        # Common patterns: {"data": [...], "results": [...], "items": [...]}
        if isinstance(data, dict):
            for key in ("data", "results", "items", "entries",
                        "records", "list", "hits", "edges", "nodes"):
                val = data.get(key)
                if isinstance(val, list):
                    return len(val)
        return None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _graphql_operation(body: str | None) -> str | None:
    """Extract GraphQL operation name from request body."""
    if not body:
        return None
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            return data.get("operationName")
    except (json.JSONDecodeError, TypeError):
        pass
    return None


# ── Setup ────────────────────────────────────────────────


def setup_network_capture(page: Page, ctx: ToolContext) -> None:
    """Register passive response listener on a page.

    Must be called BEFORE page.goto() to capture navigation requests.
    """

    async def on_response(response: Response) -> None:
        url = response.url
        content_type = response.headers.get("content-type", "")
        method = response.request.method

        # Filter tracking
        if _is_tracking(url):
            ctx.network_filtered_count["tracking"] += 1
            return

        # Filter static assets
        if _is_static(content_type):
            ctx.network_filtered_count["static"] += 1
            return

        # Only capture data API responses
        if not _is_data_api(url, content_type, method):
            return

        try:
            body = await response.body()
        except Exception:
            # Response body may not be available (e.g., redirects)
            body = b""

        item_count = _count_items(body)
        request_body = response.request.post_data if method == "POST" else None
        path = urlparse(url).path
        query = urlparse(url).query
        if query:
            path = f"{path}?{query}"

        capture = CapturedRequest(
            method=method,
            url=url,
            path=path,
            request_body=request_body,
            status=response.status,
            content_type=content_type,
            response_size=len(body),
            item_count=item_count,
            response_preview=body[:1000].decode("utf-8", errors="replace"),
        )
        ctx.network_captures.append(capture)

    page.on("response", on_response)


# ── Formatting (for browse output) ──────────────────────


def format_network_summary(ctx: ToolContext) -> str:
    """Format captured requests as a summary section for browse output."""
    captures = ctx.network_captures
    if not captures and not any(ctx.network_filtered_count.values()):
        return ""

    lines = ["--- Network Requests ---"]

    if captures:
        lines.append(f"API Requests Captured ({len(captures)}):")
        for cap in captures:
            # One line per request, compact
            size_str = _human_size(cap.response_size)
            parts = [f"  {cap.method} {cap.path} → {cap.status}"]

            type_hint = "JSON" if "json" in cap.content_type.lower() else "data"

            # GraphQL: show operation name
            op = _graphql_operation(cap.request_body)
            if op:
                parts[0] = f"  {cap.method} {cap.path} {{operationName: \"{op}\"}} → {cap.status}"

            items = f", {cap.item_count} items" if cap.item_count else ""
            parts.append(f"({type_hint}{items}, {size_str})")
            lines.append(" ".join(parts))

    # Filtered counts
    tracking = ctx.network_filtered_count.get("tracking", 0)
    static = ctx.network_filtered_count.get("static", 0)
    if tracking or static:
        parts = []
        if tracking:
            parts.append(f"{tracking} tracking/analytics")
        if static:
            parts.append(f"{static} static assets")
        lines.append(f"Filtered: {', '.join(parts)}")

    return "\n".join(lines)


def _human_size(nbytes: int) -> str:
    """Format bytes as human-readable size."""
    if nbytes < 1024:
        return f"{nbytes}B"
    elif nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.0f}KB"
    else:
        return f"{nbytes / (1024 * 1024):.1f}MB"
