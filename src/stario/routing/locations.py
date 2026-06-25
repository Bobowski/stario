"""URL path normalization and query/fragment helpers for route patterns.

Bottom of the routing stack: no segment or UrlPath imports. Everything above
builds on these string utilities.
"""

from collections.abc import Mapping
from urllib.parse import quote, urlencode


def normalize_path(path: str) -> str:
    """Canonical URL path: leading `/`, no trailing slash (`/` alone for root).

    Leading and trailing slashes are stripped before re-prefixing, so runs of
    leading slashes collapse (``//host/`` → ``/host``). Internal ``//`` segments
    are preserved.
    """
    return "/" + path.strip("/")


def append_query_fragment(
    href: str,
    *,
    query: Mapping[str, object] | None = None,
    fragment: str | None = None,
) -> str:
    """Append optional query string and fragment to `href`."""
    if query is not None:
        # Skip None values; lists become repeated keys (doseq=True).
        pairs = [(key, value) for key, value in query.items() if value is not None]
        if pairs:
            href = f"{href}?{urlencode(pairs, doseq=True)}"
    if fragment is not None:
        href = f"{href}#{quote(fragment, safe="/?:@!$&'()*+,;=")}"
    return href
