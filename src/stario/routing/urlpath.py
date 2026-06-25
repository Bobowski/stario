"""Parsed route templates — one representation for registration and link building.

``UrlPath`` is the public face of this package: parse once at import time, then
pass the same object to ``app.get(...)`` and ``HOME.href(...)``.

Construction flow: normalize path → parse host/path segments → collect
placeholders → cache static hrefs when there is nothing to substitute.
"""

from collections.abc import Mapping
from urllib.parse import quote

from stario.exceptions import StarioError
from stario.routing.locations import append_query_fragment, normalize_path
from stario.routing.segment import (
    Segment,
    SegmentKind,
    parse_host_segments,
    parse_path_segments,
)

# --- href() parameter formatting --------------------------------------------


def _format_path_value(
    pattern: str,
    name: str,
    kind: SegmentKind,
    value: object,
) -> str:
    if value is None:
        raise StarioError(
            "UrlPath parameter must not be None",
            context={"pattern": pattern, "parameter": name},
            help_text="Pass a non-empty value for each path placeholder.",
        )

    text = str(value)
    if not text:
        raise StarioError(
            "UrlPath parameter must not be empty",
            context={"pattern": pattern, "parameter": name},
            help_text="Pass a non-empty value for each path placeholder.",
        )

    if kind == "wildcard":
        # One segment — slashes require a catchall placeholder.
        if "/" in text:
            raise StarioError(
                "UrlPath path parameter contains '/'",
                context={"pattern": pattern, "parameter": name, "value": text},
                help_text="Use a catchall placeholder like '{path...}' for values with slashes.",
            )
        return quote(text, safe="")

    # Catchall: quote each piece, preserve interior slashes.
    parts = text.split("/")
    if "" in parts:
        raise StarioError(
            "UrlPath catchall parameter contains empty path segment",
            context={"pattern": pattern, "parameter": name, "value": text},
            help_text="Pass a relative path without leading, trailing, or repeated slashes.",
        )
    return "/".join(quote(part, safe="") for part in parts)


def _format_host_value(
    pattern: str,
    name: str,
    kind: SegmentKind,
    value: object,
) -> str:
    if value is None:
        raise StarioError(
            "UrlPath host parameter must not be None",
            context={"pattern": pattern, "parameter": name},
            help_text="Pass a non-empty value for each host placeholder.",
        )

    text = str(value).lower()
    if not text:
        raise StarioError(
            "UrlPath host parameter must not be empty",
            context={"pattern": pattern, "parameter": name},
            help_text="Pass a non-empty value for each host placeholder.",
        )
    if any(ch in text for ch in "/:@[]"):
        raise StarioError(
            "UrlPath host parameter contains invalid character",
            context={"pattern": pattern, "parameter": name, "value": text},
            help_text="Host parameters must be host labels, not full URLs.",
        )

    labels = text.split(".")
    if kind == "wildcard" and len(labels) != 1:
        raise StarioError(
            "UrlPath host parameter contains '.'",
            context={"pattern": pattern, "parameter": name, "value": text},
            help_text="Use a catchall host placeholder like '{tenant...}' for dotted values.",
        )
    if "" in labels:
        raise StarioError(
            "UrlPath host parameter contains empty host label",
            context={"pattern": pattern, "parameter": name, "value": text},
            help_text="Pass a host value without leading, trailing, or repeated dots.",
        )
    return text


def _collect_placeholders(
    host: tuple[Segment, ...],
    path: tuple[Segment, ...],
    *,
    pattern: str,
) -> frozenset[str]:
    seen: set[str] = set()
    placeholders: set[str] = set()
    for segment in (*host, *path):
        if segment.kind == "exact":
            continue
        if segment.name in seen:
            raise StarioError(
                "Duplicate route parameter",
                context={"pattern": pattern, "parameter": segment.name},
                help_text="Use each placeholder name only once in a UrlPath.",
            )
        seen.add(segment.name)
        placeholders.add(segment.name)
    return frozenset(placeholders)


def _append_formatted_segments(
    parts: list[str],
    segments: tuple[Segment, ...],
    values: Mapping[str, object],
    *,
    pattern: str,
    host: bool,
) -> None:
    formatter = _format_host_value if host else _format_path_value
    separator = "." if host else "/"
    for index, segment in enumerate(segments):
        if index > 0:
            parts.append(separator)
        if segment.kind == "exact":
            parts.append(segment.name)
        else:
            parts.append(formatter(pattern, segment.name, segment.kind, values[segment.name]))


