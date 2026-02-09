"""
HTTP Writer - Go-style response writer.

Owns all connection state (disconnect, transport).
Datastar-first with SSE streaming as the primary mode.

Raw Methods (no compression):
- write_headers(status) - send HTTP status line and headers
- write(data) - send raw bytes (chunked if no Content-Length)
- end(data?) - finalize response

Compressed Methods (use compressor if configured):
- html(), json(), text() - full body compression
- patch(), sync(), etc. - SSE streaming compression
"""

import asyncio
import http
import http.cookies
import json as json_module
import zlib
from compression import zstd
from dataclasses import dataclass
from datetime import datetime
from email.utils import format_datetime
from functools import lru_cache
from typing import (
    Any,
    AsyncIterator,
    Callable,
    ClassVar,
    Literal,
    Self,
    overload,
)
from urllib.parse import quote

# =============================================================================
# Optional compression backends - Python 3.14 stdlib preferred
# =============================================================================
import brotli

from stario.datastar import sse
from stario.datastar.format import SignalData
from stario.html import HtmlElement
from stario.html import render as html_render

from .headers import Headers

# =============================================================================
# Compressor - base class with shared logic
# =============================================================================
# Why three compression algorithms?
# - zstd: Best ratio + speed, but requires Python 3.14+ stdlib. Future default.
# - brotli: Excellent ratio, great browser support (94%+). Current best choice.
# - gzip: Universal fallback. Every browser supports it, slower but reliable.
#
# Priority order (zstd > brotli > gzip) maximizes compression while respecting
# what the client advertises in Accept-Encoding.
# =============================================================================


class Compressor:
    """
    Base compressor with shared logic.

    Subclasses implement frame() for one-shot and block() for streaming.
    """

    __slots__ = ("_level", "_min_size", "_stream")
    encoding: ClassVar[bytes] = b""

    def __init__(self, level: int, min_size: int) -> None:
        self._level = level
        self._min_size = min_size
        self._stream: Any = None

    def compressible(self, data: bytes) -> bool:
        """Return True if data size meets minimum threshold for compression."""
        return len(data) >= self._min_size

    def frame(self, data: bytes) -> bytes:
        """Compress entire body at once (one-shot)."""
        raise NotImplementedError

    def block(self, data: bytes) -> bytes:
        """Compress a chunk/block for streaming (e.g., SSE)."""
        raise NotImplementedError


class _Zstd(Compressor):
    """Zstandard - Python 3.14 stdlib. Fastest with best ratio."""

    encoding = b"zstd"

    def frame(self, data: bytes) -> bytes:
        return zstd.compress(data, level=self._level)

    def block(self, data: bytes) -> bytes:
        if self._stream is None:
            self._stream = zstd.ZstdCompressor(level=self._level)
        return self._stream.compress(data, mode=1)  # Flush block


class _Brotli(Compressor):
    """Brotli - great ratio, excellent browser support."""

    encoding = b"br"

    def frame(self, data: bytes) -> bytes:
        return brotli.compress(data, quality=self._level)

    def block(self, data: bytes) -> bytes:
        if self._stream is None:
            self._stream = brotli.Compressor(quality=self._level)
        return self._stream.process(data) + self._stream.flush()


class _Gzip(Compressor):
    """Gzip - universal fallback, always available."""

    encoding = b"gzip"

    def frame(self, data: bytes) -> bytes:
        c = zlib.compressobj(self._level, zlib.DEFLATED, 31)
        return c.compress(data) + c.flush()

    def block(self, data: bytes) -> bytes:
        if self._stream is None:
            self._stream = zlib.compressobj(self._level, zlib.DEFLATED, 31)
        return self._stream.compress(data) + self._stream.flush(zlib.Z_SYNC_FLUSH)


# =============================================================================
# Compression Configuration
# =============================================================================


