"""
HTTP Headers - Fast, cached header handling.

Headers are stored as lowercased bytes internally for O(1) comparison.
Common headers and values are pre-encoded at import time for zero-alloc
fast paths.

Design decisions:
- Bytes internally: Avoids repeated encoding in hot paths
- Lowercase keys: HTTP headers are case-insensitive, normalize once
- Separate header/value lookups: "Accept" is both a header and a Vary value
- LRU cache for uncommon headers: Bounded memory for dynamic headers

Memory: ~30 KB at import time for pre-encoded lookup tables.
"""

import re
from functools import lru_cache
from typing import Self

# =============================================================================
# COMMON HEADERS - Defined as canonical strings
# =============================================================================

REQUEST_HEADERS: tuple[str, ...] = (
    "Accept",
    "Accept-Charset",
    "Accept-Encoding",
    "Accept-Language",
    "Authorization",
    "Cache-Control",
    "Connection",
    "Content-Length",
    "Content-Type",
    "Cookie",
    "DNT",
    "Expect",
    "Forwarded",
    "From",
    "Host",
    "If-Match",
    "If-Modified-Since",
    "If-None-Match",
    "If-Range",
    "If-Unmodified-Since",
    "Max-Forwards",
    "Origin",
    "Pragma",
    "Proxy-Authorization",
    "Range",
    "Referer",
    "Sec-CH-UA",
    "Sec-CH-UA-Mobile",
    "Sec-CH-UA-Platform",
    "Sec-Fetch-Dest",
    "Sec-Fetch-Mode",
    "Sec-Fetch-Site",
    "Sec-Fetch-User",
    "TE",
    "Upgrade",
    "Upgrade-Insecure-Requests",
    "User-Agent",
    "Via",
    "X-Correlation-ID",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Forwarded-Proto",
    "X-Real-IP",
    "X-Request-ID",
    "X-Requested-With",
)

RESPONSE_HEADERS: tuple[str, ...] = (
    "Accept-Ranges",
    "Access-Control-Allow-Credentials",
    "Access-Control-Allow-Headers",
    "Access-Control-Allow-Methods",
    "Access-Control-Allow-Origin",
    "Access-Control-Expose-Headers",
    "Access-Control-Max-Age",
    "Age",
    "Allow",
    "Alt-Svc",
    "Cache-Control",
    "Clear-Site-Data",
    "Connection",
    "Content-Disposition",
    "Content-Encoding",
    "Content-Language",
    "Content-Length",
    "Content-Location",
    "Content-Range",
    "Content-Security-Policy",
    "Content-Security-Policy-Report-Only",
    "Content-Type",
    "Cross-Origin-Embedder-Policy",
    "Cross-Origin-Opener-Policy",
    "Cross-Origin-Resource-Policy",
    "Date",
    "ETag",
    "Expires",
    "Last-Modified",
    "Link",
    "Location",
    "NEL",
    "Permissions-Policy",
    "Pragma",
    "Proxy-Authenticate",
    "Referrer-Policy",
    "Retry-After",
    "Server",
    "Server-Timing",
    "Set-Cookie",
    "Strict-Transport-Security",
    "Timing-Allow-Origin",
    "Trailer",
    "Transfer-Encoding",
    "Upgrade",
    "Vary",
    "Via",
    "WWW-Authenticate",
    "X-Content-Type-Options",
    "X-DNS-Prefetch-Control",
    "X-Frame-Options",
    "X-Powered-By",
    "X-XSS-Protection",
)

ALL_HEADERS: tuple[str, ...] = tuple(set(REQUEST_HEADERS + RESPONSE_HEADERS))

# =============================================================================
# COMMON VALUES - Defined as strings
# =============================================================================

