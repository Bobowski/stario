"""
Per-connection HTTP/1.1: httptools parses bytes; handlers run as tasks so this layer never blocks the event loop.

Shared ``disconnect`` futures tie body reads and long responses to the same socket lifetime. Pipelining and keep-alive
follow RFC behavior even when common clients use parallel connections instead. App work is scheduled via ``app.create_task``
so shutdown can observe the same task set the app registered.
"""

import asyncio
from collections import deque
from functools import lru_cache
from typing import Callable, cast
from urllib.parse import unquote as unquote_url

import httptools

from stario.telemetry.core import Tracer

from .app import App
from .context import Context
from .headers import Headers
from .request import BodyReader, Request
from .writer import (
    CompressionConfig,
    Writer,
    _get_status_line,
)

KEEP_ALIVE_TIMEOUT = 5.0


@lru_cache(maxsize=16)
def _decode_method(method_bytes: bytes) -> str:
    return method_bytes.decode("ascii")


@lru_cache(maxsize=4096)
def _decode_path(path_bytes: bytes) -> str:
    path: str = path_bytes.decode("ascii")
    if "%" in path:
        path = unquote_url(path)
    return path


class HttpProtocol(asyncio.Protocol):
    """
    ``asyncio.Protocol`` for one TCP (or Unix) connection: parse requests with httptools, run ``app`` per message, pipeline safely.

    Pipelined requests queue until the prior response finishes; ``BodyReader`` and ``Writer`` share one disconnect future per connection.
    """

    __slots__ = (  # type: ignore[assignment]
        "loop",
        "app",
        "tracer",
        "get_date_header",
        "compression",
        "parser",
        "transport",
        "timeout_handle",
        "_reading_headers",
        "_reading_body",
        "_reading_url_bytes",
        "_active_context",
        "_active_writer",
        "_disconnect",
        "_shutdown",
        "_pipeline",
        "_connections",
        "_max_request_header_bytes",
        "_max_request_body_bytes",
        "_request_head_bytes",
    )

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        app: App,
        tracer: Tracer,
        get_date_header: Callable[[], bytes],
        compression: CompressionConfig,
        shutdown: asyncio.Future,
        connections: set["HttpProtocol"],
        *,
        max_request_header_bytes: int,
        max_request_body_bytes: int,
    ) -> None:
        self.loop = loop
        self.app = app
        self.tracer = tracer
        self.get_date_header = get_date_header
        self.compression = compression
        # Process-wide: when set, new responses close the socket after send (see on_response_completed).
        self._shutdown = shutdown
        self._connections = connections
        self._max_request_header_bytes = max_request_header_bytes
        self._max_request_body_bytes = max_request_body_bytes
        self._request_head_bytes = 0

        self.parser: httptools.HttpRequestParser | None = httptools.HttpRequestParser(
            self
        )
        self.transport: asyncio.Transport | None = None
        self.timeout_handle: asyncio.TimerHandle | None = None

        # State of the request currently being read from the transport
        self._reading_headers: Headers | None = None
        self._reading_body: BodyReader | None = None
        self._reading_url_bytes: bytes = b""

        # State of the request currently being handled by the application
        self._active_context: Context | None = None
        self._active_writer: Writer | None = None

        # One Future per TCP connection: BodyReader, Writer, and SSE loops all await this.
        self._disconnect = loop.create_future()

        # Lazy: only allocated when a second full request parses while the first response is still in flight.
        self._pipeline: deque[tuple[Context, Writer]] | None = None

    # =========================================================================
    # Timeout
    # =========================================================================

    def _reset_timeout(self) -> None:
        transport = self.transport
        assert transport is not None
        self._cancel_timeout()
        self.timeout_handle = self.loop.call_later(
            KEEP_ALIVE_TIMEOUT,
            lambda: not transport.is_closing() and transport.close(),
        )

    def _cancel_timeout(self) -> None:
        if self.timeout_handle is not None:
            self.timeout_handle.cancel()
            self.timeout_handle = None

    # =========================================================================
    # asyncio.Protocol
    # =========================================================================

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.Transport, transport)
        self._connections.add(self)
        self._reset_timeout()

    def connection_lost(self, exc: Exception | None) -> None:
        self._cancel_timeout()
        self._connections.discard(self)

        # Wake anyone awaiting Writer/BodyReader disconnect (same Future as abrupt close).
        if not self._disconnect.done():
            self._disconnect.set_result(None)

        if self._reading_body is not None:
            self._reading_body.abort()

        self.transport = None
        self.parser = None

    def eof_received(self) -> None:
        pass

    def data_received(self, data: bytes) -> None:
        assert self.parser is not None
        self._cancel_timeout()

        try:
            self.parser.feed_data(data)
        except httptools.HttpParserError:
            self._close_with_error(400, "Invalid HTTP request")
        except httptools.HttpParserUpgrade:
            self._close_with_error(400, "Upgrade not supported")

    def pause_writing(self) -> None:
        # Kernel / transport asked us to stop sending; stop reading to apply backpressure upstream.
        if self.transport and not self.transport.is_closing():
            self.transport.pause_reading()

    def resume_writing(self) -> None:
        if self.transport and not self.transport.is_closing():
            self.transport.resume_reading()

    # =========================================================================
    # httptools Callbacks
    # =========================================================================

    def on_message_begin(self) -> None:
        assert self.transport is not None
        # Reserve bytes for ``METHOD SP`` and `` HTTP/x.x\\r\\n`` (URL fragments arrive via ``on_url``).
        self._request_head_bytes = 40
        self._reading_headers = Headers()
        self._reading_body = BodyReader(
            pause=self.transport.pause_reading,
            resume=self.transport.resume_reading,
            disconnect=self._disconnect,
            max_size=self._max_request_body_bytes,
        )

    def on_url(self, url: bytes) -> None:
        self._request_head_bytes += len(url)
        if self._request_head_bytes > self._max_request_header_bytes:
            self._close_with_error(431, "Request header fields too large")
            return
        self._reading_url_bytes += url

    def on_header(self, name: bytes, value: bytes) -> None:
        assert self._reading_headers is not None
        self._request_head_bytes += len(name) + len(value)
        if self._request_head_bytes > self._max_request_header_bytes:
            self._close_with_error(431, "Request header fields too large")
            return
        self._reading_headers.add(name, value)

    def on_headers_complete(self) -> None:
        parser = self.parser
        transport = self.transport
        headers = self._reading_headers
        body_reader = self._reading_body

        assert parser is not None
        assert transport is not None
        assert headers is not None
        assert body_reader is not None

        parsed_url = httptools.parse_url(self._reading_url_bytes)

        # Send 100 Continue response if expected
        if headers.get(b"expect") == b"100-continue":

            def send_100() -> None:
                if transport and not transport.is_closing():
                    transport.write(b"HTTP/1.1 100 Continue\r\n\r\n")

            body_reader.send_100_continue = send_100

        # Hand off to the app: one Request + Writer per message; handler runs in a task (see on_response_completed).
        try:
            path_str = _decode_path(parsed_url.path)
        except UnicodeDecodeError:
            self._close_with_error(400, "Invalid request path")
            return

        # fmt: off
        request = Request(
            method           = _decode_method(parser.get_method()),
            path             = path_str,
            query_bytes      = parsed_url.query or b"",
            protocol_version = parser.get_http_version(),
            keep_alive       = parser.should_keep_alive(),
            headers          = headers,
            body             = body_reader,
        )

        writer = Writer(
            transport_write = transport.write,
            get_date_header = self.get_date_header,
            on_completed    = self.on_response_completed,
            disconnect      = self._disconnect,
            shutdown        = self._shutdown,
            compression     = self.compression,
            accept_encoding = headers.get(b"accept-encoding"),
        )

        context = Context(
            app  = self.app,
            req  = request,
            span = self.tracer.create(request.method),
        )
        # fmt: on

        if self._active_context is None:
            self._active_context = context
            self._active_writer = writer

            self.app.create_task(self.app(context, writer))

        else:
            # Pipeline queue: must not run the next handler until bytes are fully written.
            transport.pause_reading()
            if self._pipeline is None:
                self._pipeline = deque()
            self._pipeline.append((context, writer))

    def on_body(self, body: bytes) -> None:
        if self._reading_body:
            self._reading_body.feed(body)

    def on_message_complete(self) -> None:
        if self._reading_body:
            self._reading_body.complete()

            self._reading_body = None
            self._reading_headers = None
            self._reading_url_bytes = b""

    # =========================================================================
    # Request Handling
    # =========================================================================

    def on_response_completed(self) -> None:
        t = self.transport
        w = self._active_writer
        c = self._active_context

        if t is None or w is None or c is None or t.is_closing() or w.disconnected:
            self._active_context = None
            self._active_writer = None
            return

        if (
            w.headers.rget(b"connection") == b"close"
            or not c.req.keep_alive
            or self._shutdown.done()
        ):
            t.close()
            self._active_context = None
            self._active_writer = None
            return

        # FIFO: pipeline order matches arrival order; each handler still runs only after prior body is written.
        if self._pipeline:
            next_c, next_w = self._pipeline.popleft()
            self._active_writer = next_w
            self._active_context = next_c

            self.app.create_task(self.app(next_c, next_w))
            self._cancel_timeout()
            t.resume_reading()
        else:
            self._active_context = None
            self._active_writer = None
            self._reset_timeout()
            t.resume_reading()

    # =========================================================================
    # Errors
    # =========================================================================

    def _close_with_error(self, status_code: int, message: str) -> None:
        transport = self.transport
        if transport is None:
            return

        body = message.encode("utf-8")
        parts = [
            _get_status_line(status_code),
            self.get_date_header(),
            b"content-type: text/plain; charset=utf-8\r\n",
            b"content-length: %d\r\n" % len(body),
            b"connection: close\r\n",
            b"\r\n",
            body,
        ]
        transport.write(b"".join(parts))
        transport.close()
