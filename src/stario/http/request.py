"""
HTTP Request - Read from this.

Simple, immutable request data. No connection state.
"""

import asyncio
import http.cookies
import json as json_module
from collections.abc import AsyncIterator, Callable
from typing import Any

from stario.exceptions import HttpException

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

# DEFAULT_BODY_TIMEOUT: Slowloris attack protection.
# If a client sends data slower than this between chunks, we abort.
# This prevents attackers from holding connections open indefinitely
# by sending 1 byte every 29 seconds.
DEFAULT_BODY_TIMEOUT = 30.0  # seconds


class BodyReader:
    """
    Request body reader with backpressure and security limits.

    Features:
    - Backpressure: pauses reading when buffer is full
    - Size limit: rejects bodies exceeding max_size
    - Timeout: rejects slow requests (slowloris defense)
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
        "_aborted",
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
        self._aborted = False
        self.send_100_continue: Callable[[], None] | None = None

    def feed(self, chunk: bytes) -> None:
        """Called by protocol when body data arrives."""
        self._total_read += len(chunk)

        # Enforce size limit
        if self._total_read > self._max_size:
            self._aborted = True
            self._data_ready.set()
            return

        self._chunks.append(chunk)
        self._buffered += len(chunk)
        self._data_ready.set()

        if self._buffered > HIGH_WATER_MARK:
            self._pause()

    def complete(self) -> None:
        """Called by protocol when body is fully received."""
        self._complete = True
        if not self._aborted:
            self._cached = b"".join(self._chunks)
        if self._streaming:
            self._data_ready.set()
        else:
            self._chunks.clear()

    def abort(self) -> None:
        """Called by protocol when connection is lost."""
        self._aborted = True
        self._data_ready.set()  # Wake up stream()

    async def stream(self) -> AsyncIterator[bytes]:
        """
        Stream body chunks as they arrive.

        Raises:
            HttpException: If body exceeds max_size (413 Payload Too Large)
            HttpException: If reading times out (408 Request Timeout)
        """
        if self._aborted:
            raise HttpException(413, "Request body too large")

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

            if self._aborted:
                self._chunks.clear()
                raise HttpException(413, "Request body too large")

            if self._complete or (self._disconnect and self._disconnect.done()):
                self._chunks.clear()
                break

            # Timeout protection against slowloris attacks
            try:
                await asyncio.wait_for(self._data_ready.wait(), self._timeout)
            except asyncio.TimeoutError:
                self._aborted = True
                self._chunks.clear()
                raise HttpException(
                    408,
                    "Request timeout: body upload too slow. "
                    "This may indicate a slowloris attack or very poor connection.",
                )
            self._data_ready.clear()

    async def read(self, max_size: int | None = None) -> bytes:
        """
        Read entire body into memory.

        Args:
            max_size: Override the default max body size for this read.

        Raises:
            HttpException: If body exceeds max_size (413 Payload Too Large)
            HttpException: If reading times out (408 Request Timeout)
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
    __slots__ = (
        # HTTP data
        "method",
        "path",
        "tail",
        "subhost",
        "headers",
        "protocol_version",
        "keep_alive",
        # Parsed data (lazy)
        "_query_bytes",
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
        self.tail = ""  # The rest of the path after the prefix match /*
        self.subhost = (
            ""  # The matched wildcard portion for host routing (*.example.com)
        )
        self.headers = headers
        self.protocol_version = protocol_version
        self.keep_alive = keep_alive
        self._query_bytes = query_bytes

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
        """
        Host from the Host header (without port, lowercase).

        Lowercase for case-insensitive matching per HTTP/DNS spec.
        Returns empty string if no Host header is present.
        """
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
        """Parsed query parameters with consistent access.

        Usage:
            req.query.get("page")           → str | None
            req.query.get("page", "1")      → str
            req.query.getlist("tags")        → list[str]
            "page" in req.query             → bool
        """
        if self._query is None:
            self._query = QueryParams(self._query_bytes)
        return self._query

    # =========================================================================
    # Cookies
    # =========================================================================

    @property
    def cookies(self) -> dict[str, str]:
        """Parsed cookies from Cookie header."""
        if self._cookies is None:
            self._cookies = {}
            for cookie_str in self.headers.getlist("cookie"):
                self._cookies.update(_parse_cookies(cookie_str))
        return self._cookies

    # =========================================================================
    # Body
    # =========================================================================

    async def body(self) -> bytes:
        """Read entire request body into memory."""
        if self._body is None:
            return b""
        return await self._body.read()

    async def stream(self) -> AsyncIterator[bytes]:
        """Stream request body chunks."""
        if self._body is None:
            return
        async for chunk in self._body.stream():
            yield chunk

    async def json(self) -> Any:
        """
        Parse request body as JSON.

        Raises:
            HttpException(400): If body is not valid JSON
        """
        data = await self.body()
        try:
            return json_module.loads(data)
        except json_module.JSONDecodeError as e:
            raise HttpException(400, f"Invalid JSON: {e.msg}") from e
