"""
HTTP Headers - Fast, cached header handling.

Headers are stored as lowercased bytes internally for O(1) comparison.
Common headers and values are pre-encoded at import time.

Design:
- Bytes internally: avoids repeated encoding in hot paths
- Lowercase keys: HTTP headers are case-insensitive, normalize once
- Separate header/value lookups: "Accept" is both a header and a Vary value
- HEADER_LOOKUP caches custom header names on miss via __missing__
- VALUE_LOOKUP uses .get() without caching (values may carry sensitive data)
"""

import re
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
# VALIDATION — rejects CTLs/newlines to prevent HTTP response splitting.
# Header names: RFC 9110 "token" — regex matches *invalid* bytes.
# Header values: reject CTLs except HT (0x09), allow obs-text (0x80-0xFF).
# =============================================================================

HEADER_RE = re.compile(rb'[\x00-\x1f\x7f()<>@,;:\\"/\[\]\?={} \t]')
HEADER_VALUE_RE = re.compile(b"[\x00-\x08\x0a-\x1f\x7f]")


def _validate_header(name: str | bytes) -> bytes:
    """Validate and normalize header name to lowercased bytes."""
    if isinstance(name, str):
        name = name.encode("latin-1")

    lowered = name.lower()

    if HEADER_RE.search(lowered):
        raise ValueError(f"Invalid header name: {name}")

    return lowered


def _validate_value(value: str | bytes) -> bytes:
    """Validate header value bytes."""
    if isinstance(value, str):
        value = value.encode("latin-1")

    if HEADER_VALUE_RE.search(value):
        raise ValueError(f"Invalid header value: {value}")

    return value


# =============================================================================
# LOOKUPS — separate to avoid collisions (e.g. "Accept" is both a header and a Vary value)
# =============================================================================


class _HeaderLookup(dict):
    """Self-populating header name lookup. Bounded to _MAX_SIZE entries."""

    __slots__ = ()
    _MAX_SIZE = 1024  # ~400 pre-populated + room for ~600 custom

    def __missing__(self, key: str | bytes) -> bytes:
        result = _validate_header(key)
        if len(self) < self._MAX_SIZE:
            self[key] = result
        return result


HEADER_LOOKUP: _HeaderLookup = _HeaderLookup()
_h = _hb = _lo = _lob = ""
for _h in ALL_HEADERS:
    _hb = _h.encode("latin-1")
    _lo = _h.lower()
    _lob = _lo.encode("latin-1")
    HEADER_LOOKUP[_h] = _lob
    HEADER_LOOKUP[_hb] = _lob
    HEADER_LOOKUP[_lo] = _lob
    HEADER_LOOKUP[_lob] = _lob

VALUE_LOOKUP: dict[str | bytes, bytes] = {}
_v = _vb = ""
for _v in ALL_VALUES:
    _vb = _v.encode("latin-1")
    VALUE_LOOKUP[_v] = _vb
    VALUE_LOOKUP[_vb] = _vb

del _h, _hb, _lo, _lob, _v, _vb


def encode_value(value: str | bytes) -> bytes:
    """Encode header value to bytes. Custom values validated per call (not cached)."""
    if encoded := VALUE_LOOKUP.get(value):
        return encoded
    return _validate_value(value)


# =============================================================================
# HEADERS CLASS
# =============================================================================


class Headers:
    """HTTP Headers container. Keys stored as lowercased bytes."""

    __slots__ = ("_data",)

    def __init__(
        self, raw_header_data: dict[bytes, bytes | list[bytes]] | None = None
    ) -> None:
        """
        WARNING: raw_header_data is used as-is with no validation or normalization.
        Caller must provide lowercased ASCII bytes keys and valid byte values.
        """
        self._data = raw_header_data or {}

    def add(self, name: str | bytes, value: str | bytes) -> Self:
        """Add a header (allows multiple values for same name)."""
        key = HEADER_LOOKUP[name]
        val = encode_value(value)
        if (existing := self._data.setdefault(key, val)) is not val:
            if isinstance(existing, list):
                existing.append(val)
            else:
                self._data[key] = [existing, val]
        return self

    def set(self, name: str | bytes, value: str | bytes) -> Self:
        """Set a header (replaces existing)."""
        self._data[HEADER_LOOKUP[name]] = encode_value(value)
        return self

    # -------------------------------------------------------------------------
    # Raw methods - no encoding/validation
    # -------------------------------------------------------------------------

    def radd(self, name: bytes, value: bytes) -> Self:
        """Add raw header. Caller must provide lowercased name and valid value bytes."""
        if (existing := self._data.setdefault(name, value)) is not value:
            if isinstance(existing, list):
                existing.append(value)
            else:
                self._data[name] = [existing, value]
        return self

    def rset(self, name: bytes, value: bytes) -> Self:
        """Set raw header. Caller must provide lowercased name and valid value bytes."""
        self._data[name] = value
        return self

    def update(self, other: dict[str | bytes, str | bytes] | None) -> Self:
        if other is None:
            return self
        # Directly add items to _data (faster than calling set repeatedly)
        for name, value in other.items():
            self._data[HEADER_LOOKUP[name]] = encode_value(value)
        return self

    def setdefault(self, name: str | bytes, value: str | bytes) -> str:
        """Set header if not present, return value as string."""
        key = HEADER_LOOKUP[name]
        existing = self._data.get(key)
        if existing is None:
            val = encode_value(value)
            self._data[key] = val
            return val.decode("latin-1")
        raw = existing if isinstance(existing, bytes) else existing[0]
        return raw.decode("latin-1")

    def get[T: str | None = None](
        self, name: str | bytes, default: T = None
    ) -> T | str:
        """Get first value for header, decoded as string."""
        value = self._data.get(name if isinstance(name, bytes) else HEADER_LOOKUP[name])
        if value is None:
            return default
        raw = value if isinstance(value, bytes) else value[0]
        return raw.decode("latin-1")

    def getlist(self, name: str | bytes) -> list[str]:
        """Get all values for header, decoded as strings."""
        value = self._data.get(name if isinstance(name, bytes) else HEADER_LOOKUP[name])
        if value is None:
            return []
        if isinstance(value, bytes):
            return [value.decode("latin-1")]
        return [v.decode("latin-1") for v in value]

    # -------------------------------------------------------------------------
    # Raw read methods - return bytes
    # -------------------------------------------------------------------------

    def rget[T: bytes | None = None](self, name: bytes, default: T = None) -> T | bytes:
        """Get first value for header as raw bytes."""
        value = self._data.get(name)
        if value is None:
            return default
        return value if isinstance(value, bytes) else value[0]

    def rgetlist(self, name: bytes) -> list[bytes]:
        """Get all values for header as raw bytes."""
        value = self._data.get(name)
        if value is None:
            return []
        return [value] if isinstance(value, bytes) else list(value)

    def remove(self, name: str | bytes) -> Self:
        """Remove a header."""
        self._data.pop(name if isinstance(name, bytes) else HEADER_LOOKUP[name], None)
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
        key = name if isinstance(name, bytes) else HEADER_LOOKUP[name]
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"Headers({self._data!r})"
