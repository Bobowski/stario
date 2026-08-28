# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True
"""asyncio Protocol + llhttp: one TCP connection, pipelining, and dispatch.

``HttpProtocol`` owns parser callbacks, pause/resume, and request dispatch.
URL bytes and header fragments are written into the current exchange arena.
Path/query decoding is cached here because it is connection-parse work.
"""

from collections import deque

from libc.stdint cimport uint16_t, uint32_t, uint64_t
from libc.stdlib cimport free, malloc
from libc.string cimport memcmp, memcpy
from cpython.bytes cimport PyBytes_FromStringAndSize
from cpython.unicode cimport PyUnicode_DecodeASCII

from stario.http.config import (
    DEFAULT_HEADER_TIMEOUT,
    DEFAULT_KEEP_ALIVE_TIMEOUT,
    DEFAULT_MAX_PIPELINED_REQUESTS,
)
from stario.http.request import DEFAULT_BODY_TIMEOUT
from stario.http.wire import decode_path
from stario.telemetry.noop import NoOpTracer

from stario_cython.exchange cimport Request, RequestExchange, acquire_exchange, _status_line
from stario_cython.llhttp cimport *

cdef enum:
    URL_CACHE_CAP = 256
    URL_CACHE_MAX_KEY = 512
    SMALL_BODY_COMPLETE_DISPATCH = 64 * 1024

cdef char* _UC_KEY[256]
cdef Py_ssize_t _UC_LEN[256]
cdef uint32_t _UC_HASH[256]
cdef list _UC_PATH = None
cdef list _UC_QUERY = None
cdef object Q_EMPTY = b""
cdef object PATH_EMPTY = ""


cdef uint32_t _url_hash(const char* s, Py_ssize_t n) noexcept:
    cdef uint32_t value = <uint32_t>2166136261
    cdef Py_ssize_t i
    for i in range(n):
        value = (value ^ <unsigned char>s[i]) * <uint32_t>16777619
    return value


cdef void _url_cache_init():
    global _UC_PATH, _UC_QUERY
    cdef int i
    if _UC_PATH is not None:
        return
    _UC_PATH = [None] * URL_CACHE_CAP
    _UC_QUERY = [None] * URL_CACHE_CAP
    for i in range(URL_CACHE_CAP):
        _UC_KEY[i] = NULL
        _UC_LEN[i] = 0
        _UC_HASH[i] = 0


cdef void _url_cache_clear():
    cdef int i
    for i in range(URL_CACHE_CAP):
        if _UC_KEY[i] != NULL:
            free(_UC_KEY[i])
            _UC_KEY[i] = NULL
        _UC_LEN[i] = 0
        _UC_HASH[i] = 0
        _UC_PATH[i] = None
        _UC_QUERY[i] = None


cdef int _url_find(const char* url, Py_ssize_t n, uint32_t h) noexcept:
    cdef int slot = <int>(h & (URL_CACHE_CAP - 1))
    cdef int i
    for i in range(URL_CACHE_CAP):
        if _UC_KEY[slot] == NULL:
            return -1
        if (
            _UC_HASH[slot] == h
            and _UC_LEN[slot] == n
            and memcmp(_UC_KEY[slot], url, <size_t>n) == 0
        ):
            return slot
        slot = (slot + 1) & (URL_CACHE_CAP - 1)
    return -1


cdef void _url_store(
    const char* url,
    Py_ssize_t n,
    uint32_t h,
    object path,
    object query,
):
    cdef int slot
    cdef int i
    cdef char* copy
    slot = <int>(h & (URL_CACHE_CAP - 1))
    for i in range(URL_CACHE_CAP):
        if _UC_KEY[slot] == NULL:
            copy = <char*>malloc(<size_t>n if n > 0 else 1)
            if copy == NULL:
                return
            if n:
                memcpy(copy, url, <size_t>n)
            _UC_KEY[slot] = copy
            _UC_LEN[slot] = n
            _UC_HASH[slot] = h
            _UC_PATH[slot] = path
            _UC_QUERY[slot] = query
            return
        slot = (slot + 1) & (URL_CACHE_CAP - 1)
    _url_cache_clear()
    slot = <int>(h & (URL_CACHE_CAP - 1))
    copy = <char*>malloc(<size_t>n if n > 0 else 1)
    if copy == NULL:
        return
    if n:
        memcpy(copy, url, <size_t>n)
    _UC_KEY[slot] = copy
    _UC_LEN[slot] = n
    _UC_HASH[slot] = h
    _UC_PATH[slot] = path
    _UC_QUERY[slot] = query


