"""fetch — HTTP request using the browser's session.

Goes through Playwright's APIRequestContext (`pw_context.request`), which
shares the BrowserContext's cookie jar (HttpOnly cookies included), follows
redirects automatically, and uses the browser's TLS stack.

Agent never needs to think about cookies, auth headers, redirects, or TLS —
those are infrastructure. The agent only sees:

  - the response body (small inline, large written to disk)
  - HTTP status, content type, final URL
  - error reason + actionable hint when something goes wrong

Use for:
  - JSON API endpoints (replays the kind agent used to do via bash + curl_cffi)
  - Binary file downloads (.zip/.pdf/.figma/.png/.mp4/...)
  - Any HTTP GET that requires the current login session

Don't use for:
  - HTML page rendering → use browse() (it executes JS and returns structured page)
  - Shell commands / scripts / file ops → use bash

See: docs/抽象边界原则.md (the "Agent vs Infrastructure" principle this tool
embodies — cookies/redirects/TLS are infra, agent only sees semantic signals)
"""

from __future__ import annotations

import json as _json
import time
from typing import Any

from src.browser.context import ToolContext
from src.config import Config
from src.utils.logging import get_logger

logger = get_logger(__name__)

TOOL_NAME = "fetch"
TOOL_DESCRIPTION = (
    "Fetch a URL using the current browser session. Cookies (including HttpOnly "
    "auth cookies) are sent automatically. Redirects are followed. The browser's "
    "TLS stack is used. Use for JSON APIs, binary file downloads, and anything "
    "requiring the current login session.\n\n"
    "Behavior:\n"
    "- Without save_as: small responses (<200KB) returned inline; larger ones "
    "auto-saved to workspace/ (treated as kind='workspace').\n"
    "- With save_as: kind is REQUIRED. The kind picks the directory:\n"
    "    kind='sample'    → samples/{save_as}    primary data (the deliverable)\n"
    "    kind='catalog'   → catalog/{save_as}    indexes / listings / API metadata\n"
    "    kind='workspace' → workspace/{save_as}  exploration / debug / scratch\n"
    "  save_as must be a FILENAME only (no directory prefix, no '..'). The kind "
    "determines where it lands.\n\n"
    "What counts as each kind (BE STRICT — verification will check):\n"
    "  sample = ONE concrete instance of the primary data, in its native form. "
    "A pen's full source HTML+CSS+JS, a Figma .fig file, an article body. "
    "A LIST of 100 pen IDs is NOT a sample.\n"
    "  catalog = metadata about samples: API responses, listings, URL maps, "
    "ID/title/owner pages. Useful as recon notes but NOT the deliverable.\n"
    "  workspace = exploration that may or may not turn into something. "
    "Failed extractions, debug dumps, intermediate parses.\n\n"
    "Returns on success: {status:'ok', body|path, size_bytes, content_type, "
    "http_status, final_url}.\n"
    "Returns on error: {status:'error', reason, hint, ...}. Reasons: auth_required, "
    "forbidden, not_found, got_html_likely_login_redirect, empty_body, "
    "exceeds_max_size, server_error, network_error, invalid_input.\n\n"
    "Don't use for: rendering HTML pages (use browse), shell commands (use bash)."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "Absolute URL to fetch."},
        "save_as": {
            "type": "string",
            "description": (
                "Optional. FILENAME ONLY (no directory, no '..'). Required-with kind. "
                "Examples: 'lion_pen.html', 'pens_page1.json', 'design_42.fig.zip'."
            ),
        },
        "kind": {
            "type": "string",
            "enum": ["sample", "catalog", "workspace"],
            "description": (
                "REQUIRED when save_as is given. Classifies the saved content: "
                "'sample' (primary data, the deliverable), "
                "'catalog' (metadata about samples — listings, APIs, IDs), "
                "'workspace' (exploration / debug / scratch)."
            ),
        },
        "max_size_mb": {
            "type": "integer",
            "description": "Abort if response larger than this (default 100).",
        },
    },
    "required": ["url"],
}

_KIND_DIRS = {"sample": "samples", "catalog": "catalog", "workspace": "workspace"}


