"""
HTTP/1.1 Protocol - Simple, fast.

This module handles the low-level HTTP/1.1 parsing and connection management.
Uses httptools (http-parser via Cython) for parsing.

Design decisions:
- Task tracking: Handler tasks are registered in a per-worker set shared with
  the server. During shutdown, connections are closed first (graceful timeout),
  then any remaining handler tasks are cancelled. Only stario-managed tasks
  are affected; external tasks on the same loop are never touched.
- Pipelining support: Requests are queued if the prior response isn't complete.
  This is rare in practice (browsers mostly use concurrent connections), but
  required for HTTP/1.1 compliance.
- Keep-alive timeout: 5 seconds of idle before closing. This balances resource
  usage against connection reuse. Most real traffic comes with Connection: keep-alive.
- Disconnect future: Shared across all requests on a connection. When the client
  disconnects, all SSE streams and body readers are notified immediately.
"""

import asyncio
from collections import deque
from collections.abc import Coroutine
from functools import lru_cache
from typing import Any, Callable, cast
from urllib.parse import unquote as unquote_url

import httptools

from .headers import Headers
from .request import BodyReader, Request
from .writer import (
    Compressor,
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
    HTTP/1.1 protocol handler.

    One instance per connection.
    """

    __slots__ = (  # type: ignore[assignment]
        "loop",
        "request_handler",
        "get_date_header",
        "get_compressor",
        "parser",
        "transport",
        "timeout_handle",
        "_reading_headers",
        "_reading_body",
        "_reading_url_bytes",
        "_active_request",
        "_active_writer",
        "_disconnect",
        "_shutdown",
        "_pipeline",
        "_connections",
        "_tasks",
    )

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        request_handler: Callable[[Request, Writer], Coroutine[Any, Any, None]],
        get_date_header: Callable[[], bytes],
        get_compressor: Callable[[str | None], Compressor | None],
        shutdown: asyncio.Future,
        connections: set["HttpProtocol"],
        tasks: set[asyncio.Task[None]],
    ) -> None:
        self.loop = loop
        self.request_handler = request_handler
        self.get_date_header = get_date_header
        self.get_compressor = get_compressor
        self._shutdown = shutdown
        self._connections = connections
        self._tasks = tasks

        self.parser = httptools.HttpRequestParser(self)
        self.transport: asyncio.Transport | None = None
        self.timeout_handle: asyncio.TimerHandle | None = None

        # State of the request currently being read from the transport
        self._reading_headers: Headers | None = None
        self._reading_body: BodyReader | None = None
        self._reading_url_bytes: bytes = b""

        # State of the request currently being handled by the application
        self._active_request: Request | None = None
        self._active_writer: Writer | None = None

        # Common disconnect info for all requests on this connection
        self._disconnect = asyncio.Future()

        self._pipeline: deque[tuple[Request, Writer]] | None = None

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

        # Notify response writer and body reader that connection is lost
        if not self._disconnect.done():
            self._disconnect.set_result(None)

        if self._reading_body is not None:
            self._reading_body.abort()

        self.transport = None
        self.parser = None  # type: ignore

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
        self._reading_headers = Headers()
        self._reading_body = BodyReader(
            pause=self.transport.pause_reading,
            resume=self.transport.resume_reading,
            disconnect=self._disconnect,
        )

    def on_url(self, url: bytes) -> None:
        self._reading_url_bytes += url

    def on_header(self, name: bytes, value: bytes) -> None:
        assert self._reading_headers is not None
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

        # fmt: off
        request = Request(
            method           = _decode_method(parser.get_method()),
            path             = _decode_path(parsed_url.path),
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
            compress        = self.get_compressor(headers.get(b"accept-encoding")),
        )
        # fmt: on

        if self._active_request is None:
            # There are no active requests on this connection yet
            self._active_request = request
            self._active_writer = writer

            self._spawn_handler(request, writer)

        else:
            # Earlier request has not responded yet, queue it for later
            transport.pause_reading()
            if self._pipeline is None:
                self._pipeline = deque()
            self._pipeline.append((request, writer))

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
        r = self._active_request

        if t is None or w is None or r is None or t.is_closing() or w.disconnected:
            self._active_request = None
            self._active_writer = None
            return

        if (
            w.headers.rget(b"connection") == b"close"
            or not r.keep_alive
            or self._shutdown.done()
        ):
            t.close()
            self._active_request = None
            self._active_writer = None
            return

        # Next pipelined request
        if self._pipeline:
            next_r, next_w = self._pipeline.popleft()
            self._active_writer = next_w
            self._active_request = next_r

            self._spawn_handler(next_r, next_w)
            self._cancel_timeout()
            t.resume_reading()
        else:
            self._active_request = None
            self._active_writer = None
            self._reset_timeout()
            t.resume_reading()

    def _spawn_handler(self, request: Request, writer: Writer) -> None:
        """Create a handler task and register it in the worker's task set."""
        task = self.loop.create_task(self.request_handler(request, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