cdef object _decode_path_n(const char* s, Py_ssize_t n):
    cdef Py_ssize_t i
    cdef unsigned char c
    if n <= 0:
        return PATH_EMPTY
    for i in range(n):
        c = <unsigned char>s[i]
        if c == 37 or c >= 128:
            return decode_path(PyBytes_FromStringAndSize(s, n))
    return PyUnicode_DecodeASCII(s, n, NULL)


cdef tuple _split_request_target_n(const char* url, Py_ssize_t n):
    cdef Py_ssize_t question = -1
    cdef Py_ssize_t i
    cdef uint32_t h = 0
    cdef int slot
    cdef object path
    cdef object query
    if n <= 0:
        return (PATH_EMPTY, Q_EMPTY)
    if n <= URL_CACHE_MAX_KEY:
        h = _url_hash(url, n)
        slot = _url_find(url, n, h)
        if slot >= 0:
            return (_UC_PATH[slot], _UC_QUERY[slot])
    for i in range(n):
        if url[i] == 63:
            question = i
            break
    if question < 0:
        path = _decode_path_n(url, n)
        query = Q_EMPTY
    else:
        path = _decode_path_n(url, question)
        if question + 1 < n:
            query = PyBytes_FromStringAndSize(url + question + 1, n - question - 1)
        else:
            query = Q_EMPTY
    if n <= URL_CACHE_MAX_KEY:
        _url_store(url, n, h, path, query)
    return (path, query)

cdef int F_CONTENT_LENGTH = 0x20
cdef int PAUSE_WRITE = 1
cdef int PAUSE_PIPELINE = 2
cdef int PAUSE_BODY = 4
cdef int PARSER_QUANTUM = 512 * 1024
cdef int TIMEOUT_NONE = 0
cdef int TIMEOUT_HEADER = 1
cdef int TIMEOUT_IDLE = 2
# GET and large/chunked bodies dispatch at headers-complete. Small
# Content-Length bodies dispatch at message-complete so body() is cached.

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



cdef void _bind_settings():
    global _SETTINGS
    if _SETTINGS != NULL:
        return
    _url_cache_init()
    _SETTINGS = stario_settings_new()
    _SETTINGS.on_message_begin = _cb_message_begin
    _SETTINGS.on_url = _cb_url
    _SETTINGS.on_header_field = _cb_header_field
    _SETTINGS.on_header_value = _cb_header_value
    _SETTINGS.on_header_value_complete = _cb_header_value_complete
    _SETTINGS.on_headers_complete = _cb_headers_complete
    _SETTINGS.on_body = _cb_body
    _SETTINGS.on_message_complete = _cb_message_complete


cdef int _cb_message_begin(llhttp_t* parser) noexcept:
    cdef HttpProtocol proto = <HttpProtocol>stario_parser_get_data(parser)
    proto._on_message_begin()
    return -1 if proto.rejected else 0


cdef int _cb_url(llhttp_t* parser, const char* at, size_t length) noexcept:
    cdef HttpProtocol proto = <HttpProtocol>stario_parser_get_data(parser)
    proto._on_url(at, length)
    return -1 if proto.rejected else 0


cdef int _cb_header_field(llhttp_t* parser, const char* at, size_t length) noexcept:
    cdef HttpProtocol proto = <HttpProtocol>stario_parser_get_data(parser)
    proto._on_header_field(at, length)
    return -1 if proto.rejected else 0