_DEFAULT_MAX_MB = 100
_INLINE_THRESHOLD = 200 * 1024  # 200KB — above this, auto-save instead of inline
_TIMEOUT_MS = 120_000

_FAILURE_HINTS = {
    "auth_required":
        "Cookies expired or insufficient. Re-check login via browse(), and call "
        "request_human_assist if a human needs to log in.",
    "forbidden":
        "Access denied — your account may not have permission. Verify via "
        "browse() or request_human_assist.",
    "not_found":
        "URL is wrong or the resource was deleted. Re-derive the URL via "
        "browse() on the relevant entity page.",
    "got_html_likely_login_redirect":
        "Server returned HTML when binary/data was expected — likely an "
        "unauthenticated redirect to login. Re-check login state with browse(); "
        "if not logged in, request_human_assist.",
    "empty_body":
        "Server returned 0 bytes. The endpoint may be broken or rate-limited; "
        "try a different URL or wait.",
    "exceeds_max_size":
        "Response larger than max_size_mb. Increase max_size_mb if intentional.",
    "server_error":
        "Server returned 5xx. May be temporary; retry once or report.",
    "network_error":
        "Network failure during fetch. Check the URL or retry.",
    "invalid_input":
        "Check the url and save_as parameters.",
    "http_error":
        "Unexpected HTTP status. See http_status field.",
}


async def handle(ctx: ToolContext, **kwargs: Any) -> dict:
    url = (kwargs.get("url") or "").strip()
    save_as: str | None = kwargs.get("save_as")
    if save_as is not None:
        save_as = save_as.strip()
    kind: str | None = kwargs.get("kind")
    if kind is not None:
        kind = kind.strip().lower() or None
    max_mb = int(kwargs.get("max_size_mb") or _DEFAULT_MAX_MB)

    # Validate input
    if not url:
        return _err("invalid_input", hint="url is required")
    if save_as is not None:
        if "/" in save_as or "\\" in save_as or ".." in save_as:
            return _err(
                "invalid_input",
                hint="save_as must be a FILENAME only (no directory, no '..'). "
                     "The kind parameter picks the directory.",
            )
        if not kind:
            return _err(
                "invalid_input",
                hint="kind is REQUIRED when save_as is given. "
                     "Pick one: 'sample' (primary data deliverable), "
                     "'catalog' (metadata/listings/IDs), or 'workspace' (debug/scratch). "
                     "If unsure, this content is probably catalog or workspace, not sample.",
            )
        if kind not in _KIND_DIRS:
            return _err(
                "invalid_input",
                hint=f"kind must be one of {list(_KIND_DIRS)}, got '{kind}'.",
            )

    # Resolve domain (for run_dir destination)
    domain = getattr(ctx, "_domain", None)
    if not domain:
        try:
            from urllib.parse import urlparse
            domain = urlparse(ctx.page.url).hostname or "unknown"
        except Exception:
            domain = "unknown"
    run_dir = Config.run_dir(domain)
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # Issue the request through the browser's APIRequestContext.
    # Cookies (incl. HttpOnly), redirects, TLS — all handled by Playwright.
    try:
        api = ctx.pw_context.request
        resp = await api.get(url, timeout=_TIMEOUT_MS, max_redirects=20)
    except Exception as e:
        logger.warning(f"fetch network error: {e}", extra={"url": url})
        return _err("network_error", hint=str(e))

    status = resp.status
    headers = resp.headers or {}
    content_type = (headers.get("content-type") or headers.get("Content-Type") or "").lower()
    final_url = resp.url

    # Status-based error classification
    if status == 401:
        return _err("auth_required", http_status=status, content_type=content_type)
    if status == 403:
        return _err("forbidden", http_status=status, content_type=content_type)
    if status == 404:
        return _err("not_found", http_status=status, content_type=content_type)
    if status >= 500:
        return _err("server_error", http_status=status, content_type=content_type)
    if not (200 <= status < 300):
        return _err(
            "http_error",
            http_status=status,
            content_type=content_type,
            hint=f"Unexpected HTTP status {status}",
        )

    # Read body
    try:
        body_bytes = await resp.body()
    except Exception as e:
        return _err("network_error", hint=f"body read failed: {e}")

    size = len(body_bytes)

    # Size cap check
    if size > max_mb * 1024 * 1024:
        return _err(
            "exceeds_max_size",
            http_status=status,
            content_type=content_type,
            hint=f"got {_human(size)}, limit {max_mb}MB",
        )
    if size == 0:
        return _err("empty_body", http_status=status, content_type=content_type)

    # HTML when binary expected → likely login redirect
    # Heuristic: if save_as suggests a non-HTML extension, or no save_as but
    # content is HTML, that's the smell. Skip if save_as explicitly is .html/.htm.
    if "text/html" in content_type:
        suggests_html = save_as and save_as.lower().endswith((".html", ".htm"))
        if not suggests_html and (save_as or _looks_like_login_html(body_bytes)):
            return _err(
                "got_html_likely_login_redirect",
                http_status=status,
                content_type=content_type,
                final_url=final_url,
            )

    # Decide save destination: explicit save_as wins; else auto-save large; else inline
    if save_as:
        # kind is validated above — route by kind
        rel_path = f"{_KIND_DIRS[kind]}/{save_as}"
        dest = run_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body_bytes)
        logger.info(
            f"fetch saved {url} → {rel_path} (kind={kind}, {_human(size)})",
            extra={"tool": "fetch", "size": size, "domain": domain, "kind": kind},
        )
        return {
            "status": "ok",
            "path": rel_path,
            "kind": kind,
            "size_bytes": size,
            "size_human": _human(size),
            "content_type": content_type,
            "http_status": status,
            "final_url": final_url,
        }

    if size > _INLINE_THRESHOLD:
        # Auto-save large response to workspace
        ext = _guess_ext(content_type)
        filename = f"fetch_{int(time.time() * 1000)}{ext}"
        dest = workspace / filename
        dest.write_bytes(body_bytes)
        rel_path = f"workspace/{filename}"
        logger.info(
            f"fetch auto-saved (>200KB) {url} → {rel_path} ({_human(size)})",
            extra={"tool": "fetch", "size": size, "domain": domain},
        )
        return {
            "status": "ok",
            "path": rel_path,
            "size_bytes": size,
            "size_human": _human(size),
            "content_type": content_type,
            "http_status": status,
            "final_url": final_url,
            "note": "Response >200KB auto-saved to workspace/. Use bash to inspect.",
        }

    # Inline body
    body_text = body_bytes.decode("utf-8", errors="replace")
    inline: Any = body_text
    if "json" in content_type:
        try:
            inline = _json.loads(body_text)
        except Exception:
            pass  # fall back to text
    return {
        "status": "ok",
        "body": inline,
        "size_bytes": size,
        "size_human": _human(size),
        "content_type": content_type,
        "http_status": status,
        "final_url": final_url,
    }


