"""
Immutable request view; mutable I/O state is isolated in ``BodyReader`` so handlers see a stable ``Request`` shape.

Query, cookies, and host are parsed lazily. The reader applies size caps, timeouts, and backpressure so default
request handling stays safe without each route re-implementing upload limits.
"""

import asyncio
import http.cookies
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal

from stario.exceptions import ClientDisconnected, HttpException

from .headers import Headers
from .query import QueryParams


def _parse_cookies(cookie_string: str) -> dict[str, str]:
    """Parse Cookie header into dict."""
    cookie_dict: dict[str, str] = {}
    for chunk in cookie_string.split(";"):
        if "=" in chunk:
            key, val = chunk.split("=", 1)
        else:
            key, val = "", chunk
        key, val = key.strip(), val.strip()
        if key or val:
            cookie_dict[key] = http.cookies._unquote(val)
    return cookie_dict


# =============================================================================
# Backpressure thresholds
# =============================================================================
# These control when we pause/resume reading from the socket.
# HIGH_WATER_MARK: Pause reading when buffer exceeds this (prevent memory bloat)
# LOW_WATER_MARK: Resume reading when buffer drops below this (ensure throughput)
#
# Why 64KB high / 16KB low?
# - 64KB is enough to buffer typical request chunks without wasting memory
# - 16KB gap prevents rapid pause/resume cycling (hysteresis)
# - These values work well with typical TCP window sizes
# =============================================================================
LOW_WATER_MARK = 16 * 1024
HIGH_WATER_MARK = 64 * 1024

# =============================================================================
# Security limits
# =============================================================================
# DEFAULT_MAX_BODY_SIZE: Prevents memory exhaustion from large uploads.
# Uploads larger than this get 413 Payload Too Large.
# Override per-request via BodyReader.read(max_size=...) for file uploads.
DEFAULT_MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MB

# DEFAULT_MAX_HEADER_BYTES: Total size of the request line (method + URL in on_url chunks) plus all header
# name/value bytes before the blank line. Exceeding this yields 431 (Request Header Fields Too Large).
DEFAULT_MAX_HEADER_BYTES = 64 * 1024  # 64 KiB

# DEFAULT_BODY_TIMEOUT: Slowloris attack protection.
# If a client sends data slower than this between chunks, we abort.
# This prevents attackers from holding connections open indefinitely
# by sending 1 byte every 29 seconds.
DEFAULT_BODY_TIMEOUT = 30.0  # seconds


class BodyReader:
    """Protocol-owned buffer bridged to ``async`` readers: safe size limits, slow-read timeouts, and read pausing.

    Handlers normally reach this only via ``Request``; the HTTP layer calls ``feed``, ``complete``, and ``abort``.
    """

    __slots__ = (
        "_pause",
        "_resume",
        "_disconnect",
        "_chunks",
        "_buffered",
        "_streaming",
        "_complete",
        "_cached",
        "_data_ready",
        "_total_read",
        "_max_size",
        "_timeout",
        "_abort_reason",
        "send_100_continue",
    )

    def __init__(
        self,
        pause: Callable[[], None],
        resume: Callable[[], None],
        disconnect: asyncio.Future[None] | None = None,
        *,
        max_size: int = DEFAULT_MAX_BODY_SIZE,
        timeout: float = DEFAULT_BODY_TIMEOUT,
    ) -> None:
        self._pause = pause
        self._resume = resume
        self._disconnect = disconnect
        self._chunks: list[bytes] = []
        self._buffered = 0
        self._streaming = False
        self._complete = False
        self._cached: bytes | None = None
        self._data_ready = asyncio.Event()
        self._total_read = 0
        self._max_size = max_size
        self._timeout = timeout
        self._abort_reason: Literal["too_large", "disconnected", "timeout"] | None = (
            None
        )
        self.send_100_continue: Callable[[], None] | None = None

    def _raise_abort(self) -> None:
        reason = self._abort_reason
        if reason == "too_large":
            raise HttpException(413, "Request body too large")
        if reason == "timeout":
            raise HttpException(
                408,
                "Request timeout: body upload too slow. "
                "This may indicate a slowloris attack or very poor connection.",
            )
        if reason == "disconnected":
            raise ClientDisconnected("Client disconnected while reading request body")

    def feed(self, chunk: bytes) -> None:
        """Called by protocol when body data arrives."""
        self._total_read += len(chunk)

        # Enforce size limit
        if self._total_read > self._max_size:
            self._abort_reason = "too_large"
            self._data_ready.set()
            return

        self._chunks.append(chunk)
        self._buffered += len(chunk)
        self._data_ready.set()

        if self._buffered > HIGH_WATER_MARK:
            self._pause()

    def complete(self) -> None:
        """Protocol hook: message fully parsed (empty body still calls this)."""
        self._complete = True
        if self._abort_reason is None:
            # Small/medium bodies: one contiguous byte string for fast ``body()``.
            self._cached = b"".join(self._chunks)
        if self._streaming:
            # Streaming consumer may be blocked in ``wait_for``; wake it to exit.
            self._data_ready.set()
        else:
            self._chunks.clear()

    def abort(self) -> None:
        """Called by protocol when connection is lost."""
        if not self._complete and self._abort_reason is None:
            self._abort_reason = "disconnected"
        self._data_ready.set()  # Wake up stream()

    async def stream(self) -> AsyncIterator[bytes]:
        """Iterate body chunks as they arrive (single consumer per request).

        Raises:
            HttpException: ``413`` if the body exceeds the configured maximum size.
            HttpException: ``408`` if bytes stall longer than the body read timeout (slowloris protection).
            ClientDisconnected: If the peer closes before the body finishes.
            RuntimeError: If ``stream`` or ``read`` already consumed this body.
        """
        if self._abort_reason is not None:
            self._raise_abort()

        if self._cached is not None:
            yield self._cached
            return

        if self._streaming:
            raise RuntimeError(
                "Body already streaming. "
                "Each request body can only be streamed once. "
                "Use body() to read into memory if you need to access it multiple times."
            )
        self._streaming = True

        if self.send_100_continue:
            self.send_100_continue()

        index = 0
        while True:
            while index < len(self._chunks):
                chunk = self._chunks[index]
                self._buffered -= len(chunk)

                if self._buffered < LOW_WATER_MARK:
                    self._resume()
                yield chunk
                index += 1

            if self._abort_reason is not None:
                self._chunks.clear()
                self._raise_abort()

            if self._complete:
                self._chunks.clear()
                break

            if self._disconnect and self._disconnect.done():
                self._chunks.clear()
                if self._abort_reason is None:
                    self._abort_reason = "disconnected"
                self._raise_abort()

            # Timeout protection against slowloris attacks
            try:
                await asyncio.wait_for(self._data_ready.wait(), self._timeout)
            except asyncio.TimeoutError:
                self._abort_reason = "timeout"
                self._chunks.clear()
                self._raise_abort()
            self._data_ready.clear()

    async def read(self, max_size: int | None = None) -> bytes:
        """Buffer the entire body into one ``bytes`` object.

        Parameters:
            max_size: Optional override for this read's maximum bytes (defaults to the reader's configured cap).

        Raises:
            HttpException: ``413`` / ``408`` for oversize or stalled uploads.
            ClientDisconnected: If the peer closes mid-body.
            RuntimeError: If the body was already streamed.
        """
        if max_size is not None:
            self._max_size = max_size

        if self._cached is None:
            chunks: list[bytes] = []
            async for chunk in self.stream():
                chunks.append(chunk)
            self._cached = b"".join(chunks)

        return self._cached