class UrlPath:
    """Canonical route template for registration and URL building.

    Path-only routes use a leading slash: `UrlPath("/users")`. Host-aware routes
    pass the host separately: `UrlPath("/users", host="api.example.com")` or
    `UrlPath("/users", host="{tenant}.example.com")`.

    Pass the object to route registration (`app.get(HOME, …)`). Call `href()` to build
    a browser URL with path params, query params, or a fragment. Host-aware patterns
    build network-path hrefs (`//host/path`).

    Host labels are stored left-to-right as authored. The trie walks them right-to-left
    (DNS-style); use `host_trie()` for registration and `request_host()` for matching.
    """

    __slots__ = (
        "_path_text",
        "_static_href",
        "host",
        "host_text",
        "path",
        "placeholders",
        "text",
    )

    def __init__(self, path: str, *, host: str | None = None) -> None:
        if path and not path.startswith("/"):
            raise StarioError(
                "UrlPath path must start with '/'",
                context={"path": path},
                help_text="Use UrlPath('/path') for app paths.",
            )
        if host is not None and not host:
            raise StarioError(
                "UrlPath host must not be empty",
                context={"path": path, "host": host},
                help_text="Omit host= for path-only routes.",
            )

        path_text = normalize_path(path) if path else "/"
        self.path = parse_path_segments(path_text)

        if host is not None:
            self.host = parse_host_segments(host)
            self.host_text = ".".join(segment.pattern for segment in self.host)
        else:
            self.host = ()
            self.host_text = None

        self._path_text = path_text
        self.text = (self.host_text + path_text) if self.host_text is not None else path_text
        self.placeholders = _collect_placeholders(
            self.host,
            self.path,
            pattern=self.text,
        )
        self._static_href = self._build_static_href()

    def __repr__(self) -> str:
        if self.host_text is None:
            return f"UrlPath({self._path_text!r})"
        return f"UrlPath({self._path_text!r}, host={self.host_text!r})"

    @property
    def path_text(self) -> str:
        return self._path_text

    # --- trie helpers (used by stario.http.dispatch) ------------------------

    def host_trie(self) -> tuple[Segment, ...]:
        """Host segments in trie registration / match order (right-to-left)."""
        return self.host[::-1]

    @staticmethod
    def request_host(host: str) -> tuple[str, ...]:
        """Split a request host into trie match order (right-to-left labels)."""
        return tuple(reversed(host.lower().split("."))) if host else ()

    @staticmethod
    def request_path(path: str) -> tuple[str, ...]:
        """Split a canonical request path into trie match segments."""
        return () if path == "/" else tuple(path[1:].split("/"))

    def _build_static_href(self) -> str | None:
        if self.placeholders:
            return None
        if self.host_text is not None:
            return f"//{self.host_text}{self._path_text}"
        return self._path_text

    def _assert_params(self, values: Mapping[str, object]) -> None:
        for name in self.placeholders:
            if name not in values:
                raise StarioError(
                    "UrlPath parameter missing",
                    context={"pattern": self.text, "parameter": name},
                    help_text=f"Pass {name!r} as a keyword argument.",
                )
        for key in values:
            if key not in self.placeholders:
                raise StarioError(
                    "UrlPath unknown parameter",
                    context={"pattern": self.text, "parameter": key},
                    help_text="Pass each placeholder name exactly once.",
                )

    def format(self, values: Mapping[str, object]) -> str:
        """Build the href body from parameter values (no query or fragment)."""
        self._assert_params(values)
        if self._static_href is not None:
            return self._static_href

        parts: list[str] = []

        if self.host:
            if any(segment.kind != "exact" for segment in self.host):
                parts.append("//")
                _append_formatted_segments(
                    parts,
                    self.host,
                    values,
                    pattern=self.text,
                    host=True,
                )
            else:
                # Static host — no substitution needed.
                parts.append("//" + ".".join(segment.name for segment in self.host))

        if self.path:
            parts.append("/")
            _append_formatted_segments(
                parts,
                self.path,
                values,
                pattern=self.text,
                host=False,
            )
        elif self.host or not parts:
            parts.append("/")

        return "".join(parts)

    def __truediv__(self, suffix: str) -> UrlPath:
        child = suffix.strip("/")
        base_path = self._path_text.rstrip("/")
        new_path = f"{base_path}/{child}" if child else base_path or "/"
        return UrlPath(new_path, host=self.host_text)

    def href(
        self,
        params: Mapping[str, object] | None = None,
        /,
        *,
        query: Mapping[str, object] | None = None,
        fragment: str | None = None,
        **params_kwargs: object,
    ) -> str:
        """Build a browser URL from this route pattern.

        Path and host placeholders are passed as keyword arguments (or a positional
        mapping). URL query parameters use the ``query=`` keyword only — not a path
        segment named ``query``.
        """
        if params is None:
            body = self.format(params_kwargs)
        else:
            values = {**params, **params_kwargs} if params_kwargs else dict(params)
            body = self.format(values)
        return append_query_fragment(body, query=query, fragment=fragment)