CONTENT_TYPES: tuple[str, ...] = (
    # Application
    "application/gzip",
    "application/javascript",
    "application/javascript; charset=utf-8",
    "application/json",
    "application/json; charset=utf-8",
    "application/ld+json",
    "application/manifest+json",
    "application/octet-stream",
    "application/pdf",
    "application/vnd.api+json",
    "application/x-www-form-urlencoded",
    "application/xml",
    "application/xml; charset=utf-8",
    "application/zip",
    # Audio
    "audio/mpeg",
    "audio/ogg",
    "audio/wav",
    "audio/webm",
    # Font
    "font/otf",
    "font/ttf",
    "font/woff",
    "font/woff2",
    # Image
    "image/avif",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/svg+xml",
    "image/webp",
    "image/x-icon",
    # Multipart
    "multipart/form-data",
    # Text
    "text/css",
    "text/css; charset=utf-8",
    "text/csv",
    "text/event-stream",
    "text/html",
    "text/html; charset=utf-8",
    "text/javascript",
    "text/javascript; charset=utf-8",
    "text/markdown",
    "text/plain",
    "text/plain; charset=utf-8",
    "text/xml",
    # Video
    "video/mp4",
    "video/ogg",
    "video/webm",
)

ENCODINGS: tuple[str, ...] = (
    "br",
    "deflate",
    "gzip",
    "identity",
    "zstd",
    "gzip, br",
    "gzip, deflate",
    "gzip, deflate, br",
    "gzip, deflate, br, zstd",
    "zstd, br, gzip, deflate",
)

CACHE_CONTROL_VALUES: tuple[str, ...] = (
    "max-age=0",
    "max-age=3600",
    "max-age=31536000",
    "max-age=31536000, immutable",
    "no-cache",
    "no-cache, no-store",
    "no-cache, no-store, must-revalidate",
    "no-store",
    "private",
    "private, max-age=0",
    "private, no-cache",
    "public",
    "public, max-age=31536000",
    "public, max-age=31536000, immutable",
)

CONNECTION_VALUES: tuple[str, ...] = (
    "close",
    "keep-alive",
    "upgrade",
)

TRANSFER_ENCODING_VALUES: tuple[str, ...] = (
    "chunked",
    "compress",
    "deflate",
    "gzip",
    "identity",
)

VARY_VALUES: tuple[str, ...] = (
    "*",
    "Accept",
    "Accept-Encoding",
    "Accept-Encoding, Accept-Language",
    "Accept-Language",
    "Cookie",
    "Origin",
    "User-Agent",
)

ACCESS_CONTROL_VALUES: tuple[str, ...] = (
    "*",
    "false",
    "true",
)

X_CONTENT_TYPE_OPTIONS_VALUES: tuple[str, ...] = ("nosniff",)

X_FRAME_OPTIONS_VALUES: tuple[str, ...] = (
    "DENY",
    "SAMEORIGIN",
)

# Referrer-Policy values
REFERRER_POLICY_VALUES: tuple[str, ...] = (
    "no-referrer",
    "no-referrer-when-downgrade",
    "origin",
    "origin-when-cross-origin",
    "same-origin",
    "strict-origin",
    "strict-origin-when-cross-origin",
    "unsafe-url",
)

# Cross-Origin policy values
CROSS_ORIGIN_VALUES: tuple[str, ...] = (
    "anonymous",
    "use-credentials",
    "same-origin",
    "same-site",
    "cross-origin",
    "require-corp",
    "credentialless",
    "unsafe-none",
)

# Accept-Ranges values
ACCEPT_RANGES_VALUES: tuple[str, ...] = (
    "bytes",
    "none",
)

# Common HTTP methods (for Access-Control-Allow-Methods)
HTTP_METHODS: tuple[str, ...] = (
    "GET",
    "POST",
    "PUT",
    "DELETE",
    "PATCH",
    "HEAD",
    "OPTIONS",
    "CONNECT",
    "TRACE",
    "GET, POST",
    "GET, POST, PUT, DELETE",
    "GET, POST, PUT, DELETE, PATCH",
    "GET, POST, PUT, DELETE, PATCH, OPTIONS",
)

# Content-Disposition values
CONTENT_DISPOSITION_VALUES: tuple[str, ...] = (
    "inline",
    "attachment",
)

ALL_VALUES: tuple[str, ...] = (
    CONTENT_TYPES
    + ENCODINGS
    + CACHE_CONTROL_VALUES
    + CONNECTION_VALUES
    + TRANSFER_ENCODING_VALUES
    + VARY_VALUES
    + ACCESS_CONTROL_VALUES
    + X_CONTENT_TYPE_OPTIONS_VALUES
    + X_FRAME_OPTIONS_VALUES
    + REFERRER_POLICY_VALUES
    + CROSS_ORIGIN_VALUES
    + ACCEPT_RANGES_VALUES
    + HTTP_METHODS
    + CONTENT_DISPOSITION_VALUES
)