cdef int _cb_header_value(llhttp_t* parser, const char* at, size_t length) noexcept:
    cdef HttpProtocol proto = <HttpProtocol>stario_parser_get_data(parser)
    proto._on_header_value(at, length)
    return -1 if proto.rejected else 0


cdef int _cb_header_value_complete(llhttp_t* parser) noexcept:
    cdef HttpProtocol proto = <HttpProtocol>stario_parser_get_data(parser)
    proto._on_header_value_complete()
    return -1 if proto.rejected else 0


cdef int _cb_headers_complete(llhttp_t* parser) noexcept:
    cdef HttpProtocol proto = <HttpProtocol>stario_parser_get_data(parser)
    proto._on_headers_complete()
    return -1 if proto.rejected else 0


cdef int _cb_body(llhttp_t* parser, const char* at, size_t length) noexcept:
    cdef HttpProtocol proto = <HttpProtocol>stario_parser_get_data(parser)
    proto._on_body(at, length)
    return -1 if proto.rejected else 0


cdef int _cb_message_complete(llhttp_t* parser) noexcept:
    cdef HttpProtocol proto = <HttpProtocol>stario_parser_get_data(parser)
    proto._on_message_complete()
    return -1 if proto.rejected else 0


cdef class HttpProtocol:
    cdef llhttp_t* parser
    cdef public object loop
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
    cdef int head_bytes
    cdef int max_header_bytes
    cdef int max_body_bytes
    cdef int max_pipelined_requests
    cdef double header_timeout
    cdef double keep_alive_timeout
    cdef double body_timeout
    cdef int timeout_kind
    cdef double timeout_deadline
    cdef double timeout_handle_when
    cdef object timeout_handle
    cdef bint header_timeout_reset
    cdef bint rejected
    cdef bint request_dispatched
    cdef bint request_keep_alive
    cdef int pause_reasons
    cdef object body_pause_owner
    cdef object held_data
    cdef Py_ssize_t held_offset
    cdef bint pump_scheduled
    cdef object _create_task

    def __cinit__(self):
        _bind_settings()
        self.parser = stario_parser_new()
        if self.parser == NULL:
            raise MemoryError()
        llhttp_init(self.parser, HTTP_REQUEST, _SETTINGS)
        stario_parser_set_data(self.parser, <void*>self)
        self.closed = False
        self.disconnect = None
        self.reading_exchange = None
        self.active_exchange = None
        self.idle_exchange = None
        self.pause_reasons = 0
        self.body_pause_owner = None
        self.held_data = None
        self.held_offset = 0
        self.pump_scheduled = False
        self.timeout_kind = TIMEOUT_NONE
        self.timeout_deadline = 0.0
        self.timeout_handle_when = 0.0
        self.timeout_handle = None
        self.header_timeout_reset = False

    def __dealloc__(self):
        if self.parser != NULL:
            stario_parser_del(self.parser)
            self.parser = NULL

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
        header_timeout=DEFAULT_HEADER_TIMEOUT,
        keep_alive_timeout=DEFAULT_KEEP_ALIVE_TIMEOUT,
        body_timeout=DEFAULT_BODY_TIMEOUT,
        max_pipelined_requests=DEFAULT_MAX_PIPELINED_REQUESTS,
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
        self._create_task = app.create_task
        self.transport = None
        self.pending_exchanges = deque()
        self.head_bytes = 0
        self.max_header_bytes = max_header_bytes
        self.max_body_bytes = max_body_bytes
        self.max_pipelined_requests = max_pipelined_requests
        self.header_timeout = header_timeout
        self.keep_alive_timeout = keep_alive_timeout
        self.body_timeout = body_timeout
        self.rejected = False
        self.request_dispatched = False
        self.request_keep_alive = True
        self.timeout_kind = TIMEOUT_NONE
        self.timeout_deadline = 0.0
        self.timeout_handle_when = 0.0
        self.timeout_handle = None
        self.header_timeout_reset = False

    def connection_made(self, transport):
        self.transport = transport
        self.closed = False
        self.disconnect = None
        self.pause_reasons = 0
        self.body_pause_owner = None
        self.held_data = None
        self.held_offset = 0
        self.pump_scheduled = False
        self.rejected = False
        self.timeout_kind = TIMEOUT_NONE
        self.timeout_deadline = 0.0
        self.connections.add(self)
        # First request: header timer only. wrk keep-alive must not re-arm
        # a TimerHandle per request (see _arm_timeout).
        self._arm_timeout(TIMEOUT_HEADER, self.header_timeout)

    def ensure_disconnect(self):
        if self.disconnect is None:
            self.disconnect = self.loop.create_future()
            if self.closed and not self.disconnect.done():
                self.disconnect.set_result(None)
        return self.disconnect

    def connection_lost(self, exc):
        cdef RequestExchange exchange
        self.closed = True
        self._cancel_timeout()
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
        self.pause_reasons = 0
        self.body_pause_owner = None
        self.held_data = None
        self.held_offset = 0
        self.pump_scheduled = False
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
        if self.rejected or self.parser == NULL or not data:
            return
        if self.timeout_kind == TIMEOUT_IDLE:
            # Keep-alive traffic: drop the idle deadline without cancelling
            # the TimerHandle. The watchdog reschedules or stops when it fires.
            self.timeout_kind = TIMEOUT_NONE
            self.timeout_deadline = 0.0
        if self.held_data is not None:
            self.held_data = self.held_data[self.held_offset :] + data
            self.held_offset = 0
            return
        if self.pause_reasons:
            self.held_data = data
            self.held_offset = 0
            return
        self._pump_data(data, 0)
        self._after_pump()

    cdef void _arm_timeout(self, int kind, double seconds):
        """Arm header or idle deadline. Reuses one TimerHandle per connection.

        wrk keep-alive would die if we ``call_later`` per request. Store a
        deadline (cheap) and only allocate a handle when none exists, or when
        the new deadline is *sooner* than the current fire time (slowloris
        tests with 10ms headers after a 5s idle handle).
        """
        cdef object loop
        cdef object transport
        cdef double now
        cdef double deadline
        transport = self.transport
        if transport is None or transport.is_closing() or seconds <= 0:
            return
        loop = self.loop
        now = loop.time()
        deadline = now + seconds
        self.timeout_kind = kind
        self.timeout_deadline = deadline
        if self.timeout_handle is None:
            self.timeout_handle = loop.call_later(seconds, self._on_timeout)
            self.timeout_handle_when = deadline
        elif deadline < self.timeout_handle_when:
            self.timeout_handle.cancel()
            self.timeout_handle = loop.call_later(seconds, self._on_timeout)
            self.timeout_handle_when = deadline

    cdef void _cancel_timeout(self):
        cdef object handle
        self.timeout_kind = TIMEOUT_NONE
        self.timeout_deadline = 0.0
        self.timeout_handle_when = 0.0
        handle = self.timeout_handle
        if handle is not None:
            handle.cancel()
            self.timeout_handle = None

    def _on_timeout(self):
        cdef object transport
        cdef object loop
        cdef double now
        cdef double deadline
        self.timeout_handle = None
        self.timeout_handle_when = 0.0
        if self.rejected:
            return
        transport = self.transport
        if transport is None or transport.is_closing():
            return
        deadline = self.timeout_deadline
        if self.timeout_kind == TIMEOUT_NONE or deadline <= 0:
            return
        loop = self.loop
        now = loop.time()
        if now < deadline:
            self.timeout_handle = loop.call_later(deadline - now, self._on_timeout)
            self.timeout_handle_when = deadline
            return
        self.timeout_kind = TIMEOUT_NONE
        self.timeout_deadline = 0.0
        transport.close()

    cdef void _after_pump(self):
        """Arm a header timer only if this read left headers (or a deferred
        small body) unfinished. Full wrk requests dispatch inside the pump,
        so this is a no-op on the keep-alive hot path.
        """
        if not self.header_timeout_reset:
            return
        self.header_timeout_reset = False
        if (
            not self.rejected
            and self.reading_exchange is not None
            and not self.request_dispatched
        ):
            self._arm_timeout(TIMEOUT_HEADER, self.header_timeout)

    cdef void _pump_data(self, object data, Py_ssize_t offset) noexcept:
        cdef const char* ptr
        cdef Py_ssize_t n
        cdef Py_ssize_t end
        cdef int err
        n = len(data)
        ptr = <const char*>data
        if n <= PARSER_QUANTUM:
            err = llhttp_execute(self.parser, ptr + offset, <size_t>(n - offset))
            if err != HPE_OK:
                self._close_error(400, "Invalid HTTP request")
            return
        while offset < n:
            end = offset + PARSER_QUANTUM
            if end > n:
                end = n
            err = llhttp_execute(
                self.parser,
                ptr + offset,
                <size_t>(end - offset),
            )
            if err != HPE_OK:
                self._close_error(400, "Invalid HTTP request")
                return
            offset = end
            if self.pause_reasons:
                if offset < n:
                    self.held_data = data
                    self.held_offset = offset
                return

    cdef void _set_pause_reason(self, int reason, bint paused):
        cdef int previous = self.pause_reasons
        cdef object transport = self.transport
        if paused:
            self.pause_reasons |= reason
        else:
            self.pause_reasons &= ~reason
        if previous == self.pause_reasons:
            return
        if previous == 0:
            if transport is not None and not transport.is_closing():
                transport.pause_reading()
            return
        if self.pause_reasons != 0:
            return
        if self.held_data is not None:
            if not self.pump_scheduled:
                self.pump_scheduled = True
                self.loop.call_soon(self._resume_held_input)
            return
        if transport is not None and not transport.is_closing():
            transport.resume_reading()

    def _resume_held_input(self):
        cdef object data
        cdef Py_ssize_t offset
        cdef object transport
        self.pump_scheduled = False
        if self.pause_reasons or self.held_data is None:
            return
        data = self.held_data
        offset = self.held_offset
        self.held_data = None
        self.held_offset = 0
        self._pump_data(data, offset)
        self._after_pump()
        if self.pause_reasons == 0 and self.held_data is None:
            transport = self.transport
            if transport is not None and not transport.is_closing():
                transport.resume_reading()

    def set_body_paused(self, exchange, paused):
        if paused:
            self.body_pause_owner = exchange
            self._set_pause_reason(PAUSE_BODY, True)
        elif self.body_pause_owner is exchange:
            self.body_pause_owner = None
            self._set_pause_reason(PAUSE_BODY, False)

    def close_if_idle(self) -> bool:
        """Close the connection if it is waiting for a new request.

        Matches ``stario.http.protocol.HttpProtocol.close_if_idle`` so the
        shared ``Server`` drain path can reuse Cython connections.
        """
        cdef object transport
        if (
            self.active_exchange is not None
            or self.reading_exchange is not None
            or self.pending_exchanges
            or self.held_data is not None
            or self.rejected
        ):
            return False
        transport = self.transport
        if transport is None or transport.is_closing():
            return False
        transport.close()
        self._cancel_timeout()
        return True

    def pause_writing(self):
        self._set_pause_reason(PAUSE_WRITE, True)

    def resume_writing(self):
        self._set_pause_reason(PAUSE_WRITE, False)

    cdef bint _header_too_large(self, size_t length) noexcept:
        self.head_bytes += <int>length
        if self.head_bytes > self.max_header_bytes:
            self._close_error(431, "Request header fields too large")
            return True
        return False

    cdef void _on_message_begin(self) noexcept:
        cdef RequestExchange exchange
        if self.rejected:
            return
        exchange = self.idle_exchange
        if exchange is not None:
            self.idle_exchange = None
            self.reading_exchange = exchange
            exchange.reset(
                self,
                self.app,
                self.transport,
                self.date_box,
                self.compression,
                self.max_body_bytes,
                self.body_timeout,
            )
        else:
            # Pool miss: constructing an exchange can raise. Keepalive reuse
            # above is noexcept.
            try:
                self.reading_exchange = acquire_exchange(
                    self,
                    self.app,
                    self.transport,
                    self.date_box,
                    self.compression,
                    self.max_body_bytes,
                    self.body_timeout,
                )
            except Exception:
                self._close_error(400, "Invalid HTTP request")
                return
        self.head_bytes = 40
        self.request_dispatched = False
        self.request_keep_alive = True
        self.header_timeout_reset = True

    cdef void _on_url(self, const char* at, size_t length) noexcept:
        if self.rejected or self.reading_exchange is None:
            return
        if self._header_too_large(length):
            return
        if self.reading_exchange.append_request_url(at, length) != 0:
            self._close_error(431, "Request header fields too large")

    cdef void _on_header_field(self, const char* at, size_t length) noexcept:
        if self.rejected or self.reading_exchange is None:
            return
        if self._header_too_large(length):
            return
        if self.reading_exchange.append_request_header_name(at, length) != 0:
            self._close_error(400, "Invalid HTTP request")

    cdef void _on_header_value(self, const char* at, size_t length) noexcept:
        if self.rejected or self.reading_exchange is None:
            return
        if self._header_too_large(length):
            return
        if self.reading_exchange.append_request_header_value(at, length) != 0:
            self._close_error(400, "Invalid HTTP request")

    cdef void _on_header_value_complete(self) noexcept:
        if self.reading_exchange is not None:
            if self.reading_exchange.finish_request_header() != 0:
                self._close_error(400, "Invalid HTTP request")

    cdef void _on_headers_complete(self) noexcept:
        cdef RequestExchange exchange
        cdef uint16_t flags
        cdef uint64_t content_length
        if self.rejected or self.reading_exchange is None:
            return
        exchange = self.reading_exchange
        if exchange.finish_request_header() != 0:
            self._close_error(400, "Invalid HTTP request")
            return
        if llhttp_get_upgrade(self.parser):
            self._close_error(400, "Upgrade not supported")
            return
        flags = stario_parser_flags(self.parser)
        content_length = stario_parser_content_length(self.parser)
        if flags & F_CONTENT_LENGTH and content_length > <uint64_t>self.max_body_bytes:
            self._close_error(413, "Request body too large")
            return
        exchange.cache_hot_request_headers()
        self.request_keep_alive = (
            llhttp_should_keep_alive(self.parser) != 0
            or (
                llhttp_get_http_major(self.parser) == 1
                and llhttp_get_http_minor(self.parser) == 1
                and not exchange._req_connection_close
            )
        )
        if self.request_dispatched or self.rejected:
            return
        if self.transport is None or self.transport.is_closing():
            return
        try:
            exchange.reset_body(
                exchange._req_expect_continue,
                <Py_ssize_t>content_length if flags & F_CONTENT_LENGTH else -1,
            )
            # Small Content-Length bodies that fit in the current read complete
            # before the handler runs, so body() hits the cached bytes instead
            # of waiting on an Event during llhttp_execute. Expect: 100-continue
            # and large/chunked bodies still dispatch at headers-complete.
            if (
                not exchange._req_expect_continue
                and (flags & F_CONTENT_LENGTH)
                and 0 < content_length <= <uint64_t>SMALL_BODY_COMPLETE_DISPATCH
            ):
                return
            self._dispatch(exchange, self._build_request(exchange, exchange))
        except Exception:
            self._close_error(400, "Invalid HTTP request")

    cdef void _on_body(self, const char* at, size_t length) noexcept:
        if self.rejected or self.reading_exchange is None:
            return
        if self.reading_exchange.c_feed(at, length) != 0:
            self._close_error(400, "Invalid HTTP request")

    cdef void _on_message_complete(self) noexcept:
        cdef RequestExchange exchange
        if self.rejected:
            return
        exchange = self.reading_exchange
        if exchange is not None and exchange._body_active:
            if exchange.c_complete() != 0:
                self._close_error(400, "Invalid HTTP request")
                self.reading_exchange = None
                return
        if (
            not self.request_dispatched
            and exchange is not None
            and not self.rejected
        ):
            try:
                if self.transport is None or self.transport.is_closing():
                    self.reading_exchange = None
                    return
                self._dispatch(
                    exchange,
                    self._build_request(exchange, exchange),
                )
            except Exception:
                self._close_error(400, "Invalid HTTP request")
                self.reading_exchange = None
                return
        self.reading_exchange = None

    cdef Request _build_request(self, RequestExchange exchange, object body):
        cdef object method
        cdef object version
        cdef tuple split
        cdef Request request = exchange.req
        if exchange._req_url_length > 0:
            split = _split_request_target_n(
                exchange._req_arena + exchange._req_url_offset,
                exchange._req_url_length,
            )
        else:
            split = (PATH_EMPTY, Q_EMPTY)
        method = _method_str(<int>llhttp_get_method(self.parser))
        version = _version_str(
            <int>llhttp_get_http_major(self.parser),
            <int>llhttp_get_http_minor(self.parser),
        )
        request.reset(
            method,
            split[0],
            split[1],
            version,
            self.request_keep_alive,
            exchange.request_headers,
            body,
        )
        return request

    cdef void _dispatch(self, RequestExchange exchange, Request request):
        cdef object span
        self.request_dispatched = True
        if self.timeout_kind == TIMEOUT_HEADER:
            self.timeout_kind = TIMEOUT_NONE
            self.timeout_deadline = 0.0
        if self.noop_span is not None:
            span = self.noop_span
        else:
            span = self.tracer.create("request") if self.tracer is not None else None
        exchange.span = span
        if self.active_exchange is None:
            self._start_exchange(exchange, True)
        else:
            if len(self.pending_exchanges) >= self.max_pipelined_requests:
                self._close_error(429, "Too many pipelined requests")
                return
            self.pending_exchanges.append(exchange)
            self._set_pause_reason(PAUSE_PIPELINE, True)

    cdef void _start_exchange(self, RequestExchange exchange, bint eager_start):
        cdef object task
        self.active_exchange = exchange
        exchange.start_response()
        task = self._create_task(
            self.app(exchange, exchange),
            loop=self.loop,
            eager_start=eager_start,
        )
        if task.done():
            exchange.handler_finished()
        else:
            task.add_done_callback(exchange.on_handler_done)

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
        # User may have set Connection: close on the response Headers.
        conn = exchange.headers.c_get(b"connection")
        if conn is not None and (
            conn == b"close" or conn.lower() == b"close"
        ):
            transport.close()
            self._drop_pending()
            return
        if not exchange.req.keep_alive or self.app.shutdown.done():
            transport.close()
            self._drop_pending()
            return
        if self.pending_exchanges:
            next_exchange = self.pending_exchanges.popleft()
            self._start_exchange(next_exchange, False)
            if not self.pending_exchanges:
                self._set_pause_reason(PAUSE_PIPELINE, False)
            return
        self._set_pause_reason(PAUSE_PIPELINE, False)
        self._arm_timeout(TIMEOUT_IDLE, self.keep_alive_timeout)

    cdef void _close_error(self, int status, object message) noexcept:
        cdef object transport
        cdef object body
        cdef object date
        if self.rejected:
            return
        self.rejected = True
        self._cancel_timeout()
        transport = self.transport
        try:
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
                    _status_line(status),
                    date,
                    b"content-type: text/plain; charset=utf-8\r\n",
                    b"content-length: %d\r\n" % len(body),
                    b"connection: close\r\n",
                    b"\r\n",
                    body,
                ))
            )
            transport.close()
        except Exception:
            try:
                if transport is not None:
                    transport.close()
            except Exception:
                pass