class Request:
    """Stable snapshot of request-line data plus headers; body I/O goes through an internal ``BodyReader``.

    ``query_bytes`` is the raw ``?``-suffix from the URL (no leading ``?``). ``host``, ``query``,
    and ``cookies`` are computed lazily on first access. Do not reassign ``query_bytes`` after
    construction—it would desynchronize the cached ``query`` view.
    """

    __slots__ = (
        # HTTP data
        "method",
        "path",
        "headers",
        "protocol_version",
        "keep_alive",
        # Parsed data (lazy)
        "query_bytes",
        "_query",
        "_cookies",
        "_signals_cache",
        "_host",
        # Body
        "_body",
    )

    def __init__(
        self,
        *,
        method: str = "GET",
        path: str = "/",
        query_bytes: bytes = b"",
        protocol_version: str = "1.1",
        keep_alive: bool = True,
        headers: Headers,
        body: BodyReader,
    ) -> None:
        self.method = method
        self.path = path
        self.headers = headers
        self.protocol_version = protocol_version
        self.keep_alive = keep_alive
        self.query_bytes = query_bytes

        self._query: QueryParams | None = None
        self._cookies: dict[str, str] | None = None
        self._signals_cache: dict[str, Any] | None = None
        self._host: str | None = None

        self._body = body

    # =========================================================================
    # Host
    # =========================================================================

    @property
    def host(self) -> str:
        """``Host`` header value without port, lowercased; IPv6 hosts keep brackets. Empty string if missing."""
        if self._host is None:
            host_str = self.headers.get("host")
            if host_str:
                # Handle IPv6 addresses like [::1]:8000
                if host_str.startswith("["):
                    bracket_end = host_str.find("]")
                    if bracket_end != -1:
                        self._host = host_str[: bracket_end + 1].lower()
                    else:
                        self._host = host_str.lower()
                else:
                    # Regular host:port or just host
                    self._host = host_str.rsplit(":", 1)[0].lower()
            else:
                self._host = ""
        return self._host

    # =========================================================================
    # Query string
    # =========================================================================

    @property
    def query(self) -> QueryParams:
        """Parsed query string (``?a=1&a=2`` keeps multiple values); see ``QueryParams`` for API."""
        if self._query is None:
            self._query = QueryParams(self.query_bytes)
        return self._query

    # =========================================================================
    # Cookies
    # =========================================================================

    @property
    def cookies(self) -> dict[str, str]:
        """Merged ``Cookie`` header values (lazy parse)."""
        if self._cookies is None:
            self._cookies = {}
            for cookie_str in self.headers.getlist("cookie"):
                self._cookies.update(_parse_cookies(cookie_str))
        return self._cookies

    # =========================================================================
    # Body
    # =========================================================================

    async def body(self) -> bytes:
        """Return the entire body as ``bytes`` (empty if there is no body reader).

        Internally uses ``BodyReader.read`` on the protocol-owned reader (size cap, timeouts, backpressure).

        Raises:
            HttpException: ``413`` when the body exceeds the configured maximum size.
            HttpException: ``408`` when bytes stall longer than the body read timeout (slow upload / slowloris guard).
            ClientDisconnected: When the peer closes before the body finishes.
            RuntimeError: If the body was already streamed via ``stream()``.
        """
        if self._body is None:
            return b""
        return await self._body.read()

    async def stream(self) -> AsyncIterator[bytes]:
        """Stream the body; mutually exclusive with ``body()`` for a given request.

        Internally uses ``BodyReader.stream``.

        Yields:
            Consecutive body chunks.

        Raises:
            HttpException: ``413`` / ``408`` for oversize or stalled uploads (same rules as ``body()``).
            ClientDisconnected: When the peer closes before the body finishes.
            RuntimeError: If ``stream()`` or ``body()`` already consumed this body.
        """
        if self._body is None:
            return
        async for chunk in self._body.stream():
            yield chunk