# =============================================================================
# LOOKUP DICTIONARY - Maps str -> bytes (headers lowercased, values as-is)
# =============================================================================


def _build_header_lookup() -> dict[str | bytes, bytes]:
    """
    Build header lookup: str -> lowercased bytes

    Maps both canonical and lowercase forms:
        "Content-Type" -> b"content-type"
        "content-type" -> b"content-type"
    """
    lookup: dict[str | bytes, bytes] = {}
    for header in ALL_HEADERS:
        header_bytes = header.encode("latin-1")
        lowered = header.lower()
        lowered_bytes = lowered.encode("latin-1")
        lookup[header] = lowered_bytes
        lookup[header_bytes] = lowered_bytes
        lookup[lowered] = lowered_bytes
        lookup[lowered_bytes] = lowered_bytes
    return lookup


def _build_value_lookup() -> dict[str | bytes, bytes]:
    """
    Build value lookup: str -> bytes (case-sensitive)

        "text/html" -> b"text/html"
    """
    lookup: dict[str | bytes, bytes] = {}
    for value in ALL_VALUES:
        value_bytes = value.encode("latin-1")
        lookup[value] = value_bytes
        lookup[value_bytes] = value_bytes
    return lookup


# Separate lookups to avoid collisions (e.g. "Accept" is both a header and a Vary value)
HEADER_LOOKUP: dict[str | bytes, bytes] = _build_header_lookup()
VALUE_LOOKUP: dict[str | bytes, bytes] = _build_value_lookup()

# =============================================================================
# ENCODING FUNCTIONS - OPTIMIZED FOR MAX PERFORMANCE
# =============================================================================
#
# Performance strategy:
# 1. HEADER_LOOKUP/VALUE_LOOKUP: O(1) dict lookup for ~95% of real traffic
#    Common headers like "Content-Type" are pre-encoded at import time.
# 2. LRU cache: For custom headers, validate once and cache the result.
#    512 entries handles most apps without memory bloat.
# 3. Regex validation: When we miss both caches, regex is faster than
#    iterating characters in Python.
#
# Security: Header names and values are validated to prevent HTTP response
# splitting attacks. Invalid characters (CTLs, newlines) are rejected.
# =============================================================================

# Compiled regex patterns for validation (faster than table lookup for actual validation)
# Header field-names must be valid "token" characters (RFC 9110).
# This regex matches *invalid* bytes (CTLs + separators), so search() returning a
# match means the header name is invalid.
#
# Note: inside a character class, [ and ] must be escaped to be treated literally.
HEADER_RE = re.compile(rb'[\x00-\x1f\x7f()<>@,;:\\"/\[\]\?={} \t]')
HEADER_VALUE_RE = re.compile(b"[\x00-\x08\x0a-\x1f\x7f]")


@lru_cache(maxsize=512)
def _validate_header(name: str | bytes) -> bytes:
    """
    Validate and normalize header name to lowercased bytes.
    Uses regex for fast validation. Cached for custom headers.
    """
    if isinstance(name, str):
        name = name.encode("latin-1")

    lowered = name.lower()

    # Regex validation (optimized C code, faster than Python loop)
    # Returns None if valid (no invalid chars found)
    if HEADER_RE.search(lowered):
        raise ValueError(f"Invalid header name: {name}")

    return lowered


def _validate_value(value: str | bytes) -> bytes:
    """
    Validate header value bytes.
    Uses regex for fast validation. Cached for custom values.
    """
    if isinstance(value, str):
        value = value.encode("latin-1")

    if HEADER_VALUE_RE.search(value):
        raise ValueError(f"Invalid header value: {value}")

    return value


def encode_header(name: str | bytes) -> bytes:
    """
    Encode header name to lowercased bytes.

    Fast path: O(1) lookup in common headers dict
    Slow path: regex validation + LRU caching
    """
    if encoded := HEADER_LOOKUP.get(name):
        return encoded
    return _validate_header(name)


def encode_value(value: str | bytes) -> bytes:
    """
    Encode header value to bytes.

    Fast path: O(1) lookup in common values dict
    Slow path: regex validation + LRU caching
    """
    if encoded := VALUE_LOOKUP.get(value):
        # print("cache_hit value", value)
        return encoded
    return _validate_value(value)