def _err(reason: str, *, http_status: int | None = None,
         content_type: str = "", final_url: str = "",
         hint: str | None = None) -> dict:
    out: dict[str, Any] = {
        "status": "error",
        "reason": reason,
        "hint": hint or _FAILURE_HINTS.get(reason, "Check inputs and retry."),
    }
    if http_status is not None:
        out["http_status"] = http_status
    if content_type:
        out["content_type"] = content_type
    if final_url:
        out["final_url"] = final_url
    return out


def _human(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f}KB"
    if n < 1024**3:
        return f"{n / (1024**2):.1f}MB"
    return f"{n / (1024**3):.2f}GB"


def _guess_ext(content_type: str) -> str:
    ct = content_type.lower()
    if "json" in ct:
        return ".json"
    if "html" in ct:
        return ".html"
    if "zip" in ct:
        return ".zip"
    if "pdf" in ct:
        return ".pdf"
    if "image/png" in ct:
        return ".png"
    if "image/jpeg" in ct or "image/jpg" in ct:
        return ".jpg"
    if "image/" in ct:
        return ".img"
    return ".bin"


def _looks_like_login_html(body: bytes) -> bool:
    """Sniff for login redirect HTML when content-type is text/html and no save_as."""
    head = body[:4096].decode("utf-8", errors="replace").lower()
    markers = ("sign in", "log in", "login", "<form", 'name="password"', 'type="password"')
    return any(m in head for m in markers)
