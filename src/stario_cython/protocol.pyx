# cython: language_level=3
"""uvloop connection protocol; each request runs in a pooled RequestExchange."""

import asyncio

from libc.stdlib cimport realloc, free
from libc.string cimport memcpy
from cpython.bytes cimport PyBytes_FromStringAndSize

from stario.http.wire import decode_path
from stario.http.writer import get_status_line
from stario.telemetry.noop import NoOpTracer

from stario_cython.exchange cimport RequestExchange, acquire_exchange
from stario_cython.headers cimport Headers
from stario_cython.llhttp cimport *
from stario_cython.request cimport Request

cdef int F_CHUNKED = 0x8
cdef int F_CONTENT_LENGTH = 0x20
# Handlers always start at headers-complete. Body bytes accumulate on the exchange;
# body() awaits one completion Future, stream() drains with backpressure.

cdef object METH_DELETE = "DELETE"
cdef object METH_GET = "GET"
cdef object METH_HEAD = "HEAD"
cdef object METH_POST = "POST"
cdef object METH_PUT = "PUT"
cdef object METH_CONNECT = "CONNECT"
cdef object METH_OPTIONS = "OPTIONS"
cdef object METH_TRACE = "TRACE"
cdef object METH_PATCH = "PATCH"
cdef object VER_10 = "1.0"
cdef object VER_11 = "1.1"

cdef llhttp_settings_t* _SETTINGS = NULL


cdef object _method_str(int method):
    if method == 1:
        return METH_GET
    if method == 3:
        return METH_POST
    if method == 4:
        return METH_PUT
    if method == 0:
        return METH_DELETE
    if method == 2:
        return METH_HEAD
    if method == 6:
        return METH_OPTIONS
    if method == 28:
        return METH_PATCH
    if method == 5:
        return METH_CONNECT
    if method == 7:
        return METH_TRACE
    return llhttp_method_name(method).decode("ascii")


cdef object _version_str(int major, int minor):
    if major == 1 and minor == 1:
        return VER_11
    if major == 1 and minor == 0:
        return VER_10
    return "%d.%d" % (major, minor)


cdef void _bind_settings() noexcept:
    global _SETTINGS
    if _SETTINGS != NULL:
        return
    _SETTINGS = stario_settings_new()
    _SETTINGS.on_message_begin = _cb_message_begin
    _SETTINGS.on_url = _cb_url
    _SETTINGS.on_header_field = _cb_header_field
    _SETTINGS.on_header_value = _cb_header_value
    _SETTINGS.on_headers_complete = _cb_headers_complete
    _SETTINGS.on_body = _cb_body
    _SETTINGS.on_message_complete = _cb_message_complete


cdef HttpProtocol _proto(llhttp_t* parser):
    return <HttpProtocol>stario_parser_get_data(parser)


cdef int _cb_message_begin(llhttp_t* parser) noexcept:
    try:
        _proto(parser)._on_message_begin()
    except Exception:
        return -1
    return 0


cdef int _cb_url(llhttp_t* parser, const char* at, size_t length) noexcept:
    try:
        _proto(parser)._on_url(at, length)
    except Exception:
        return -1
    return 0


cdef int _cb_header_field(llhttp_t* parser, const char* at, size_t length) noexcept:
    try:
        _proto(parser)._on_header_field(at, length)
    except Exception:
        return -1
    return 0


cdef int _cb_header_value(llhttp_t* parser, const char* at, size_t length) noexcept:
    try:
        _proto(parser)._on_header_value(at, length)
    except Exception:
        return -1
    return 0


cdef int _cb_headers_complete(llhttp_t* parser) noexcept:
    try:
        _proto(parser)._on_headers_complete()
    except Exception:
        return -1
    return 0


cdef int _cb_body(llhttp_t* parser, const char* at, size_t length) noexcept:
    try:
        _proto(parser)._on_body(at, length)
    except Exception:
        return -1
    return 0


cdef int _cb_message_complete(llhttp_t* parser) noexcept:
    try:
        _proto(parser)._on_message_complete()
    except Exception:
        return -1
    return 0


cdef int _grow(char** buf, Py_ssize_t* cap, Py_ssize_t need):
    cdef char* p
    cdef Py_ssize_t next_cap
    if need <= cap[0]:
        return 0
    next_cap = 64
    if cap[0] > 0:
        next_cap = cap[0] * 2
    if next_cap < need:
        next_cap = need
    p = <char*>realloc(buf[0], <size_t>next_cap)
    if p == NULL:
        return -1
    buf[0] = p
    cap[0] = next_cap
    return 0


