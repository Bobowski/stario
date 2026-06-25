"""
Immutable request view; mutable I/O state is isolated in `BodyReader` so handlers see a stable `Request` shape.

Query, cookies, and host are parsed lazily. The reader applies size caps, timeouts, and backpressure so default
request handling stays safe without each route re-implementing upload limits.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from types import MappingProxyType
from typing import Literal

from stario import cookies as cookie_helpers
from stario.exceptions import ClientDisconnected, HttpException, StarioRuntime

from .headers import Headers
from .query import ParsedQuery

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
# Override per read via Request.body(max_size=...) for smaller per-route limits.
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
    """Protocol-owned buffer bridged to `async` readers: safe size limits, slow-read timeouts, and read pausing.

    Handlers normally reach this only via `Request`. The HTTP layer calls `feed` and
    `complete` and supplies the connection disconnect future so blocked reads can exit.
    """

    __slots__ = (
        "_abort_reason",
        "_buffered",
        "_cached",
        "_chunks",
        "_complete",
        "_consumed_as",
        "_data_ready",
        "_disconnect",
        "_max_size",
        "_pause",
        "_resume",
        "_timeout",
        "_total_read",
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
        send_100_continue: Callable[[], None] | None = None,
    ) -> None:
        self._pause = pause
        self._resume = resume
        self._disconnect = disconnect
        self._chunks: list[bytes] = []
        self._buffered = 0
        self._consumed_as: Literal["body", "stream"] | None = None
        self._complete = False
        self._cached: bytes | None = None
        self._data_ready = asyncio.Event()
        self._total_read = 0
        self._max_size = max_size
        self._timeout = timeout
        self._abort_reason: Literal["too_large", "disconnected", "timeout"] | None = (
            None
        )
        self.send_100_continue = send_100_continue

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
            raise ClientDisconnected()

    def _take_chunk(self, index: int) -> tuple[int, bytes]:
        chunk = self._chunks[index]
        self._buffered -= len(chunk)
        if self._buffered < LOW_WATER_MARK:
            self._resume()
        return index + 1, chunk

    async def _wait_for_body_data(self) -> None:
        if self._abort_reason is not None:
            self._chunks.clear()
            self._raise_abort()
        if self._disconnect and self._disconnect.done():
            self._chunks.clear()
            if self._abort_reason is None:
                self._abort_reason = "disconnected"
            self._raise_abort()
        try:
            await asyncio.wait_for(self._data_ready.wait(), self._timeout)
        except TimeoutError:
            self._abort_reason = "timeout"
            self._chunks.clear()
            self._raise_abort()
        self._data_ready.clear()

    def _maybe_send_continue(self) -> None:
        if self.send_100_continue:
            self.send_100_continue()
            self.send_100_continue = None

    def feed(self, chunk: bytes) -> None:
        """Called by protocol when body data arrives."""
        self._total_read += len(chunk)

        # Enforce size limit
        if self._total_read > self._max_size:
            self._abort_reason = "too_large"
            # Streaming consumer may be blocked in `wait_for`; wake it to exit.
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
        if self._abort_reason is None and self._consumed_as is None:
            # Small/medium bodies: one contiguous byte string for fast `body()`.
            if not self._chunks:
                self._cached = b""
            elif len(self._chunks) == 1:
                self._cached = self._chunks[0]
            else:
                self._cached = b"".join(self._chunks)
        if self._consumed_as in {"body", "stream"}:
            # Active consumers may be blocked in `wait_for`; wake them to finish.
            self._data_ready.set()
        else:
            self._chunks.clear()

    def abort(self) -> None:
        """Mark the upload dead and wake any blocked consumer."""
        if self._abort_reason is not None:
            return
        self._abort_reason = "disconnected"
        self._data_ready.set()

    async def stream(self) -> AsyncIterator[bytes]:
        """Iterate body chunks as they arrive (single consumer per request).

        - `HttpException` (`413`): If the body exceeds the configured maximum size.
        - `HttpException` (`408`): If bytes stall longer than the body read timeout (slowloris protection).
        - `ClientDisconnected`: Peer closed before the request body finished uploading.
        - `StarioRuntime`: If `stream()` or `body()` already consumed this body.
        """
        if self._abort_reason is not None:
            self._raise_abort()

        if self._consumed_as == "body":
            raise StarioRuntime(
                "Body already read with body(). "
                "Use the returned bytes from body(); request bodies cannot switch to streaming after buffering.",
                help_text="Choose body() or stream() once per request — not both.",
            )

        if self._consumed_as == "stream":
            raise StarioRuntime(
                "Body already streaming. "
                "Each request body can only be streamed once. "
                "Use body() to read into memory if you need to access it multiple times.",
                help_text="Call stream() only once per request.",
            )

        self._consumed_as = "stream"

        if self._cached is not None:
            yield self._cached
            return

        self._maybe_send_continue()

        index = 0
        while True:
            while index < len(self._chunks):
                index, chunk = self._take_chunk(index)
                yield chunk

            if self._complete:
                self._chunks.clear()
                break

            await self._wait_for_body_data()

    async def read(self, max_size: int | None = None) -> bytes:
        """Buffer the entire body into one `bytes` object.

        - `max_size`: Optional maximum bytes for this read. The protocol-wide cap still applies.

        - `HttpException` (`413` / `408`): For oversize or stalled uploads.
        - `ClientDisconnected`: Peer closed before the request body finished uploading.
        - `StarioRuntime`: If the body was already streamed.
        """
        if max_size is not None and max_size < 0:
            raise ValueError("max_size must be non-negative.")

        if self._abort_reason is not None:
            self._raise_abort()

        if self._consumed_as == "stream":
            raise StarioRuntime(
                "Body already streamed. "
                "Each request body can only be consumed once unless body() buffered it first.",
                help_text="Choose body() or stream() once per request — not both.",
            )

        if self._cached is not None:
            if max_size is not None and len(self._cached) > max_size:
                raise HttpException(413, "Request body too large")
            self._consumed_as = "body"
            return self._cached

        self._maybe_send_continue()

        chunks: list[bytes] = []
        total = 0
        index = 0
        while True:
            while index < len(self._chunks):
                chunk = self._chunks[index]
                if max_size is not None and total + len(chunk) > max_size:
                    raise HttpException(413, "Request body too large")
                index, chunk = self._take_chunk(index)
                total += len(chunk)
                chunks.append(chunk)

            if self._complete:
                if self._cached is None:
                    self._cached = b"".join(chunks)
                self._chunks.clear()
                self._consumed_as = "body"
                return self._cached

            await self._wait_for_body_data()


class Request:
    """Stable snapshot of request-line data plus headers; body I/O goes through an internal `BodyReader`.

    `query_bytes` is the raw `?`-suffix from the URL (no leading `?`). `host`, `query`,
    and `cookies` are computed lazily on first access. Do not reassign `query_bytes` after
    construction or mutate the returned `cookies` dict in place — both desynchronize the
    cached views.
    """

    __slots__ = (
        # Body
        "_body",
        "_cookies",
        "_host",
        "_query",
        "headers",
        "keep_alive",
        # HTTP data
        "method",
        "path",
        "protocol_version",
        # Parsed data (lazy)
        "query_bytes",
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
        body: BodyReader | None,
    ) -> None:
        self.method = method
        self.path = path
        self.headers = headers
        self.protocol_version = protocol_version
        self.keep_alive = keep_alive
        self.query_bytes = query_bytes

        self._query: ParsedQuery | None = None
        self._cookies: dict[str, str] | None = None
        self._host: str | None = None

        self._body = body

    # =========================================================================
    # Host
    # =========================================================================

    @property
    def host(self) -> str:
        """`Host` header value without port, lowercased; IPv6 hosts keep brackets. Empty string if missing."""
        if self._host is None:
            host_str = (self.headers.get("host") or "").strip()
            if not host_str:
                self._host = ""
            elif host_str.startswith("["):
                # Bracketed IPv6 literal, optional :port suffix.
                bracket_end = host_str.find("]")
                if bracket_end == -1:
                    self._host = host_str.lower()
                else:
                    host = host_str[: bracket_end + 1].lower()
                    rest = host_str[bracket_end + 1 :]
                    if rest and (not rest.startswith(":") or not rest[1:].isdigit()):
                        self._host = host_str.lower()
                    else:
                        self._host = host
            elif ":" in host_str:
                host_part, _, port_part = host_str.rpartition(":")
                # Only strip a numeric port; bare colons stay in IPv6 literals.
                self._host = (
                    host_part.lower()
                    if port_part.isdigit() and host_part
                    else host_str.lower()
                )
            else:
                self._host = host_str.lower()
        return self._host

    # =========================================================================
    # Query string
    # =========================================================================

    @property
    def query(self) -> ParsedQuery:
        """Parsed query string (`?a=1&a=2` keeps multiple values); see `ParsedQuery` for API."""
        if self._query is None:
            self._query = ParsedQuery(self.query_bytes)
        return self._query

    # =========================================================================
    # Cookies
    # =========================================================================

    @property
    def cookies(self) -> Mapping[str, str]:
        """Merged `Cookie` header values (lazy parse). Read-only view.

        Parsed with RFC 6265 cookie-value rules via `http.cookies.SimpleCookie`.
        Later `Cookie` header lines win; within one header, later names override earlier.
        """
        if self._cookies is None:
            self._cookies = cookie_helpers.parse_cookie_headers(
                self.headers.getlist("cookie")
            )
        return MappingProxyType(self._cookies)

    # =========================================================================
    # Body
    # =========================================================================

    async def body(self, max_size: int | None = None) -> bytes:
        """Return the entire body as `bytes` (empty if there is no body reader).

        Internally uses `BodyReader.read` on the protocol-owned reader (size cap, timeouts, backpressure).

        - `max_size`: Optional lower per-call limit. The server's configured maximum body size still applies.
        - `HttpException` (`413`): When the body exceeds the configured maximum size.
        - `HttpException` (`408`): When bytes stall longer than the body read timeout (slow upload / slowloris guard).
        - `ClientDisconnected`: When the peer closes before the request body finishes uploading.
        - `StarioRuntime`: If the body was already streamed via `stream()`.
        """
        if self._body is None:
            return b""
        return await self._body.read(max_size=max_size)

    async def stream(self) -> AsyncIterator[bytes]:
        """Stream the body; mutually exclusive with `body()` for a given request.

        Internally uses `BodyReader.stream`.

        - `HttpException` (`413` / `408`): For oversize or stalled uploads (same rules as `body()`).
        - `ClientDisconnected`: When the peer closes before the request body finishes uploading.
        - `StarioRuntime`: If `stream()` or `body()` already consumed this body.
        """
        if self._body is None:
            # No body reader → empty stream.
            return
        async for chunk in self._body.stream():
            yield chunk
