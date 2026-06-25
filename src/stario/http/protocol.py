"""
Per-connection HTTP/1.1: httptools parses bytes; handlers run as tasks so this layer never blocks the event loop.

Shared `disconnect` futures tie body reads and long responses to the same socket lifetime. Pipelining and keep-alive
follow RFC behavior even when common clients use parallel connections instead. App work is scheduled via `app.create_task`
so shutdown can observe the same task set the app registered.
"""

import asyncio
from collections import deque
from collections.abc import Callable
from typing import Literal, cast

from httptools import (  # pyright: ignore[reportMissingTypeStubs]
    HttpParserError,
    HttpParserUpgrade,
    HttpRequestParser,
    parse_url,  # pyright: ignore[reportUnknownVariableType]
)

from stario.telemetry.core import Tracer

from .app import App
from .compression import CompressionConfig
from .config import RequestPolicy
from .context import Context
from .headers import Headers
from .request import BodyReader, Request
from .wire import decode_method, decode_path
from .writer import (
    Writer,
    get_status_line,
)


class HttpProtocol(asyncio.Protocol):
    """
    `asyncio.Protocol` for one TCP (or Unix) connection: parse requests with httptools, run `app` per message, pipeline safely.

    Pipelined requests queue until the prior response finishes; `BodyReader` and `Writer` share one disconnect future per connection.
    """

    __slots__ = (  # type: ignore[assignment]
        "_active_context",
        "_active_task",
        "_active_writer",
        "_compression",
        "_connections",
        "_disconnect",
        "_pipeline",
        "_reading_body",
        "_reading_headers",
        "_reading_url_parts",
        "_rejected",
        "_request_head_bytes",
        "_request_policy",
        "_timeout_kind",
        "app",
        "get_date_header",
        "loop",
        "parser",
        "timeout_handle",
        "tracer",
        "transport",
    )

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        app: App,
        tracer: Tracer,
        get_date_header: Callable[[], bytes],
        compression: CompressionConfig,
        connections: set[HttpProtocol],
        request_policy: RequestPolicy,
    ) -> None:
        self.loop = loop
        self.app = app
        self.tracer = tracer
        self.get_date_header = get_date_header
        self._request_policy = request_policy
        self._compression = compression
        self._connections = connections
        self._request_head_bytes = 0
        self._timeout_kind: Literal["header", "idle"] | None = None
        self._rejected = False

        self.parser: HttpRequestParser | None = HttpRequestParser(self)
        self.transport: asyncio.Transport | None = None
        self.timeout_handle: asyncio.TimerHandle | None = None

        # State of the request currently being read from the transport
        self._reading_headers: Headers | None = None
        self._reading_body: BodyReader | None = None
        self._reading_url_parts: list[bytes] = []

        # State of the request currently being handled by the application
        self._active_context: Context | None = None
        self._active_writer: Writer | None = None
        self._active_task: asyncio.Task[None] | None = None

        # One Future per TCP connection: BodyReader, Writer, and SSE loops all await this.
        self._disconnect = loop.create_future()

        # Lazy: only allocated when a second full request parses while the first response is still in flight.
        self._pipeline: deque[tuple[Context, Writer]] | None = None

    # =========================================================================
    # Timeout
    # =========================================================================

    def _reset_timeout(
        self,
        kind: Literal["header", "idle"],
        seconds: float,
    ) -> None:
        transport = self.transport
        if transport is None or transport.is_closing():
            return
        self._cancel_timeout()
        self._timeout_kind = kind
        self.timeout_handle = self.loop.call_later(
            seconds,
            lambda: not transport.is_closing() and transport.close(),
        )

    def _cancel_timeout(self) -> None:
        if self.timeout_handle is not None:
            self.timeout_handle.cancel()
            self.timeout_handle = None
        self._timeout_kind = None

    # =========================================================================
    # asyncio.Protocol
    # =========================================================================

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.Transport, transport)
        self._connections.add(self)
        self._reset_timeout("header", self._request_policy.header_timeout)

    def close_if_idle(self) -> bool:
        """Close a connection with no request parsing or handler work in progress."""
        if (
            self._active_context is not None
            or self._active_writer is not None
            or self._active_task is not None
            or self._reading_headers is not None
            or self._reading_body is not None
            or self._reading_url_parts
            or self._pipeline
        ):
            return False
        transport = self.transport
        if transport is None or transport.is_closing():
            return False
        transport.close()
        self._active_context = None
        self._active_writer = None
        self._active_task = None
        return True

    def connection_lost(self, exc: Exception | None) -> None:
        self._cancel_timeout()
        self._connections.discard(self)

        self._abort_pending_work()

        self.transport = None
        self.parser = None

    def eof_received(self) -> None:
        pass

    def data_received(self, data: bytes) -> None:
        if self._rejected:
            return
        parser = self.parser
        if parser is None:
            return
        if self._timeout_kind == "idle":
            self._cancel_timeout()

        try:
            parser.feed_data(data)  # pyright: ignore[reportUnknownMemberType]
        except HttpParserError:
            self._close_with_error(400, "Invalid HTTP request")
        except HttpParserUpgrade:
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
        # Reserve bytes for `METHOD SP` and ` HTTP/x.x\r\n` (URL fragments arrive via `on_url`).
        self._request_head_bytes = 40
        self._reading_headers = Headers()
        self._reading_body = None
        self._reading_url_parts = []
        self._reset_timeout("header", self._request_policy.header_timeout)

    def on_url(self, url: bytes) -> None:
        if self._rejected:
            return
        self._request_head_bytes += len(url)
        if self._request_head_bytes > self._request_policy.max_header_bytes:
            self._close_with_error(431, "Request header fields too large")
            return
        self._reading_url_parts.append(url)

    def on_header(self, name: bytes, value: bytes) -> None:
        if self._rejected:
            return
        headers = self._reading_headers
        if headers is None:
            return
        self._request_head_bytes += len(name) + len(value)
        if self._request_head_bytes > self._request_policy.max_header_bytes:
            self._close_with_error(431, "Request header fields too large")
            return

        # httptools rejects invalid names/values before this runs.
        headers.unsafe_add(name.lower(), value)

    def on_headers_complete(self) -> None:
        if self._rejected:
            return
        self._cancel_timeout()
        parser = self.parser
        transport = self.transport
        headers = self._reading_headers

        if (
            parser is None
            or transport is None
            or transport.is_closing()
            or headers is None
        ):
            return

        url = b"".join(self._reading_url_parts)
        parsed_url = parse_url(url)
        if not self._validate_request_framing(headers):
            return

        body_reader = self._reading_body
        content_length = headers.unsafe_get(b"content-length")
        policy = self._request_policy
        if content_length not in (None, b"0"):
            try:
                declared_length = int(content_length)
            except ValueError:
                self._close_with_error(400, "Invalid Content-Length")
                return
            if declared_length < 0:
                self._close_with_error(400, "Invalid Content-Length")
                return
            if declared_length > policy.max_body_bytes:
                self._close_with_error(413, "Request body too large")
                return

        if (
            content_length not in (None, b"0")
            or headers.unsafe_get(b"transfer-encoding") is not None
        ):
            send_100_continue: Callable[[], None] | None = None
            if headers.unsafe_get(b"expect", b"").lower() == b"100-continue":

                def _send_100_continue() -> None:
                    if transport and not transport.is_closing():
                        transport.write(b"HTTP/1.1 100 Continue\r\n\r\n")

                send_100_continue = _send_100_continue

            body_reader = BodyReader(
                pause=transport.pause_reading,
                resume=transport.resume_reading,
                disconnect=self._disconnect,
                max_size=policy.max_body_bytes,
                timeout=policy.body_timeout,
                send_100_continue=send_100_continue,
            )
            self._reading_body = body_reader

        # Hand off to the app: one Request + Writer per message; handler runs in a task (see on_response_completed).
        try:
            method = decode_method(parser.get_method())
            path_str = decode_path(parsed_url.path)
        except UnicodeDecodeError, ValueError:
            self._close_with_error(400, "Invalid request")
            return

        # fmt: off
        request = Request(
            method           = method,
            path             = path_str,
            query_bytes      = parsed_url.query or b"",
            protocol_version = parser.get_http_version(),
            keep_alive       = parser.should_keep_alive(),
            headers          = headers,
            body             = body_reader,
        )

        context = Context(
            app         = self.app,
            req         = request,
            span        = self.tracer.create(request.method),
            _disconnect = self._disconnect,
        )

        writer = Writer(
            transport       = transport,
            get_date_header = self.get_date_header,
            on_completed    = self.on_response_completed,
            compression     = self._compression,
            accept_encoding = headers.unsafe_get(b"accept-encoding"),
        )
        # fmt: on

        if self._active_context is None:
            self._active_context = context
            self._active_writer = writer

            # Reuse the protocol loop here; this is one task per request.
            self._active_task = self.app.create_task(
                self.app(context, writer), loop=self.loop
            )

        else:
            # Pipeline queue: must not run the next handler until bytes are fully written.
            transport.pause_reading()
            if self._pipeline is None:
                self._pipeline = deque()
            if len(self._pipeline) >= self._request_policy.max_pipelined_requests:
                self._close_with_error(503, "Pipeline queue full")
                return
            self._pipeline.append((context, writer))

    def on_body(self, body: bytes) -> None:
        if self._rejected:
            return
        if self._reading_body:
            self._reading_body.feed(body)

    def on_message_complete(self) -> None:
        if self._rejected:
            return
        if self._reading_body:
            self._reading_body.complete()

        self._reading_body = None
        self._reading_headers = None
        self._reading_url_parts = []

    # =========================================================================
    # Request Handling
    # =========================================================================

    def on_response_completed(self) -> None:
        t = self.transport
        w = self._active_writer
        c = self._active_context

        if t is None or w is None or c is None or t.is_closing():
            self._active_context = None
            self._active_writer = None
            self._active_task = None
            return

        if (
            w.headers.unsafe_get(b"connection", b"").lower() == b"close"
            or not c.req.keep_alive
            or self.app.shutdown.done()
        ):
            t.close()
            self._active_context = None
            self._active_writer = None
            self._active_task = None
            return

        # FIFO: pipeline order matches arrival order; each handler still runs only after prior body is written.
        if self._pipeline:
            next_c, next_w = self._pipeline.popleft()
            self._active_writer = next_w
            self._active_context = next_c
            self._active_task = self.app.create_task(
                self.app(next_c, next_w), loop=self.loop
            )
            t.resume_reading()
        else:
            self._active_context = None
            self._active_writer = None
            self._active_task = None
            self._reset_timeout("idle", self._request_policy.keep_alive_timeout)
            t.resume_reading()

    # =========================================================================
    # Errors
    # =========================================================================

    def _validate_request_framing(self, headers: Headers) -> bool:
        """Reject ambiguous body framing before a handler runs."""
        cl_values = headers.unsafe_getlist(b"content-length")
        if len(cl_values) > 1:
            self._close_with_error(400, "Invalid Content-Length")
            return False
        if len(cl_values) == 1:
            try:
                int(cl_values[0])
            except ValueError:
                self._close_with_error(400, "Invalid Content-Length")
                return False

        te_values = headers.unsafe_getlist(b"transfer-encoding")
        if te_values:
            if len(te_values) > 1 or te_values[0].strip().lower() != b"chunked":
                self._close_with_error(400, "Unsupported Transfer-Encoding")
                return False
            if cl_values:
                self._close_with_error(400, "Invalid message framing")
                return False
        return True

    def _abort_pending_work(self) -> None:
        """Stop reading and wake handlers; do not cancel in-flight handler tasks.

        One body upload is parsed at a time (`_reading_body`); abort it so blocked
        `req.body()` / `req.stream()` calls exit. Completing the disconnect future
        lets handlers observe `Context.disconnect` / `app.shutdown` (via `c.alive()`)
        and finish cleanup — same model as Go's `net/http` after the client goes away.
        """
        if self._reading_body is not None:
            self._reading_body.abort()

        if not self._disconnect.done():
            self._disconnect.set_result(None)

        if self._pipeline:
            self._pipeline.clear()

        # Drop protocol-side tracking; the `app.create_task` work keeps running.
        self._active_task = None
        self._active_context = None
        self._active_writer = None

        self._reading_body = None
        self._reading_headers = None
        self._reading_url_parts = []

    def _close_with_error(self, status_code: int, message: str) -> None:
        transport = self.transport
        if transport is None or transport.is_closing() or self._rejected:
            return

        self._rejected = True
        self._cancel_timeout()
        self._abort_pending_work()
        body = message.encode("utf-8")
        parts = [
            get_status_line(status_code),
            self.get_date_header(),
            b"content-type: text/plain; charset=utf-8\r\n",
            b"content-length: %d\r\n" % len(body),
            b"connection: close\r\n",
            b"\r\n",
            body,
        ]
        transport.write(b"".join(parts))
        transport.close()