@dataclass(slots=True, frozen=True)
class CompressionConfig:
    """
    Application-level compression configuration.

    Set level to negative to disable a specific algorithm.
    Level 0 is valid for brotli (fastest).

    Example:
        CompressionConfig()                        # All enabled, defaults
        CompressionConfig(zstd_level=-1)           # Disable zstd
        CompressionConfig(zstd_level=-1, brotli_level=-1)  # Only gzip
    """

    min_size: int = 256
    zstd_level: int = 3  # 1-22, negative to disable
    brotli_level: int = 4  # 0-11, negative to disable
    gzip_level: int = 6  # 1-9, negative to disable

    def select(self, accept_encoding: str | None) -> Compressor | None:
        """
        Select compressor for request based on Accept-Encoding.

        Priority: zstd > brotli > gzip.
        Returns None if no compression supported/requested.
        """
        if accept_encoding is None:
            return None

        accept = accept_encoding.lower()

        if self.zstd_level >= 0 and "zstd" in accept:
            return _Zstd(self.zstd_level, self.min_size)

        if self.brotli_level >= 0 and "br" in accept:
            return _Brotli(self.brotli_level, self.min_size)

        if self.gzip_level >= 0 and "gzip" in accept:
            return _Gzip(self.gzip_level, self.min_size)

        return None


# =============================================================================
# HTTP Status Line Cache
# =============================================================================


@lru_cache(maxsize=128)
def _get_status_line(status_code: int) -> bytes:
    """Build HTTP/1.1 status line."""
    try:
        phrase = http.HTTPStatus(status_code).phrase.encode()
    except ValueError:
        phrase = b""
    return b"HTTP/1.1 %d %s\r\n" % (status_code, phrase)


# =============================================================================
# Writer
# =============================================================================