# =============================================================================
# HEADERS CLASS
# =============================================================================


class Headers:
    """
    HTTP Headers container storing headers as lowercased bytes.

    - str names/values: encoded via lookup cache
    - bytes names/values: passed through as-is
    """

    __slots__ = ("_data",)

    def __init__(
        self, raw_header_data: dict[bytes, bytes | list[bytes]] | None = None
    ) -> None:
        """
        Initialize Headers with an existing mapping of header entries.

        WARNING: If you use this constructor, you must provide the raw header data already
        in correct, finalized (lowercased ASCII bytes, no conversion), and the dictionary will
        be used *as is*, with no copying, normalization, or validation.

        This is intended for advanced use-cases where performance is critical and you can guarantee
        the structure and correctness of the mapping you provide.
        """
        self._data = raw_header_data or {}

    def add(self, name: str | bytes, value: str | bytes) -> Self:
        """Add a header (allows multiple values for same name)."""
        key = encode_header(name)
        val = encode_value(value)
        existing = self._data.get(key)
        if existing is None:
            self._data[key] = val
        elif isinstance(existing, list):
            existing.append(val)
        else:
            self._data[key] = [existing, val]
        return self

    def set(self, name: str | bytes, value: str | bytes) -> Self:
        """Set a header (replaces existing)."""
        self._data[encode_header(name)] = encode_value(value)
        return self

    # -------------------------------------------------------------------------
    # Raw methods - no encoding/validation, use when you know what you're doing
    # -------------------------------------------------------------------------

    def radd(self, name: bytes, value: bytes) -> Self:
        """
        Add a header without encoding/validation (raw).

        WARNING: Caller must ensure:
        - name is lowercased bytes (e.g. b"content-type", not b"Content-Type")
        - value is valid bytes (no control characters except tab)
        """
        existing = self._data.setdefault(name, value)
        if existing is not value:
            if isinstance(existing, list):
                existing.append(value)
            else:
                self._data[name] = [existing, value]
        return self

    def rset(self, name: bytes, value: bytes) -> Self:
        """
        Set a header without encoding/validation (raw).

        WARNING: Caller must ensure:
        - name is lowercased bytes (e.g. b"content-type", not b"Content-Type")
        - value is valid bytes (no control characters except tab)
        """
        self._data[name] = value
        return self

    def update(self, other: dict[str | bytes, str | bytes] | None) -> Self:
        if other is None:
            return self
        # Directly add items to _data (faster than calling set repeatedly)
        for name, value in other.items():
            self._data[encode_header(name)] = encode_value(value)
        return self

    def setdefault(self, name: str | bytes, value: str | bytes) -> bytes:
        """Set header if not present, return value."""
        key = encode_header(name)
        if key not in self._data:
            self._data[key] = encode_value(value)
        existing = self._data[key]
        return existing if isinstance(existing, bytes) else existing[0]

    def get[T: bytes | None = None](
        self, name: str | bytes, default: T = None
    ) -> T | bytes:
        """Get first value for header."""
        value = self._data.get(name if isinstance(name, bytes) else encode_header(name))
        if value is None:
            return default
        return value if isinstance(value, bytes) else value[0]

    def getlist(self, name: str | bytes) -> list[bytes]:
        """Get all values for header."""
        value = self._data.get(name if isinstance(name, bytes) else encode_header(name))
        if value is None:
            return []
        return [value] if isinstance(value, bytes) else list(value)

    def remove(self, name: str | bytes) -> Self:
        """Remove a header."""
        self._data.pop(name if isinstance(name, bytes) else encode_header(name), None)
        return self

    def items(self) -> list[tuple[bytes, bytes]]:
        """Return all header name-value pairs (flattened)."""
        result: list[tuple[bytes, bytes]] = []
        for name, value in self._data.items():
            if isinstance(value, bytes):
                result.append((name, value))
            else:
                for v in value:
                    result.append((name, v))
        return result

    def clear(self) -> None:
        """Remove all headers."""
        self._data.clear()

    def __contains__(self, name: str | bytes) -> bool:
        key = name if isinstance(name, bytes) else encode_header(name)
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"Headers({self._data!r})"