cdef class HttpProtocol:
    cdef llhttp_t* parser
    cdef object loop
    cdef object app
    cdef object tracer
    cdef object noop_span
    cdef list date_box
    cdef object compression
    cdef object connections
    cdef public object transport
    cdef public object disconnect
    cdef public bint closed
    cdef RequestExchange reading_exchange
    cdef RequestExchange active_exchange
    cdef RequestExchange idle_exchange
    cdef object pending_exchanges
    cdef bytes url
    cdef char* name_buf
    cdef char* value_buf
    cdef char* url_buf
    cdef Py_ssize_t name_len
    cdef Py_ssize_t name_cap
    cdef Py_ssize_t value_len
    cdef Py_ssize_t value_cap
    cdef Py_ssize_t url_len
    cdef Py_ssize_t url_cap
    cdef int head_bytes
    cdef int max_header_bytes
    cdef int max_body_bytes
    cdef bint have_value
    cdef bint rejected
    cdef bint request_dispatched
    cdef bint request_keep_alive

    def __cinit__(self):
        _bind_settings()
        self.parser = stario_parser_new()
        if self.parser == NULL:
            raise MemoryError()
        llhttp_init(self.parser, HTTP_REQUEST, _SETTINGS)
        stario_parser_set_data(self.parser, <void*>self)
        self.name_buf = NULL
        self.value_buf = NULL
        self.url_buf = NULL
        self.name_len = 0
        self.name_cap = 0
        self.value_len = 0
        self.value_cap = 0
        self.url_len = 0
        self.url_cap = 0
        self.closed = False
        self.disconnect = None
        self.reading_exchange = None
        self.active_exchange = None
        self.idle_exchange = None

    def __dealloc__(self):
        if self.parser != NULL:
            stario_parser_del(self.parser)
            self.parser = NULL
        if self.name_buf != NULL:
            free(self.name_buf)
            self.name_buf = NULL
        if self.value_buf != NULL:
            free(self.value_buf)
            self.value_buf = NULL
        if self.url_buf != NULL:
            free(self.url_buf)
            self.url_buf = NULL

    def __init__(
        self,
        loop,
        app,
        tracer,
        list date_box,
        compression,
        connections,
        max_header_bytes=64 * 1024,
        max_body_bytes=10 * 1024 * 1024,
    ):
        self.loop = loop
        self.app = app
        self.tracer = tracer
        self.noop_span = (
            tracer.create("request") if isinstance(tracer, NoOpTracer) else None
        )
        self.date_box = date_box
        self.compression = compression
        self.connections = connections
        self.transport = None
        self.pending_exchanges = []
        self.url = b""
        self.head_bytes = 0
        self.max_header_bytes = max_header_bytes
        self.max_body_bytes = max_body_bytes
        self.have_value = False
        self.rejected = False
        self.request_dispatched = False
        self.request_keep_alive = True

    def connection_made(self, transport):
        self.transport = transport
        self.closed = False
        self.disconnect = None
        self.connections.add(self)

    def ensure_disconnect(self):
        if self.disconnect is None:
            self.disconnect = self.loop.create_future()
            if self.closed and not self.disconnect.done():
                self.disconnect.set_result(None)
        return self.disconnect

    def connection_lost(self, exc):
        cdef RequestExchange exchange
        self.closed = True
        self.connections.discard(self)
        if self.idle_exchange is not None:
            self.idle_exchange.release_global()
            self.idle_exchange = None
        if self.reading_exchange is not None:
            self.reading_exchange.c_abort()
        for exchange in self.pending_exchanges:
            exchange.cancel_before_start()
        self.pending_exchanges.clear()
        if (
            self.reading_exchange is not None
            and not self.reading_exchange.handler_started
        ):
            self.reading_exchange.cancel_before_start()
        if self.active_exchange is not None:
            self.active_exchange.c_abort()
        if self.disconnect is not None and not self.disconnect.done():
            self.disconnect.set_result(None)
        self.transport = None

    def recycle_exchange(self, RequestExchange exchange):
        exchange.park()
        if not self.closed and self.idle_exchange is None:
            self.idle_exchange = exchange
        else:
            exchange.release_global()

    def eof_received(self):
        return False

    def data_received(self, data):
        cdef const char* ptr
        cdef Py_ssize_t n
        cdef int err
        if self.rejected or self.parser == NULL:
            return
        n = len(data)
        if n == 0:
            return
        ptr = <const char*>data
        err = llhttp_execute(self.parser, ptr, <size_t>n)
        if err != HPE_OK:
            self._close_error(400, "Invalid HTTP request")

    def pause_writing(self):
        if self.transport is not None and not self.transport.is_closing():
            self.transport.pause_reading()

    def resume_writing(self):
        if self.transport is not None and not self.transport.is_closing():
            self.transport.resume_reading()

    cdef int _append(self, char** buf, Py_ssize_t* length, Py_ssize_t* cap, const char* at, size_t n):
        if _grow(buf, cap, length[0] + <Py_ssize_t>n) != 0:
            return -1
        memcpy(buf[0] + length[0], at, n)
        length[0] += <Py_ssize_t>n
        return 0

    cdef void _flush_header(self):
        cdef Headers headers
        if self.reading_exchange is None or self.name_len == 0:
            self.name_len = 0
            self.value_len = 0
            self.have_value = False
            return
        headers = self.reading_exchange.request_headers
        if self.value_buf == NULL:
            headers.add_raw(self.name_buf, <size_t>self.name_len, "", 0)
        else:
            headers.add_raw(
                self.name_buf,
                <size_t>self.name_len,
                self.value_buf,
                <size_t>self.value_len,
            )
        self.name_len = 0
        self.value_len = 0
        self.have_value = False

    cdef void _on_message_begin(self):
        self.url_len = 0
        self.name_len = 0
        self.value_len = 0
        self.have_value = False
        if self.idle_exchange is not None:
            self.reading_exchange = self.idle_exchange
            self.idle_exchange = None
            self.reading_exchange.reset(
                self,
                self.app,
                self.transport,
                self.date_box,
                self.compression,
                self.max_body_bytes,
            )
        else:
            self.reading_exchange = acquire_exchange(
                self,
                self.app,
                self.transport,
                self.date_box,
                self.compression,
                self.max_body_bytes,
            )
        self.head_bytes = 40
        self.request_dispatched = False
        self.request_keep_alive = True
        self.url = b""

    cdef void _on_url(self, const char* at, size_t length):
        self.head_bytes += <int>length
        if self.head_bytes > self.max_header_bytes:
            self._close_error(431, "Request header fields too large")
            return
        if self._append(&self.url_buf, &self.url_len, &self.url_cap, at, length) != 0:
            self._close_error(431, "Request header fields too large")

    cdef void _on_header_field(self, const char* at, size_t length):
        if self.have_value:
            self._flush_header()
        self.head_bytes += <int>length
        if self.head_bytes > self.max_header_bytes:
            self._close_error(431, "Request header fields too large")
            return
        if self._append(&self.name_buf, &self.name_len, &self.name_cap, at, length) != 0:
            self._close_error(431, "Request header fields too large")

    cdef void _on_header_value(self, const char* at, size_t length):
        self.head_bytes += <int>length
        if self.head_bytes > self.max_header_bytes:
            self._close_error(431, "Request header fields too large")
            return
        if self._append(&self.value_buf, &self.value_len, &self.value_cap, at, length) != 0:
            self._close_error(431, "Request header fields too large")
            return
        self.have_value = True

    cdef void _on_headers_complete(self):
        cdef RequestExchange exchange
        cdef uint16_t flags
        cdef uint64_t content_length
        if self.url_buf != NULL and self.url_len > 0:
            self.url = PyBytes_FromStringAndSize(self.url_buf, self.url_len)
        else:
            self.url = b""
        self._flush_header()
        if self.rejected or self.reading_exchange is None:
            return
        exchange = self.reading_exchange
        if llhttp_get_upgrade(self.parser):
            self._close_error(400, "Upgrade not supported")
            return
        flags = stario_parser_flags(self.parser)
        content_length = stario_parser_content_length(self.parser)
        if flags & F_CONTENT_LENGTH and content_length > <uint64_t>self.max_body_bytes:
            self._close_error(413, "Request body too large")
            return
        # One dict pass into exchange slots; keep-alive / expect / accept-encoding
        # then use those fields (Headers public API unchanged).
        exchange.cache_hot_request_headers()
        self.request_keep_alive = (
            llhttp_should_keep_alive(self.parser) != 0
            or (
                llhttp_get_http_major(self.parser) == 1
                and llhttp_get_http_minor(self.parser) == 1
                and not exchange._req_connection_close
            )
        )
        # Always start the handler at headers-complete. Pre-size _acc when the
        # Content-Length is known so body() can materialize without realloc churn.
        if flags & F_CONTENT_LENGTH:
            self.complete_headers(
                exchange._req_expect_continue, <Py_ssize_t>content_length
            )
        else:
            self.complete_headers(exchange._req_expect_continue, -1)

    cdef void _on_body(self, const char* at, size_t length):
        self.reading_exchange.c_feed(at, length)

    cdef void _on_message_complete(self):
        cdef RequestExchange exchange
        if self.rejected:
            return
        exchange = self.reading_exchange
        if exchange is not None and exchange._body_active:
            exchange.c_complete()
        self.reading_exchange = None

    cdef Request _build_request(self, RequestExchange exchange, object body):
        cdef object path_bytes
        cdef object query
        cdef object method
        cdef object version
        cdef Request request = exchange.req
        cdef object url = self.url
        cdef Py_ssize_t q = url.find(b"?")
        if q == -1:
            path_bytes, query = url, b""
        else:
            path_bytes, query = url[:q], url[q + 1 :]
        method = _method_str(<int>llhttp_get_method(self.parser))
        version = _version_str(
            <int>llhttp_get_http_major(self.parser),
            <int>llhttp_get_http_minor(self.parser),
        )
        request.reset(
            method,
            decode_path(path_bytes),
            query,
            version,
            self.request_keep_alive,
            exchange.request_headers,
            body,
        )
        return request

    cdef void complete_headers(self, bint expect_continue, Py_ssize_t expected_body):
        cdef RequestExchange exchange
        cdef Request request
        if self.request_dispatched or self.rejected:
            return
        if self.transport is None or self.transport.is_closing():
            return
        exchange = self.reading_exchange
        request = self._build_request(exchange, exchange)
        exchange.reset_body(expect_continue)
        if expected_body > 0:
            exchange.prepare_body_capacity(expected_body)
        self._dispatch(exchange, request)

    cdef void _dispatch(self, RequestExchange exchange, Request request):
        cdef object span
        self.request_dispatched = True
        if self.noop_span is not None:
            span = self.noop_span
        else:
            span = self.tracer.create("request") if self.tracer is not None else None
        exchange.span = span
        if self.active_exchange is None:
            self._start_exchange(exchange, True)
        else:
            self.pending_exchanges.append(exchange)
            if self.transport is not None:
                self.transport.pause_reading()

    cdef void _start_exchange(self, RequestExchange exchange, bint eager):
        cdef object task
        self.active_exchange = exchange
        exchange.start_response()
        if eager:
            self.app.create_task(self._run(exchange), loop=self.loop)
            return
        task = asyncio.Task(self._run(exchange), loop=self.loop)
        self.app.tasks.add(task)
        task.add_done_callback(self.app.tasks.discard)

    async def _run(self, RequestExchange exchange):
        try:
            await self.app(exchange, exchange)
        finally:
            exchange.handler_finished()

    cdef void _drop_pending(self):
        cdef RequestExchange exchange
        for exchange in self.pending_exchanges:
            exchange.cancel_before_start()
        self.pending_exchanges.clear()

    def response_completed(self, RequestExchange exchange):
        """Advance the connection after the response is fully sent.

        Fired from ``respond()`` / ``end()`` / ``abort()`` via ``_done`` — not when
        the handler coroutine returns. The handler (or ``app.create_task`` work)
        may still run after this; the connection is free to start the next
        pipelined/keep-alive exchange immediately.
        """
        cdef object transport = self.transport
        cdef object conn
        cdef RequestExchange next_exchange
        if self.active_exchange is not exchange:
            return
        self.active_exchange = None
        if transport is None or transport.is_closing():
            self._drop_pending()
            return
        if exchange._resp_connection_close:
            transport.close()
            self._drop_pending()
            return
        # User may have set Connection: close on the response Headers dict.
        conn = exchange.headers.c_get(b"connection")
        if conn is not None and conn.lower() == b"close":
            transport.close()
            self._drop_pending()
            return
        if not exchange.req.keep_alive or self.app.shutdown.done():
            transport.close()
            self._drop_pending()
            return
        if self.pending_exchanges:
            next_exchange = self.pending_exchanges.pop(0)
            self._start_exchange(next_exchange, False)
            return
        transport.resume_reading()

    cdef void _close_error(self, int status, object message):
        cdef object transport
        cdef object body
        cdef object date
        if self.rejected:
            return
        self.rejected = True
        transport = self.transport
        if transport is None or transport.is_closing():
            return
        if self.reading_exchange is not None:
            self.reading_exchange.c_abort()
            if not self.reading_exchange.handler_started:
                self.reading_exchange.cancel_before_start()
        self._drop_pending()
        body = message.encode("utf-8")
        date = self.date_box[0]
        transport.write(
            b"".join((
                get_status_line(status),
                date,
                b"content-type: text/plain; charset=utf-8\r\n",
                b"content-length: %d\r\n" % len(body),
                b"connection: close\r\n",
                b"\r\n",
                body,
            ))
        )
        transport.close()