class Writer:
    """
    HTTP Response Writer (Go-style).

    Raw methods (write_headers, write, end) handle chunking.
    Convenience methods (html, json, patch, etc.) also handle compression.

    Internal state:
    - _known_length: True if Content-Length was set (no chunking needed)
    - _completed: True after end() called
    - _compress: Compressor instance or None if no compression
    """

    __slots__ = (
        "_transport_write",
        "_get_date_header",
        "_disconnect",
        "_shutdown",
        "_on_completed",
        "_status_code",
        "_known_length",
        "_completed",
        "_compress",
        "headers",
    )

    def __init__(
        self,
        transport_write: Callable[[bytes], None],
        get_date_header: Callable[[], bytes],
        on_completed: Callable[[], None],
        disconnect: asyncio.Future,
        shutdown: asyncio.Future,
        compress: Compressor | None,
    ) -> None:
        self._transport_write = transport_write
        self._get_date_header = get_date_header
        self._disconnect = disconnect
        self._shutdown = shutdown
        self._on_completed = on_completed

        self._status_code: int | None = None
        self._known_length = False  # True if Content-Length set (no chunking)
        self._compress = compress  # None if no compression
        self._completed = False

        # User can set these:
        self.headers = Headers()

    # =========================================================================
    # Connection state
    # =========================================================================

    @property
    def status_code(self) -> int | None:
        """Status code sent, or None if headers not yet sent."""
        return self._status_code

    @property
    def started(self) -> bool:
        """True if headers sent."""
        return self._status_code is not None

    @property
    def completed(self) -> bool:
        """True if response complete."""
        return self._completed

    @property
    def disconnected(self) -> bool:
        """True if client has disconnected."""
        return self._disconnect.done()

    @property
    def shutting_down(self) -> bool:
        """True if server is shutting down."""
        return self._shutdown.done()

    @overload
    def alive(self, source: None = None) -> _Alive[None]: ...

    @overload
    def alive[T](self, source: AsyncIterator[T]) -> _Alive[T]: ...

    def alive[T](
        self, source: AsyncIterator[T] | None = None
    ) -> _Alive[T] | _Alive[None]:
        """
        Async iterator and context manager for connection lifecycle.

        Three usage patterns:
        1. Infinite loop (await inside):
            async for _ in w.alive():
                msg = await queue.get()
                w.patch(render(msg))
            cleanup()

        2. Iterate async source:
            async for msg in w.alive(message_stream):
                w.patch(render(msg))
            cleanup()

        3. One-shot operation:
            async with w.alive():
                result = await slow_api_call()
                w.patch(render(result))
            cleanup()

        All patterns exit cleanly on disconnect or server shutdown.
        Code after the block always runs for cleanup.
        """
        return _Alive(self, source)

    # =========================================================================
    # SSE/Datastar streaming (compressed)
    # =========================================================================

    def _set_sse_headers(self) -> None:
        """Ensure SSE headers are set. Called on first streaming method."""
        if self._completed:
            raise RuntimeError(
                "Cannot send SSE events after response is completed. "
                "This happens when you call w.patch()/w.sync() after w.end() or a one-shot method like w.html(). "
                "For SSE streaming, use w.patch()/w.sync() before any finalization."
            )

        if not self.started:
            h = self.headers
            h.rset(b"content-type", b"text/event-stream")
            h.rset(b"cache-control", b"no-cache")
            h.rset(b"connection", b"keep-alive")
            h.rset(b"transfer-encoding", b"chunked")

            if self._compress is not None:
                h.rset(b"content-encoding", self._compress.encoding)
                h.rset(b"vary", b"accept-encoding")

    def patch(
        self,
        element: HtmlElement,
        *,
        mode: Literal[
            "outer",
            "inner",
            "prepend",
            "append",
            "before",
            "after",
        ] = "outer",
        selector: str | None = None,
        use_view_transition: bool = False,
    ) -> None:
        """Patch DOM elements via SSE (compressed if configured)."""
        self._set_sse_headers()
        self.write(
            sse.patch(
                element,
                mode=mode,
                selector=selector,
                use_view_transition=use_view_transition,
            )
        )

    def sync(
        self,
        data: SignalData,
        *,
        only_if_missing: bool = False,
    ) -> None:
        """
        Update reactive signals via SSE (compressed if configured).

        Accepts dict, dataclass instance, Pydantic model, or TypedDict.
        Non-dict types are automatically converted to JSON-serializable dicts.

        Args:
            data: Signal values (dict, dataclass, Pydantic model, or TypedDict)
            only_if_missing: Only set if signal doesn't exist on client

        Raises:
            StarioError: If data cannot be converted to JSON-serializable dict

        Examples:
            # Using dict:
            w.sync({"count": 5, "name": "test"})

            # Using dataclass:
            @dataclass
            class Signals:
                count: int = 0
                name: str = ""

            w.sync(Signals(count=5, name="test"))
        """
        self._set_sse_headers()
        self.write(sse.signals(data, only_if_missing=only_if_missing))

    def navigate(self, url: str) -> None:
        """Navigate browser to URL via SSE."""
        self._set_sse_headers()
        self.write(sse.redirect(url))

    def execute(self, code: str, *, auto_remove: bool = True) -> None:
        """Execute JavaScript on client via SSE."""
        self._set_sse_headers()
        self.write(sse.script(code, auto_remove=auto_remove))

    def remove(self, selector: str) -> None:
        """Remove elements matching selector from DOM via SSE."""
        self._set_sse_headers()
        self.write(sse.remove(selector))

    # =========================================================================
    # One-shot responses (compressed)
    # =========================================================================

    def _compress_body(self, body: bytes) -> bytes:
        """Compress body if compressor set and meets threshold."""
        c = self._compress
        h = self.headers
        if (
            c is None
            or not c.compressible(body)
            or h.rget(b"content-encoding") is not None
        ):
            return body

        # Set headers to indicate compression is in use
        h.rset(b"content-encoding", c.encoding)
        h.rset(b"vary", b"accept-encoding")
        return c.frame(body)

    def respond(self, body: bytes, content_type: bytes, status: int = 200) -> None:
        """Send whole response (compressed if configured)."""
        body = self._compress_body(body)
        h = self.headers
        h.set(b"content-type", content_type)
        h.rset(b"content-length", b"%d" % len(body))
        self.write_headers(status).end(body)

    def json(self, data: Any, status: int = 200) -> None:
        """Send JSON response (compressed if configured)."""
        body = json_module.dumps(data, separators=(",", ":"), ensure_ascii=False)
        self.respond(body.encode(), b"application/json; charset=utf-8", status)

    def html(self, content: HtmlElement, status: int = 200) -> None:
        """Send HTML response (compressed if configured)."""
        self.respond(html_render(content).encode(), b"text/html; charset=utf-8", status)

    def text(self, content: str, status: int = 200) -> None:
        """Send plain text response (compressed if configured)."""
        self.respond(content.encode(), b"text/plain; charset=utf-8", status)

    def redirect(self, url: str, status: int = 307) -> None:
        """Send HTTP redirect response (301, 302, 303, 307, 308)."""
        location = quote(str(url), safe=":/%#?=@[]!$&'()*+,;")
        h = self.headers
        h.rset(b"location", location.encode("latin-1"))
        h.rset(b"content-length", b"0")
        self.write_headers(status).end()

    def empty(self, status: int = 204) -> None:
        """Send empty response (204, 304)."""
        self.headers.rset(b"content-length", b"0")
        self.write_headers(status).end()

    # =========================================================================
    # Cookies
    # =========================================================================

    def cookie(
        self,
        name: str,
        value: str,
        *,
        max_age: int | None = None,
        expires: datetime | str | int | None = None,
        path: str = "/",
        domain: str | None = None,
        secure: bool = False,
        httponly: bool = False,
        samesite: Literal["lax", "strict", "none"] | None = "lax",
    ) -> Self:
        """
        Set a cookie on the response.

        SECURITY: For session cookies, ALWAYS use:
            w.cookie("session", token, httponly=True, secure=True)

        - httponly=True prevents JavaScript access (mitigates XSS cookie theft)
        - secure=True ensures cookie only sent over HTTPS (prevents MITM)
        - samesite="lax" (default) prevents most CSRF attacks

        Args:
            name: Cookie name
            value: Cookie value
            max_age: Max age in seconds (None = session cookie)
            expires: Expiration datetime, string, or Unix timestamp
            path: Cookie path (default "/")
            domain: Cookie domain (None = current domain)
            secure: Only send over HTTPS (ALWAYS True for auth cookies!)
            httponly: Not accessible via JavaScript (ALWAYS True for auth cookies!)
            samesite: "lax", "strict", or "none"

        Returns:
            Self for chaining

        Example:
            # Auth cookie (secure defaults for production):
            w.cookie("session", token, httponly=True, secure=True, max_age=86400)

            # Preference cookie (can be less restrictive):
            w.cookie("theme", "dark", max_age=31536000)
        """
        cookie: http.cookies.BaseCookie[str] = http.cookies.SimpleCookie()
        cookie[name] = value

        if max_age is not None:
            cookie[name]["max-age"] = str(max_age)
        if expires is not None:
            if isinstance(expires, datetime):
                cookie[name]["expires"] = format_datetime(expires, usegmt=True)
            elif isinstance(expires, int):
                cookie[name]["expires"] = str(expires)
            else:
                cookie[name]["expires"] = expires
        if path:
            cookie[name]["path"] = path
        if domain:
            cookie[name]["domain"] = domain
        if secure:
            cookie[name]["secure"] = True
        if httponly:
            cookie[name]["httponly"] = True
        if samesite:
            cookie[name]["samesite"] = samesite

        cookie_bytes = cookie.output(header="").strip().encode("latin-1")
        self.headers.radd(b"set-cookie", cookie_bytes)
        return self

    def delete_cookie(
        self,
        name: str,
        *,
        path: str = "/",
        domain: str | None = None,
    ) -> Self:
        """
        Delete a cookie by setting it to expire immediately.

        Args:
            name: Cookie name to delete
            path: Cookie path (must match the path used when setting)
            domain: Cookie domain (must match the domain used when setting)

        Returns:
            Self for chaining

        Example:
            w.delete_cookie("session")
        """
        return self.cookie(
            name,
            "",
            max_age=0,
            expires="Thu, 01 Jan 1970 00:00:00 GMT",
            path=path,
            domain=domain,
        )

    # =========================================================================
    # Raw methods (no compression, Go-style)
    # =========================================================================

    def write_headers(self, status_code: int) -> Self:
        """
        Send HTTP status line and headers.

        Headers are sent immediately to the transport.
        If Content-Length is set, uses fixed-length mode.
        Otherwise, uses chunked transfer encoding.
        """
        if self.disconnected:
            return self

        if self._status_code:
            raise RuntimeError(
                "Response already started (headers sent). "
                "Cannot call write_headers() twice. Headers are sent on first write or when calling one-shot methods. "
                "Set headers via w.headers.set() before any write operations."
            )

        headers = self.headers

        # Determine mode based on existing headers
        if headers.rget(b"content-length") is not None:
            # When we know the length, we don't need to use chunked encoding
            headers.remove(b"transfer-encoding")
            self._known_length = True

        else:
            headers.rset(b"transfer-encoding", b"chunked")
            self._known_length = False

        parts = [_get_status_line(status_code), self._get_date_header()]
        append = parts.append

        for name, value in headers._data.items():
            if isinstance(value, bytes):
                append(name)
                append(b": ")
                append(value)
                append(b"\r\n")
            else:
                for v in value:
                    append(name)
                    append(b": ")
                    append(v)
                    append(b"\r\n")

        append(b"\r\n")
        self._transport_write(b"".join(parts))
        self._status_code = status_code

        return self

    def write(self, data: bytes) -> Self:
        """
        Write bytes to response.

        If Content-Length was set, writes directly (user controls compression).
        Otherwise, writes as HTTP chunked encoding with compression if configured.
        """
        if self.disconnected:
            return self

        if self._completed:
            raise RuntimeError(
                "Cannot write after response is completed. "
                "This happens after calling w.end() or one-shot methods like w.html(), w.json(), w.text(). "
                "Each handler should only send one response."
            )

        if self._status_code is None:
            self.write_headers(200)

        if self._known_length:
            # User set Content-Length, they control compression
            self._transport_write(data)
        elif self._compress is not None:
            compressed = self._compress.block(data)
            self._transport_write(b"%x\r\n%s\r\n" % (len(compressed), compressed))
        else:
            self._transport_write(b"%x\r\n%s\r\n" % (len(data), data))

        return self

    def end(self, data: bytes | None = None) -> None:
        """
        Finalize response.

        After end(), the response is complete and no more writes allowed.
        """
        if self._completed or self.disconnected:
            return

        if self._status_code is None:
            # Not started - send minimal response
            cl = b"%d" % (len(data) if data else 0)
            self.headers.rset(b"content-length", cl)
            self.write_headers(200 if data else 204)

        if data:
            self.write(data)

        if not self._known_length:
            self._transport_write(b"0\r\n\r\n")

        self._on_completed()
        self._completed = True


