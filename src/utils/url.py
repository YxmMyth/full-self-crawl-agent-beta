"""URL normalization and domain utilities."""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse, urljoin


def normalize(url: str) -> str:
    """Normalize a URL for consistent comparison.

    - Lowercase scheme and host
    - Strip trailing slash (unless path is just '/')
    - Remove default ports (80 for http, 443 for https)
    - Remove fragment
    - Preserve query string as-is (order matters for caching)
    """
    parsed = urlparse(url)

    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()

    # Strip default ports
    port = parsed.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    netloc = f"{host}:{port}" if port else host

    # Normalize path: strip trailing slash unless root
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))


def extract_domain(url: str) -> str:
    """Extract the domain (hostname) from a URL.

    >>> extract_domain("https://example.com/category/foo")
    'example.com'
    """
    parsed = urlparse(url)
    return (parsed.hostname or "").lower()


def same_origin(url_a: str, url_b: str) -> bool:
    """Check if two URLs share the same origin (scheme + host + port)."""
    a = urlparse(url_a)
    b = urlparse(url_b)
    return (
        (a.scheme or "https").lower() == (b.scheme or "https").lower()
        and (a.hostname or "").lower() == (b.hostname or "").lower()
        and a.port == b.port
    )


def resolve(base: str, relative: str) -> str:
    """Resolve a relative URL against a base URL.

    >>> resolve("https://example.com/category/foo", "/detail/abc123")
    'https://example.com/detail/abc123'
    """
    return urljoin(base, relative)


def path_only(url: str) -> str:
    """Extract path + query from a URL (no scheme/host).

    Useful for display and location patterns.

    >>> path_only("https://example.com/category/foo?page=2")
    '/category/foo?page=2'
    """
    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        return f"{path}?{parsed.query}"
    return path