# =============================================================================
# Alive Helper
# =============================================================================


class _Alive[T]:
    """
    Connection lifecycle helper.

    Works as both async iterator and async context manager.
    Exits cleanly on disconnect or server shutdown.
    """

    __slots__ = ("_w", "_source", "_watcher")

    def __init__(self, w: Writer, source: AsyncIterator[T] | None = None) -> None:
        self._w = w
        self._source = source
        self._watcher: asyncio.Task[None] | None = None

    async def __aiter__(self) -> AsyncIterator[T]:
        """Iterate until disconnect/shutdown."""
        async with self:
            if self._source is None:
                while True:
                    yield None  # type: ignore[misc]
            else:
                async for item in self._source:
                    yield item

    async def __aenter__(self) -> Self:
        """Start watching for disconnect."""
        current_task = asyncio.current_task()

        async def watcher() -> None:
            either = asyncio.Future[None]()

            def trigger(_) -> None:
                if not either.done():
                    either.set_result(None)

            self._w._disconnect.add_done_callback(trigger)
            self._w._shutdown.add_done_callback(trigger)

            try:
                await either
                if current_task:
                    current_task.cancel()
            finally:
                self._w._disconnect.remove_done_callback(trigger)
                self._w._shutdown.remove_done_callback(trigger)

        self._watcher = asyncio.create_task(watcher())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        """Clean up watcher, swallow expected cancellations."""
        if self._watcher:
            self._watcher.cancel()
            try:
                await self._watcher
            except asyncio.CancelledError:
                pass

        return exc_type is asyncio.CancelledError
