# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True
"""asyncio Protocol: llhttp for HTTP/1, nghttp2 for HTTP/2.

``HttpProtocol`` owns parser callbacks, pause/resume, and request dispatch.
H1 vs H2 is chosen once (TLS ALPN or the cleartext H2 preface). URL bytes
and header fragments go into the current exchange arena. Path/query
decoding is cached here because it is connection-parse work.

Header, idle, and body-stall timeouts share one cleanup path. Under Server
that path is the Date-header tick (once a second): one ``loop.time()``, then
compare stored deadlines. Tests and raw ``create_server`` use a fallback
sweeper at the same period. See ``stario_cython.timeouts``.
"""

import asyncio
from collections import deque

from libc.stddef cimport size_t
from libc.stdint cimport int32_t, uint8_t, uint16_t, uint32_t, uint64_t
from libc.stdlib cimport free, malloc
from libc.string cimport memcmp, memcpy, memmove
from cpython.bytearray cimport (
    PyByteArray_AS_STRING,
    PyByteArray_Check,
    PyByteArray_GET_SIZE,
    PyByteArray_Resize,
)
from cpython.bytes cimport (
    PyBytes_AS_STRING,
    PyBytes_Check,
    PyBytes_FromStringAndSize,
    PyBytes_GET_SIZE,
)
from cpython.exc cimport PyErr_Clear
from cpython.unicode cimport PyUnicode_DecodeASCII, PyUnicode_DecodeLatin1

from stario.http.config import (
    DEFAULT_HEADER_TIMEOUT,
    DEFAULT_KEEP_ALIVE_TIMEOUT,
    DEFAULT_MAX_PIPELINED_REQUESTS,
)
from stario.http.invoke import finish_request_span, on_handler_done
from stario.http.request import DEFAULT_BODY_TIMEOUT
from stario.http.wire import decode_path
from stario.telemetry.noop import NoOpTracer

from stario_cython.exchange cimport Request, RequestExchange, acquire_exchange, _status_line
from stario_cython.headers cimport Headers
from stario_cython.llhttp cimport *
from stario_cython.nghttp2 cimport (
    ssize_t,
    NGHTTP2_DATA,
    NGHTTP2_DATA_FLAG_EOF,
    NGHTTP2_DATA_FLAG_NO_COPY,
    NGHTTP2_ERR_CALLBACK_FAILURE,
    NGHTTP2_ERR_DEFERRED,
    NGHTTP2_FLAG_END_STREAM,
    NGHTTP2_FLAG_NONE,
    NGHTTP2_HEADERS,
    NGHTTP2_INTERNAL_ERROR,
    NGHTTP2_PROTOCOL_ERROR,
    NGHTTP2_NV_FLAG_NONE,
    NGHTTP2_NV_FLAG_NO_COPY_NAME,
    NGHTTP2_NV_FLAG_NO_COPY_VALUE,
    NGHTTP2_SETTINGS_ENABLE_PUSH,
    NGHTTP2_SETTINGS_INITIAL_WINDOW_SIZE,
    NGHTTP2_SETTINGS_MAX_CONCURRENT_STREAMS,
    nghttp2_data_provider,
    nghttp2_data_source,
    nghttp2_frame,
    nghttp2_nv,
    nghttp2_option,
    nghttp2_option_del,
    nghttp2_option_new,
    nghttp2_option_set_no_auto_window_update,
    nghttp2_session,
    nghttp2_session_callbacks,
    nghttp2_session_callbacks_del,
    nghttp2_session_callbacks_new,
    nghttp2_session_callbacks_set_on_begin_headers_callback,
    nghttp2_session_callbacks_set_on_data_chunk_recv_callback,
    nghttp2_session_callbacks_set_on_frame_recv_callback,
    nghttp2_session_callbacks_set_on_header_callback,
    nghttp2_session_callbacks_set_on_stream_close_callback,
    nghttp2_session_callbacks_set_send_data_callback,
    nghttp2_session_del,
    nghttp2_session_get_effective_recv_data_length,
    nghttp2_session_get_local_window_size,
    nghttp2_session_get_stream_effective_recv_data_length,
    nghttp2_session_get_stream_local_window_size,
    nghttp2_session_get_stream_user_data,
    nghttp2_session_mem_recv,
    nghttp2_session_mem_send,
    nghttp2_session_resume_data,
    nghttp2_session_server_new2,
    nghttp2_session_set_local_window_size,
    nghttp2_session_set_stream_user_data,
    nghttp2_settings_entry,
    nghttp2_submit_headers,
    nghttp2_submit_response,
    nghttp2_submit_rst_stream,
    nghttp2_submit_settings,
    nghttp2_submit_window_update,
)
from stario_cython.timeouts import (
    DATE_TICK_SWEEP_ATTR as _PY_DATE_TICK_SWEEP_ATTR,
    TIMEOUT_MODE as _PY_TIMEOUT_MODE,
    sweep_interval as _py_sweep_interval,
)

cdef enum:
    URL_CACHE_CAP = 256
    URL_CACHE_MAX_KEY = 512
    # 256 KiB sits above API/RPC p90 (~12 KiB) and around p99 (~200 KiB).
    # body() is then already bytes when the handler starts. File uploads
    # (MiB) still dispatch at headers-complete so stream() can start early.
    SMALL_BODY_COMPLETE_DISPATCH = 256 * 1024
    PARSE_NONE = 0
    PARSE_H1 = 1
    PARSE_H2 = 2
    H2_PREFACE_LEN = 24
    # Receive windows: default 64KiB stalls a multiplexed POST and
    # auto-WINDOW_UPDATE emits a frame per DATA. 1MiB/4MiB plus batched
    # consume keeps credit up without a frame per request.
    H2_STREAM_WINDOW = 1024 * 1024
    H2_CONN_WINDOW = 4 * 1024 * 1024
    H2_STREAM_UPDATE_THRESH = 256 * 1024
    H2_CONN_UPDATE_THRESH = 512 * 1024
    H2_MAX_CONCURRENT = 256

cdef char* _UC_KEY[256]
cdef Py_ssize_t _UC_LEN[256]
cdef uint32_t _UC_HASH[256]
cdef Py_ssize_t _UC_QOFF[256]
cdef Py_ssize_t _UC_QLEN[256]
cdef list _UC_PATH = None
cdef object PATH_EMPTY = ""
cdef bytes H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
cdef uint8_t H2_NV_NOCOPY = (
    NGHTTP2_NV_FLAG_NO_COPY_NAME | NGHTTP2_NV_FLAG_NO_COPY_VALUE
)


cdef uint32_t _url_hash(const char* s, Py_ssize_t n) noexcept:
    cdef uint32_t value = <uint32_t>2166136261
    cdef Py_ssize_t i
    for i in range(n):
        value = (value ^ <unsigned char>s[i]) * <uint32_t>16777619
    return value


cdef void _url_cache_init():
    global _UC_PATH
    cdef int i
    if _UC_PATH is not None:
        return
    _UC_PATH = [None] * URL_CACHE_CAP
    for i in range(URL_CACHE_CAP):
        _UC_KEY[i] = NULL
        _UC_LEN[i] = 0
        _UC_HASH[i] = 0
        _UC_QOFF[i] = 0
        _UC_QLEN[i] = 0


cdef void _url_cache_clear():
    cdef int i
    for i in range(URL_CACHE_CAP):
        if _UC_KEY[i] != NULL:
            free(_UC_KEY[i])
            _UC_KEY[i] = NULL
        _UC_LEN[i] = 0
        _UC_HASH[i] = 0
        _UC_QOFF[i] = 0
        _UC_QLEN[i] = 0
        _UC_PATH[i] = None


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
    Py_ssize_t qoff,
    Py_ssize_t qlen,
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
            _UC_QOFF[slot] = qoff
            _UC_QLEN[slot] = qlen
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
    _UC_QOFF[slot] = qoff
    _UC_QLEN[slot] = qlen


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


cdef object _path_for_url(
    const char* url,
    Py_ssize_t n,
    Py_ssize_t* qoff,
    Py_ssize_t* qlen,
):
    """Decoded path; query is a span inside the current URL (no copy)."""
    cdef Py_ssize_t question = -1
    cdef Py_ssize_t i
    cdef uint32_t h = 0
    cdef int slot
    cdef object path
    qoff[0] = 0
    qlen[0] = 0
    if n <= 0:
        return PATH_EMPTY
    if n <= URL_CACHE_MAX_KEY:
        h = _url_hash(url, n)
        slot = _url_find(url, n, h)
        if slot >= 0:
            qoff[0] = _UC_QOFF[slot]
            qlen[0] = _UC_QLEN[slot]
            return _UC_PATH[slot]
    for i in range(n):
        if url[i] == 63:
            question = i
            break
    if question < 0:
        path = _decode_path_n(url, n)
    else:
        path = _decode_path_n(url, question)
        if question + 1 < n:
            qoff[0] = question + 1
            qlen[0] = n - question - 1
    if n <= URL_CACHE_MAX_KEY:
        _url_store(url, n, h, path, qoff[0], qlen[0])
    return path

cdef inline bint _as_buf(object data, const char** ptr, Py_ssize_t* n) noexcept:
    if PyBytes_Check(data):
        ptr[0] = PyBytes_AS_STRING(data)
        n[0] = PyBytes_GET_SIZE(data)
        return True
    if PyByteArray_Check(data):
        ptr[0] = PyByteArray_AS_STRING(data)
        n[0] = PyByteArray_GET_SIZE(data)
        return True
    return False


cdef int _preface_append(object hold, const char* p, Py_ssize_t n) noexcept:
    cdef Py_ssize_t old
    if n <= 0:
        return 0
    old = PyByteArray_GET_SIZE(hold)
    if PyByteArray_Resize(hold, old + n) < 0:
        PyErr_Clear()
        return -1
    memcpy(PyByteArray_AS_STRING(hold) + old, p, <size_t>n)
    return 0


cdef int F_CHUNKED = 0x8
cdef int F_CONTENT_LENGTH = 0x20
cdef int F_TRANSFER_ENCODING = 0x200
cdef int PAUSE_WRITE = 1
cdef int PAUSE_PIPELINE = 2
cdef int PAUSE_BODY = 4
cdef int PARSER_QUANTUM = 512 * 1024
cdef int TIMEOUT_NONE = 0
cdef int TIMEOUT_HEADER = 1
cdef int TIMEOUT_IDLE = 2
cdef int CLEANUP_OFF = 0
cdef int CLEANUP_SWEEP = 1
cdef int TIMEOUT_MODE = 1
cdef double TIMEOUT_SWEEP_INTERVAL = 1.0
cdef object _SWEEPS_ATTR = "_stario_timeout_sweeps"
# Empty / small bodies finish before the handler runs so body() is cached.
# Large, chunked, and 100-continue still dispatch at headers.

cdef object METH_DELETE = "DELETE"
cdef object METH_GET = "GET"
cdef object METH_HEAD = "HEAD"
cdef object METH_POST = "POST"
cdef object METH_PUT = "PUT"
cdef object METH_CONNECT = "CONNECT"
cdef object METH_OPTIONS = "OPTIONS"
cdef object METH_TRACE = "TRACE"
cdef object METH_PATCH = "PATCH"
cdef object METH_QUERY = "QUERY"
cdef object VER_10 = "1.0"
cdef object VER_11 = "1.1"
cdef object VER_20 = "2"

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
    if major == 2:
        return VER_20
    if major == 1 and minor == 1:
        return VER_11
    if major == 1 and minor == 0:
        return VER_10
    return "%d.%d" % (major, minor)


cdef inline bint _header_name_ok(const char* p, size_t n) noexcept nogil:
    cdef size_t i
    cdef unsigned char c
    if n == 0:
        return False
    for i in range(n):
        c = <unsigned char>p[i]
        if c <= 32 or c >= 127:
            return False
    return True


cdef object _method_from_bytes(const char* p, size_t n) noexcept:
    if n == 3:
        if memcmp(p, "GET", 3) == 0:
            return METH_GET
        if memcmp(p, "PUT", 3) == 0:
            return METH_PUT
    elif n == 4:
        if memcmp(p, "POST", 4) == 0:
            return METH_POST
        if memcmp(p, "HEAD", 4) == 0:
            return METH_HEAD
    elif n == 5:
        if memcmp(p, "PATCH", 5) == 0:
            return METH_PATCH
        if memcmp(p, "TRACE", 5) == 0:
            return METH_TRACE
        if memcmp(p, "QUERY", 5) == 0:
            return METH_QUERY
    elif n == 6:
        if memcmp(p, "DELETE", 6) == 0:
            return METH_DELETE
    elif n == 7:
        if memcmp(p, "OPTIONS", 7) == 0:
            return METH_OPTIONS
        if memcmp(p, "CONNECT", 7) == 0:
            return METH_CONNECT
    return PyUnicode_DecodeLatin1(p, <Py_ssize_t>n, NULL)



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


cdef int _h2_on_begin_headers(
    nghttp2_session* session,
    const nghttp2_frame* frame,
    void* user_data,
) noexcept:
    cdef HttpProtocol proto
    try:
        if frame.hd.type != NGHTTP2_HEADERS:
            return 0
        proto = <HttpProtocol>user_data
        proto._h2_begin_stream(frame.hd.stream_id)
        return 0
    except Exception:
        return NGHTTP2_ERR_CALLBACK_FAILURE


cdef int _h2_on_header(
    nghttp2_session* session,
    const nghttp2_frame* frame,
    const uint8_t* name,
    size_t namelen,
    const uint8_t* value,
    size_t valuelen,
    uint8_t flags,
    void* user_data,
) noexcept:
    cdef HttpProtocol proto = <HttpProtocol>user_data
    proto._h2_on_header(frame.hd.stream_id, name, namelen, value, valuelen)
    return 0


cdef int _h2_on_data(
    nghttp2_session* session,
    uint8_t flags,
    int32_t stream_id,
    const uint8_t* data,
    size_t length,
    void* user_data,
) noexcept:
    cdef HttpProtocol proto = <HttpProtocol>user_data
    proto._h2_on_data(stream_id, data, length)
    return 0


cdef int _h2_on_frame_recv(
    nghttp2_session* session,
    const nghttp2_frame* frame,
    void* user_data,
) noexcept:
    cdef HttpProtocol proto
    cdef uint8_t ftype
    try:
        proto = <HttpProtocol>user_data
        ftype = frame.hd.type
        if ftype == NGHTTP2_HEADERS:
            proto._h2_headers_frame(
                frame.hd.stream_id, (frame.hd.flags & NGHTTP2_FLAG_END_STREAM) != 0
            )
        elif ftype == NGHTTP2_DATA and (frame.hd.flags & NGHTTP2_FLAG_END_STREAM):
            proto._h2_end_stream(frame.hd.stream_id)
        return 0
    except Exception:
        return NGHTTP2_ERR_CALLBACK_FAILURE


cdef int _h2_on_stream_close(
    nghttp2_session* session,
    int32_t stream_id,
    uint32_t error_code,
    void* user_data,
) noexcept:
    cdef HttpProtocol proto
    try:
        proto = <HttpProtocol>user_data
        proto._h2_stream_closed(stream_id)
        return 0
    except Exception:
        return NGHTTP2_ERR_CALLBACK_FAILURE


cdef ssize_t _h2_data_source_read(
    nghttp2_session* session,
    int32_t stream_id,
    uint8_t* buf,
    size_t length,
    uint32_t* data_flags,
    nghttp2_data_source* source,
    void* user_data,
) noexcept:
    cdef HttpProtocol proto = <HttpProtocol>user_data
    cdef RequestExchange ex
    if source != NULL and source.ptr != NULL:
        ex = <RequestExchange>source.ptr
        return proto._h2_read_data(ex, buf, length, data_flags)
    return NGHTTP2_ERR_CALLBACK_FAILURE


cdef int _h2_send_data(
    nghttp2_session* session,
    nghttp2_frame* frame,
    const uint8_t* framehd,
    size_t length,
    nghttp2_data_source* source,
    void* user_data,
) noexcept:
    cdef HttpProtocol proto = <HttpProtocol>user_data
    return proto._h2_send_data_frame(frame, framehd, length, source)


def _bind_timeout_policy():
    global TIMEOUT_MODE, TIMEOUT_SWEEP_INTERVAL
    TIMEOUT_MODE = <int>_PY_TIMEOUT_MODE
    TIMEOUT_SWEEP_INTERVAL = <double>_py_sweep_interval()


_bind_timeout_policy()


async def _timeout_sweep_loop(loop, connections, key):
    """One ``loop.time()`` per wake, then compare every live connection."""
    sleep = asyncio.sleep
    interval = TIMEOUT_SWEEP_INTERVAL
    try:
        while True:
            await sleep(interval)
            if not connections:
                continue
            now = loop.time()
            for proto in tuple(connections):
                proto.check_timeouts(now)
    except asyncio.CancelledError:
        sweeps = getattr(loop, _SWEEPS_ATTR, None)
        if sweeps is not None:
            sweeps.pop(key, None)
        raise


def _ensure_timeout_sweeper(loop, connections):
    if TIMEOUT_MODE != CLEANUP_SWEEP:
        return
    # Server Date tick already walks this loop's connections once a second.
    if getattr(loop, _PY_DATE_TICK_SWEEP_ATTR, False):
        return
    sweeps = getattr(loop, _SWEEPS_ATTR, None)
    if sweeps is None:
        sweeps = {}
        setattr(loop, _SWEEPS_ATTR, sweeps)
    key = id(connections)
    task = sweeps.get(key)
    if task is not None and not task.done():
        return
    coro = _timeout_sweep_loop(loop, connections, key)
    try:
        task = loop.create_task(coro, name="stario-timeout-sweep")
    except TypeError:
        task = loop.create_task(coro)
    sweeps[key] = task


def _stop_timeout_sweeper(loop, connections):
    if connections:
        return
    sweeps = getattr(loop, _SWEEPS_ATTR, None)
    if not sweeps:
        return
    task = sweeps.pop(id(connections), None)
    if task is not None and not task.done():
        task.cancel()


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
    cdef object _find_handler
    cdef public int parse_mode
    cdef bytearray preface_hold
    cdef nghttp2_session* h2
    cdef dict h2_streams
    cdef object h2_out
    cdef Py_ssize_t h2_out_used
    cdef bint h2_in_recv

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
        self.header_timeout_reset = False
        self.parse_mode = PARSE_NONE
        self.preface_hold = bytearray()
        self.h2 = NULL
        self.h2_streams = {}
        self.h2_out = bytearray()
        self.h2_out_used = 0
        self.h2_in_recv = False

    def __dealloc__(self):
        if self.parser != NULL:
            stario_parser_del(self.parser)
            self.parser = NULL
        if self.h2 != NULL:
            nghttp2_session_del(self.h2)
            self.h2 = NULL

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
        self._find_handler = app.find_handler
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
        self.header_timeout_reset = False

    def connection_made(self, transport):
        cdef object ssl_object
        cdef object selected
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
        self.parse_mode = PARSE_NONE
        self.preface_hold.clear()
        self.connections.add(self)
        ssl_object = transport.get_extra_info("ssl_object")
        if ssl_object is not None:
            selected = ssl_object.selected_alpn_protocol()
            if selected == "h2":
                self._start_h2()
        # First request: header deadline only. Keep-alive stores a
        # deadline; the sweeper compares it. No TimerHandle per request.
        self._arm_timeout(TIMEOUT_HEADER, self.header_timeout)
        _ensure_timeout_sweeper(self.loop, self.connections)

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
        _stop_timeout_sweeper(self.loop, self.connections)
        if self.h2 != NULL:
            nghttp2_session_del(self.h2)
            self.h2 = NULL
        for exchange in list(self.h2_streams.values()):
            exchange.c_abort()
            if not exchange.handler_started:
                exchange.cancel_before_start()
        self.h2_streams.clear()
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
        if self.rejected or not data:
            return
        if self.parse_mode != PARSE_H2 and self.parser == NULL:
            return
        if self.timeout_kind == TIMEOUT_IDLE:
            # Keep-alive traffic: drop the idle deadline. The sweeper
            # sees TIMEOUT_NONE on the next wake.
            self.timeout_kind = TIMEOUT_NONE
            self.timeout_deadline = 0.0
        if self.held_data is not None:
            self._held_append(data)
            return
        if self.pause_reasons:
            self.held_data = data
            self.held_offset = 0
            return
        self._pump_data(data, 0)
        self._after_pump()

    cdef void _held_append(self, object data) noexcept:
        """Append ``data`` onto paused input without ``held[off:] + data``."""
        cdef object held = self.held_data
        cdef Py_ssize_t off = self.held_offset
        cdef Py_ssize_t keep
        cdef Py_ssize_t add_n
        cdef Py_ssize_t new_n
        cdef const char* src = NULL
        cdef const char* oldp = NULL
        cdef char* dst
        cdef object ba
        if not _as_buf(data, &src, &add_n):
            self.held_data = held[off:] + data
            self.held_offset = 0
            return
        if PyBytes_Check(held):
            oldp = PyBytes_AS_STRING(held)
            keep = PyBytes_GET_SIZE(held) - off
            if keep < 0:
                keep = 0
            new_n = keep + add_n
            ba = bytearray()
            if PyByteArray_Resize(ba, new_n) < 0:
                PyErr_Clear()
                self.held_data = held[off:] + data
                self.held_offset = 0
                return
            dst = PyByteArray_AS_STRING(ba)
            if keep:
                memcpy(dst, oldp + off, <size_t>keep)
            if add_n:
                memcpy(dst + keep, src, <size_t>add_n)
            self.held_data = ba
            self.held_offset = 0
            return
        if PyByteArray_Check(held):
            oldp = PyByteArray_AS_STRING(held)
            keep = PyByteArray_GET_SIZE(held) - off
            if keep < 0:
                keep = 0
            if off:
                if keep:
                    memmove(<char*>oldp, oldp + off, <size_t>keep)
                self.held_offset = 0
            new_n = keep + add_n
            if PyByteArray_Resize(held, new_n) < 0:
                PyErr_Clear()
                return
            if add_n:
                memcpy(PyByteArray_AS_STRING(held) + keep, src, <size_t>add_n)
            return
        self.held_data = held[off:] + data
        self.held_offset = 0

    cdef void _arm_timeout(self, int kind, double seconds):
        """Store a header or idle deadline on the connection.

        The sweeper compares ``timeout_deadline`` to one ``loop.time()``
        per wake. wrk keep-alive is a double store, not ``call_later``.
        """
        cdef object transport
        if TIMEOUT_MODE == CLEANUP_OFF:
            return
        transport = self.transport
        if transport is None or transport.is_closing() or seconds <= 0:
            return
        self.timeout_kind = kind
        # Sweeper fills now+seconds on the next Date tick. Avoid loop.time()
        # on the keep-alive hot path (every wrk GET).
        self.timeout_deadline = 0.0

    cdef void _cancel_timeout(self):
        self.timeout_kind = TIMEOUT_NONE
        self.timeout_deadline = 0.0

    cdef void _check_body_stall(self, RequestExchange exchange, double now):
        if exchange is None or exchange._timeout <= 0 or not exchange._waiting:
            return
        if exchange._stall_touch != exchange._stall_seen:
            exchange._stall_seen = exchange._stall_touch
            exchange._stall_deadline = now + exchange._timeout
            return
        if exchange._stall_deadline > 0.0 and now >= exchange._stall_deadline:
            exchange.fire_body_stall()

    cpdef void check_timeouts(self, double now):
        """Compare stored deadlines against ``now`` (one value per sweep)."""
        cdef RequestExchange reading
        cdef RequestExchange active
        cdef RequestExchange exchange
        cdef object transport
        cdef double seconds
        if self.rejected:
            return
        if self.timeout_kind != TIMEOUT_NONE:
            if self.timeout_deadline <= 0.0:
                if self.timeout_kind == TIMEOUT_HEADER:
                    seconds = self.header_timeout
                else:
                    seconds = self.keep_alive_timeout
                if seconds <= 0.0:
                    self.timeout_kind = TIMEOUT_NONE
                    self.timeout_deadline = 0.0
                else:
                    self.timeout_deadline = now + seconds
            elif now >= self.timeout_deadline:
                self.timeout_kind = TIMEOUT_NONE
                self.timeout_deadline = 0.0
                transport = self.transport
                if transport is not None and not transport.is_closing():
                    transport.close()
                return
        reading = self.reading_exchange
        active = self.active_exchange
        self._check_body_stall(reading, now)
        if active is not None and active is not reading:
            self._check_body_stall(active, now)
        if self.parse_mode == PARSE_H2:
            for exchange in self.h2_streams.values():
                if exchange is not reading and exchange is not active:
                    self._check_body_stall(exchange, now)

    cdef void _after_pump(self):
        """Arm a header deadline only if this read left headers (or a deferred
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
        cdef const char* ptr = NULL
        cdef Py_ssize_t n
        if not _as_buf(data, &ptr, &n):
            n = len(data)
            ptr = <const char*>data
        if offset >= n:
            return
        if self.parse_mode == PARSE_H2:
            self._h2_recv(ptr + offset, <size_t>(n - offset))
            return
        if self.parse_mode == PARSE_NONE:
            if not self._maybe_start_h2(ptr + offset, n - offset):
                return
            if self.parse_mode == PARSE_H2:
                self._h2_recv(ptr + offset, <size_t>(n - offset))
                return
            if self.preface_hold:
                self._h1_execute_held()
                return
        self._h1_execute(data, offset)

    cdef bint _maybe_start_h2(self, const char* p, Py_ssize_t n) noexcept:
        """Detect the H2 preface before llhttp sees the first bytes.

        Incomplete preface prefixes are held. Anything else is HTTP/1.
        When the hold already contains this chunk, H1 fallback executes
        the hold only (do not append ``data`` again).
        """
        cdef const char* pref = PyBytes_AS_STRING(H2_PREFACE)
        cdef bint from_hold = False
        if self.preface_hold:
            if _preface_append(self.preface_hold, p, n) != 0:
                self._close_error(500, "Internal Server Error")
                return False
            p = PyByteArray_AS_STRING(self.preface_hold)
            n = PyByteArray_GET_SIZE(self.preface_hold)
            from_hold = True
        elif n > 0 and n < H2_PREFACE_LEN and memcmp(p, pref, <size_t>n) == 0:
            if _preface_append(self.preface_hold, p, n) != 0:
                self._close_error(500, "Internal Server Error")
            return False
        if n >= H2_PREFACE_LEN and memcmp(p, pref, <size_t>H2_PREFACE_LEN) == 0:
            if from_hold:
                self._start_h2()
                self._h2_recv(p, <size_t>n)
                self.preface_hold.clear()
                return False
            self._start_h2()
            return True
        if n < H2_PREFACE_LEN and n > 0 and memcmp(p, pref, <size_t>n) == 0:
            if not from_hold:
                if _preface_append(self.preface_hold, p, n) != 0:
                    self._close_error(500, "Internal Server Error")
            return False
        self.parse_mode = PARSE_H1
        return True

    cdef void _h1_execute_held(self) noexcept:
        cdef object held = self.preface_hold
        self.preface_hold = bytearray()
        self._h1_execute(held, 0)

    cdef void _h1_execute(self, object data, Py_ssize_t offset) noexcept:
        cdef const char* ptr = NULL
        cdef Py_ssize_t n
        cdef Py_ssize_t end
        cdef int err
        if self.parser == NULL:
            return
        if not _as_buf(data, &ptr, &n):
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
            or self.h2_streams
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
        # RFC 7230 3.3.3: TE without chunked as the final coding — body
        # length is undefined. Reject before dispatch (llhttp errors after
        # on_headers_complete, which would otherwise 404 then 400).
        if (flags & F_TRANSFER_ENCODING) and not (flags & F_CHUNKED):
            self._close_error(400, "Invalid HTTP request")
            return
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
            if (
                (flags & F_CHUNKED)
                or (flags & F_CONTENT_LENGTH and content_length > 0)
                or exchange._req_expect_continue
            ):
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
                    and content_length <= <uint64_t>SMALL_BODY_COMPLETE_DISPATCH
                ):
                    return
            else:
                exchange.mark_nobody()
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
        cdef object path
        cdef Py_ssize_t qoff = 0
        cdef Py_ssize_t qlen = 0
        cdef Request request = exchange.req
        if exchange._req_url_length > 0:
            path = _path_for_url(
                exchange._req_arena + exchange._req_url_offset,
                exchange._req_url_length,
                &qoff,
                &qlen,
            )
        else:
            path = PATH_EMPTY
        if exchange._http2:
            method = exchange._h2_method or METH_GET
            version = VER_20
        else:
            method = _method_str(<int>llhttp_get_method(self.parser))
            version = _version_str(
                <int>llhttp_get_http_major(self.parser),
                <int>llhttp_get_http_minor(self.parser),
            )
        request.reset(
            method,
            path,
            None,
            version,
            True if exchange._http2 else self.request_keep_alive,
            exchange.request_headers,
            body,
        )
        if qlen > 0:
            request.bind_query_span(
                exchange,
                exchange._req_url_offset + qoff,
                qlen,
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
        if exchange._http2:
            self._start_exchange(exchange, True)
            return
        if self.active_exchange is None:
            self._start_exchange(exchange, True)
        else:
            if len(self.pending_exchanges) >= self.max_pipelined_requests:
                self._close_error(429, "Too many pipelined requests")
                return
            self.pending_exchanges.append(exchange)
            self._set_pause_reason(PAUSE_PIPELINE, True)

    cdef void _start_exchange(self, RequestExchange exchange, bint eager_start):
        cdef object req
        cdef object path
        cdef object handler
        cdef object route
        cdef object task
        cdef object span
        cdef object host
        cdef object method
        cdef object loc
        cdef const char* url
        cdef Py_ssize_t n
        cdef Py_ssize_t i
        cdef Py_ssize_t path_end
        cdef Py_ssize_t start
        cdef Py_ssize_t end
        if not exchange._http2:
            self.active_exchange = exchange
        n = exchange._req_url_length
        if n > 1:
            url = exchange._req_arena + exchange._req_url_offset
            path_end = n
            for i in range(n):
                if url[i] == 63:
                    path_end = i
                    break
            if path_end > 1 and url[path_end - 1] == 47:
                start = 0
                end = path_end
                while start < end and url[start] == 47:
                    start += 1
                while end > start and url[end - 1] == 47:
                    end -= 1
                if exchange._http2:
                    loc = b"/"
                    if end > start:
                        loc = loc + PyBytes_FromStringAndSize(
                            url + start, end - start
                        )
                    if path_end + 1 < n:
                        loc = loc + b"?" + PyBytes_FromStringAndSize(
                            url + path_end + 1, n - path_end - 1
                        )
                    exchange.start_response()
                    exchange.headers.set("location", loc.decode("latin-1"))
                    if self.noop_span is None:
                        req = exchange.req
                        finish_request_span(
                            exchange.span,
                            status=308,
                            method=req.method,
                            path=req.path,
                        )
                    exchange.respond(b"", b"text/plain; charset=utf-8", 308)
                    exchange.handler_finished()
                    return
                exchange.start_response()
                exchange._buf_bytes(_status_line(308))
                exchange._buf_bytes(exchange._date_box[0])
                exchange._buf_bytes(b"location: /")
                if end > start:
                    exchange._buf_add(url + start, end - start)
                if path_end + 1 < n:
                    exchange._buf_bytes(b"?")
                    exchange._buf_add(url + path_end + 1, n - path_end - 1)
                exchange._buf_bytes(b"\r\ncontent-length: 0\r\n\r\n")
                exchange._flush()
                exchange._status_code = 308
                exchange._completed = True
                if self.noop_span is None:
                    req = exchange.req
                    finish_request_span(
                        exchange.span,
                        status=308,
                        method=req.method,
                        path=req.path,
                    )
                exchange._done()
                exchange.handler_finished()
                return
        exchange.start_response()
        req = exchange.req
        path = req.path
        method = req.method
        span = exchange.span
        if span is not None and self.noop_span is None:
            span.start()
            span.attrs({"request.method": method, "request.path": path})
        host = req.host if self.app.host_routing else ""
        handler, route = self._find_handler(host, path, method)
        exchange.route = route
        task = self._create_task(
            handler(exchange, exchange),
            loop=self.loop,
            eager_start=eager_start,
        )
        if task.done():
            # Skip only a clean NoOp success. Write-then-raise / cancel / an
            # incomplete response must still hit on_handler_done (log + abort).
            if (
                not exchange._completed
                or self.noop_span is None
                or task.cancelled()
                or task.exception() is not None
            ):
                on_handler_done(exchange, exchange, task)
            exchange.handler_finished()
        else:
            task.add_done_callback(exchange.on_handler_done)

    cdef void _drop_pending(self):
        cdef RequestExchange exchange
        cdef object span
        cdef object req
        for exchange in self.pending_exchanges:
            if self.noop_span is None:
                span = exchange.span
                if span is not None:
                    req = exchange.req
                    try:
                        finish_request_span(
                            span,
                            method=req.method if req is not None else None,
                            path=req.path if req is not None else None,
                        )
                    except Exception:
                        pass
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
        if exchange._http2:
            self.h2_streams.pop(exchange._h2_stream_id, None)
            if (
                transport is not None
                and not transport.is_closing()
                and not self.h2_streams
                and self.reading_exchange is None
                and not self.pending_exchanges
                and not self.app.shutdown.done()
            ):
                self._arm_timeout(TIMEOUT_IDLE, self.keep_alive_timeout)
            return
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

    cdef void _finish_protocol_span(
        self,
        int status,
        object span,
        object method,
        object path,
    ) noexcept:
        if self.noop_span is not None:
            return
        try:
            if span is None and self.tracer is not None:
                span = self.tracer.create("request")
            finish_request_span(span, status=status, method=method, path=path)
        except Exception:
            pass

    cdef void _close_error(self, int status, object message) noexcept:
        cdef object transport
        cdef object body
        cdef object date
        cdef object span
        cdef object method
        cdef object path
        cdef Py_ssize_t qoff
        cdef Py_ssize_t qlen
        cdef RequestExchange exchange
        if self.rejected:
            return
        self.rejected = True
        self._cancel_timeout()
        transport = self.transport
        exchange = self.reading_exchange
        span = None
        method = None
        path = None
        qoff = 0
        qlen = 0
        if exchange is not None:
            span = exchange.span
            try:
                if exchange._req_url_length > 0:
                    path = _path_for_url(
                        exchange._req_arena + exchange._req_url_offset,
                        exchange._req_url_length,
                        &qoff,
                        &qlen,
                    )
                    if exchange._http2:
                        method = exchange._h2_method or METH_GET
                    elif self.parser != NULL:
                        method = _method_str(<int>llhttp_get_method(self.parser))
                elif self.request_dispatched and exchange.req is not None:
                    method = exchange.req.method
                    path = exchange.req.path
            except Exception:
                method = None
                path = None
        try:
            if transport is None or transport.is_closing():
                return
            if exchange is not None:
                exchange.c_abort()
                if not exchange.handler_started:
                    exchange.cancel_before_start()
            self._drop_pending()
            if self.parse_mode == PARSE_H2:
                self._finish_protocol_span(status, span, method, path)
                transport.close()
                return
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
            self._finish_protocol_span(status, span, method, path)
            transport.close()
        except Exception:
            try:
                if transport is not None:
                    transport.close()
            except Exception:
                pass

    # --- HTTP/2 ---

    cdef void _start_h2(self) noexcept:
        cdef nghttp2_session_callbacks* cbs = NULL
        cdef nghttp2_option* opt = NULL
        cdef nghttp2_settings_entry iv[3]
        cdef int rv
        if self.h2 != NULL:
            self.parse_mode = PARSE_H2
            return
        self.parse_mode = PARSE_H2
        rv = nghttp2_session_callbacks_new(&cbs)
        if rv != 0:
            self._close_error(500, "Internal Server Error")
            return
        nghttp2_session_callbacks_set_on_begin_headers_callback(cbs, _h2_on_begin_headers)
        nghttp2_session_callbacks_set_on_header_callback(cbs, _h2_on_header)
        nghttp2_session_callbacks_set_on_data_chunk_recv_callback(cbs, _h2_on_data)
        nghttp2_session_callbacks_set_on_frame_recv_callback(cbs, _h2_on_frame_recv)
        nghttp2_session_callbacks_set_on_stream_close_callback(cbs, _h2_on_stream_close)
        nghttp2_session_callbacks_set_send_data_callback(cbs, _h2_send_data)
        rv = nghttp2_option_new(&opt)
        if rv != 0:
            nghttp2_session_callbacks_del(cbs)
            self._close_error(500, "Internal Server Error")
            return
        nghttp2_option_set_no_auto_window_update(opt, 1)
        rv = nghttp2_session_server_new2(&self.h2, cbs, <void*>self, opt)
        nghttp2_option_del(opt)
        nghttp2_session_callbacks_del(cbs)
        if rv != 0:
            self.h2 = NULL
            self._close_error(500, "Internal Server Error")
            return
        iv[0].settings_id = NGHTTP2_SETTINGS_INITIAL_WINDOW_SIZE
        iv[0].value = <uint32_t>H2_STREAM_WINDOW
        iv[1].settings_id = NGHTTP2_SETTINGS_MAX_CONCURRENT_STREAMS
        iv[1].value = <uint32_t>H2_MAX_CONCURRENT
        iv[2].settings_id = NGHTTP2_SETTINGS_ENABLE_PUSH
        iv[2].value = 0
        nghttp2_submit_settings(self.h2, NGHTTP2_FLAG_NONE, iv, 3)
        nghttp2_session_set_local_window_size(
            self.h2, NGHTTP2_FLAG_NONE, 0, <int32_t>H2_CONN_WINDOW
        )
        self._h2_send()

    cdef void _h2_recv(self, const char* data, size_t n) noexcept:
        cdef ssize_t rv
        if self.h2 == NULL:
            return
        # Queue WINDOW_UPDATE / responses during this recv; one mem_send after
        # so multiplexed streams share a write instead of one flush each.
        self.h2_in_recv = True
        rv = nghttp2_session_mem_recv(self.h2, <const uint8_t*>data, n)
        self.h2_in_recv = False
        if rv < 0:
            self._close_error(400, "Invalid HTTP request")
            return
        self._h2_release_connection_window(True)
        self._h2_send()

    cdef void _h2_consume_stream(self, int32_t stream_id, bint force=False) noexcept:
        cdef int32_t unacked
        cdef int32_t local
        if self.h2 == NULL:
            return
        unacked = nghttp2_session_get_stream_effective_recv_data_length(
            self.h2, stream_id
        )
        if unacked <= 0:
            return
        if not force:
            local = nghttp2_session_get_stream_local_window_size(self.h2, stream_id)
            if unacked < H2_STREAM_UPDATE_THRESH and local >= H2_STREAM_UPDATE_THRESH:
                return
        # consume() only emits WINDOW_UPDATE at 50% of the local window.
        # Force-ack with submit so small keep-alive POSTs cannot stall.
        nghttp2_submit_window_update(
            self.h2, NGHTTP2_FLAG_NONE, stream_id, unacked
        )

    cdef void _h2_release_connection_window(self, bint force=False) noexcept:
        cdef int32_t unacked
        cdef int32_t local
        if self.h2 == NULL:
            return
        unacked = nghttp2_session_get_effective_recv_data_length(self.h2)
        if unacked <= 0:
            return
        if not force:
            local = nghttp2_session_get_local_window_size(self.h2)
            if unacked < H2_CONN_UPDATE_THRESH and local >= H2_CONN_UPDATE_THRESH:
                return
        nghttp2_submit_window_update(self.h2, NGHTTP2_FLAG_NONE, 0, unacked)

    cdef int _h2_out_append(self, const char* src, Py_ssize_t n) noexcept:
        cdef object buf
        cdef Py_ssize_t used
        cdef Py_ssize_t cap
        if n <= 0:
            return 0
        buf = self.h2_out
        if buf is None:
            buf = bytearray()
            self.h2_out = buf
        used = self.h2_out_used
        cap = PyByteArray_GET_SIZE(buf)
        if used + n > cap:
            cap = used + n
            if cap < 256:
                cap = 256
            if PyByteArray_Resize(buf, cap) < 0:
                PyErr_Clear()
                return -1
        memcpy(PyByteArray_AS_STRING(buf) + used, src, <size_t>n)
        self.h2_out_used = used + n
        return 0

    cdef void _h2_flush_out(self) noexcept:
        cdef object buf
        cdef Py_ssize_t used
        used = self.h2_out_used
        if used <= 0 or self.transport is None:
            self.h2_out_used = 0
            return
        buf = self.h2_out
        self.h2_out_used = 0
        self.transport.write(
            PyBytes_FromStringAndSize(PyByteArray_AS_STRING(buf), used)
        )

    cdef void _h2_send(self) noexcept:
        cdef const uint8_t* data = NULL
        cdef ssize_t n
        if self.h2_in_recv:
            return
        if self.h2 == NULL or self.transport is None:
            return
        while True:
            n = nghttp2_session_mem_send(self.h2, &data)
            if n == 0:
                break
            if n < 0:
                self._close_error(400, "Invalid HTTP request")
                return
            if self._h2_out_append(<const char*>data, n) != 0:
                self._close_error(500, "Internal Server Error")
                return
        self._h2_flush_out()

    cdef RequestExchange _h2_stream_ex(self, int32_t stream_id):
        cdef void* ptr
        if self.h2 != NULL:
            ptr = nghttp2_session_get_stream_user_data(self.h2, stream_id)
            if ptr != NULL:
                return <RequestExchange>ptr
        return self.h2_streams.get(stream_id)

    cdef void _h2_dispatch_stream(self, RequestExchange ex) noexcept:
        if ex is None or ex._h2_dispatched or self.rejected:
            return
        if self.transport is None or self.transport.is_closing():
            return
        try:
            self._dispatch(ex, self._build_request(ex, ex))
            ex._h2_dispatched = True
        except Exception:
            self._close_error(400, "Invalid HTTP request")

    cdef void _h2_begin_stream(self, int32_t stream_id):
        cdef RequestExchange ex
        if stream_id in self.h2_streams:
            return
        if self.idle_exchange is not None:
            ex = self.idle_exchange
            self.idle_exchange = None
            ex.reset(
                self,
                self.app,
                self.transport,
                self.date_box,
                self.compression,
                self.max_body_bytes,
                self.body_timeout,
            )
        else:
            ex = acquire_exchange(
                self,
                self.app,
                self.transport,
                self.date_box,
                self.compression,
                self.max_body_bytes,
                self.body_timeout,
            )
        ex.bind_http2(stream_id)
        self.h2_streams[stream_id] = ex
        if self.h2 != NULL:
            nghttp2_session_set_stream_user_data(self.h2, stream_id, <void*>ex)

    cdef void _h2_on_header(
        self,
        int32_t stream_id,
        const uint8_t* name,
        size_t namelen,
        const uint8_t* value,
        size_t valuelen,
    ) noexcept:
        cdef RequestExchange ex = self._h2_stream_ex(stream_id)
        cdef const char* hn = <const char*>name
        if ex is None or ex._h2_dispatched or ex._h2_headers_done:
            return
        if namelen > 0 and name[0] == 58:
            if namelen == 7 and memcmp(hn, ":method", 7) == 0:
                if valuelen == 7 and memcmp(<const char*>value, "CONNECT", 7) == 0:
                    self._h2_reject_stream(stream_id, NGHTTP2_PROTOCOL_ERROR)
                    return
                ex._h2_method = _method_from_bytes(<const char*>value, valuelen)
                return
            if namelen == 5 and memcmp(hn, ":path", 5) == 0:
                if ex.append_request_url(<const char*>value, valuelen) != 0:
                    self._close_error(431, "Request header fields too large")
                return
            if namelen == 10 and memcmp(hn, ":authority", 10) == 0:
                if ex.append_request_header("host", 4, <const char*>value, valuelen) != 0:
                    self._close_error(400, "Invalid HTTP request")
                return
            if namelen == 9 and memcmp(hn, ":protocol", 9) == 0:
                # Extended CONNECT / WebSocket — not supported.
                self._h2_reject_stream(stream_id, NGHTTP2_PROTOCOL_ERROR)
                return
            return
        if not _header_name_ok(hn, namelen):
            self._h2_reject_stream(stream_id, NGHTTP2_PROTOCOL_ERROR)
            return
        if ex.append_request_header(hn, namelen, <const char*>value, valuelen) != 0:
            self._close_error(400, "Invalid HTTP request")

    cdef void _h2_on_data(self, int32_t stream_id, const uint8_t* data, size_t length) noexcept:
        cdef RequestExchange ex = self._h2_stream_ex(stream_id)
        if ex is None or length == 0:
            return
        if not ex._body_active:
            ex.reset_body(False, -1)
        if ex.c_feed(<const char*>data, length) != 0:
            nghttp2_submit_rst_stream(
                self.h2, NGHTTP2_FLAG_NONE, stream_id, NGHTTP2_INTERNAL_ERROR
            )
            return
        self._h2_consume_stream(stream_id)
        if (
            not ex._h2_dispatched
            and not self.rejected
            and ex._total_read > SMALL_BODY_COMPLETE_DISPATCH
        ):
            self._h2_dispatch_stream(ex)

    cdef void _h2_headers_frame(self, int32_t stream_id, bint end_stream):
        cdef RequestExchange ex = self._h2_stream_ex(stream_id)
        cdef Py_ssize_t content_length
        if ex is None or self.rejected:
            return
        if ex._h2_dispatched or ex._h2_headers_done:
            # Trailers (or a later HEADERS) with END_STREAM finish the body.
            if end_stream:
                self._h2_end_stream(stream_id)
            return
        ex.finish_request_header()
        ex.cache_hot_request_headers()
        content_length = ex._req_content_length
        if content_length > self.max_body_bytes:
            self._h2_reject_stream(stream_id, NGHTTP2_PROTOCOL_ERROR)
            return
        try:
            if ex._req_expect_continue or content_length > 0:
                ex.reset_body(ex._req_expect_continue, content_length)
                ex._h2_headers_done = True
                if end_stream and ex._body_active:
                    ex.c_complete()
                if (
                    not ex._req_expect_continue
                    and content_length > 0
                    and content_length <= SMALL_BODY_COMPLETE_DISPATCH
                    and not end_stream
                ):
                    return
            else:
                ex.mark_nobody()
                ex._h2_headers_done = True
            if not ex._h2_dispatched and not self.rejected:
                self._h2_dispatch_stream(ex)
        except Exception:
            self._close_error(400, "Invalid HTTP request")

    cdef void _h2_end_stream(self, int32_t stream_id):
        cdef RequestExchange ex = self._h2_stream_ex(stream_id)
        # Return recv credit for this stream (and the connection share).
        # Batching alone never fires for small POSTs; without this, keep-alive
        # stalls once the connection window is exhausted.
        self._h2_consume_stream(stream_id, True)
        if ex is None:
            return
        if ex._body_active and not ex._body_complete:
            ex.c_complete()
        if not ex._h2_dispatched and not self.rejected:
            if not ex._h2_headers_done:
                self._h2_headers_frame(stream_id, True)
                return
            self._h2_dispatch_stream(ex)

    cdef void _h2_stream_closed(self, int32_t stream_id):
        cdef RequestExchange ex
        if self.h2 != NULL:
            nghttp2_session_set_stream_user_data(self.h2, stream_id, NULL)
        ex = self.h2_streams.pop(stream_id, None)
        if ex is None:
            return
        # Client RST or GOAWAY: stop the handler; recycle if it never started.
        if not ex._completed:
            ex.c_abort()
            if not ex.handler_started:
                ex.cancel_before_start()

    cdef void _h2_reject_stream(self, int32_t stream_id, uint32_t error_code) noexcept:
        cdef RequestExchange ex = self.h2_streams.get(stream_id)
        if ex is not None:
            ex._h2_dispatched = True
        if self.h2 != NULL:
            nghttp2_submit_rst_stream(self.h2, NGHTTP2_FLAG_NONE, stream_id, error_code)

    cdef ssize_t _h2_read_data(
        self,
        RequestExchange ex,
        uint8_t* buf,
        size_t length,
        uint32_t* data_flags,
    ) noexcept:
        cdef Py_ssize_t avail
        cdef Py_ssize_t take
        cdef object pending
        if ex is None:
            data_flags[0] |= NGHTTP2_DATA_FLAG_EOF
            return 0
        pending = ex._h2_pending
        if pending is None or pending == b"":
            avail = 0
        elif PyByteArray_Check(pending):
            avail = PyByteArray_GET_SIZE(pending) - ex._h2_pending_off
        elif PyBytes_Check(pending):
            avail = PyBytes_GET_SIZE(pending) - ex._h2_pending_off
        else:
            avail = 0
        if avail <= 0:
            if ex._h2_body_done:
                data_flags[0] |= NGHTTP2_DATA_FLAG_EOF
                return 0
            return NGHTTP2_ERR_DEFERRED
        take = avail if avail < <Py_ssize_t>length else <Py_ssize_t>length
        # Payload stays in _h2_pending; send_data appends it into h2_out.
        data_flags[0] |= NGHTTP2_DATA_FLAG_NO_COPY
        if take == avail and ex._h2_body_done:
            data_flags[0] |= NGHTTP2_DATA_FLAG_EOF
        return <ssize_t>take

    cdef int _h2_send_data_frame(
        self,
        nghttp2_frame* frame,
        const uint8_t* framehd,
        size_t length,
        nghttp2_data_source* source,
    ) noexcept:
        cdef RequestExchange ex
        cdef object pending
        cdef Py_ssize_t off
        cdef Py_ssize_t n
        cdef const char* src
        if source == NULL or source.ptr == NULL:
            return NGHTTP2_ERR_CALLBACK_FAILURE
        # We never ask nghttp2 to pad DATA; a padlen would need extra writes.
        if frame != NULL and frame.data.padlen > 0:
            return NGHTTP2_ERR_CALLBACK_FAILURE
        ex = <RequestExchange>source.ptr
        if self._h2_out_append(<const char*>framehd, 9) != 0:
            return NGHTTP2_ERR_CALLBACK_FAILURE
        if length == 0:
            return 0
        pending = ex._h2_pending
        off = ex._h2_pending_off
        if PyBytes_Check(pending):
            n = PyBytes_GET_SIZE(pending)
            src = PyBytes_AS_STRING(pending) + off
        elif PyByteArray_Check(pending):
            n = PyByteArray_GET_SIZE(pending)
            src = PyByteArray_AS_STRING(pending) + off
        else:
            return NGHTTP2_ERR_CALLBACK_FAILURE
        if off < 0 or off + <Py_ssize_t>length > n:
            return NGHTTP2_ERR_CALLBACK_FAILURE
        if self._h2_out_append(src, <Py_ssize_t>length) != 0:
            return NGHTTP2_ERR_CALLBACK_FAILURE
        ex._h2_pending_off = off + <Py_ssize_t>length
        if ex._h2_pending_off >= n:
            ex._h2_pending = b""
            ex._h2_pending_off = 0
        return 0

    cdef int _h2_fill_nva(self, object pairs, nghttp2_nv* nvs, size_t maxn) except -1:
        cdef Py_ssize_t i, n, nn, vn
        cdef bytes name, value
        cdef bint trim
        n = len(pairs)
        if n > <Py_ssize_t>maxn:
            n = <Py_ssize_t>maxn
        for i in range(n):
            name = pairs[i][0]
            value = pairs[i][1]
            trim = len(pairs[i]) > 2 and pairs[i][2]
            nn = PyBytes_GET_SIZE(name)
            vn = PyBytes_GET_SIZE(value)
            if trim:
                if nn >= 2:
                    nn -= 2
                if vn >= 2:
                    vn -= 2
            nvs[i].name = <uint8_t*>PyBytes_AS_STRING(name)
            nvs[i].namelen = <size_t>nn
            nvs[i].value = <uint8_t*>PyBytes_AS_STRING(value)
            nvs[i].valuelen = <size_t>vn
            nvs[i].flags = H2_NV_NOCOPY
        return <int>n

    cdef int _h2_fill_user_nvs(
        self,
        Headers h,
        nghttp2_nv* nvs,
        int cap,
        bint skip_ce,
        bint skip_cl,
        bint skip_ct,
    ) except -1:
        cdef Py_ssize_t i
        cdef Py_ssize_t n
        cdef object nb
        cdef object vb
        cdef char* np
        cdef char* vp
        cdef Py_ssize_t nl
        cdef Py_ssize_t vl
        cdef int filled = 0
        if h is None or cap <= 0:
            return 0
        n = h._n
        for i in range(n):
            nb = h._names[i]
            vb = h._values[i]
            np = PyBytes_AS_STRING(nb)
            nl = PyBytes_GET_SIZE(nb)
            vp = PyBytes_AS_STRING(vb)
            vl = PyBytes_GET_SIZE(vb)
            if skip_ct and nl == 14 and memcmp(np, "content-type: ", 14) == 0:
                continue
            if skip_cl and nl == 16 and memcmp(np, "content-length: ", 16) == 0:
                continue
            if skip_ce and nl == 18 and memcmp(np, "content-encoding: ", 18) == 0:
                continue
            if nl == 6 and memcmp(np, "date: ", 6) == 0:
                continue
            if nl == 19 and memcmp(np, "transfer-encoding: ", 19) == 0:
                continue
            if nl >= 2:
                nl -= 2
            if vl >= 2:
                vl -= 2
            if filled >= cap:
                break
            nvs[filled].name = <uint8_t*>np
            nvs[filled].namelen = <size_t>nl
            nvs[filled].value = <uint8_t*>vp
            nvs[filled].valuelen = <size_t>vl
            nvs[filled].flags = H2_NV_NOCOPY
            filled += 1
        return filled

    def h2_respond(
        self,
        RequestExchange ex,
        object nva,
        object body,
        bint skip_ce=False,
        bint skip_cl=True,
        bint skip_ct=True,
    ):
        cdef nghttp2_nv nvs[64]
        cdef int nvlen
        cdef nghttp2_data_provider prd
        cdef object payload
        cdef int rv
        if self.h2 == NULL or ex is None:
            return
        nvlen = self._h2_fill_nva(nva, nvs, 64)
        nvlen += self._h2_fill_user_nvs(
            ex.headers, nvs + nvlen, 64 - nvlen, skip_ce, skip_cl, skip_ct
        )
        if body is None:
            payload = b""
        elif PyBytes_Check(body) or PyByteArray_Check(body):
            payload = body
        else:
            payload = bytes(body)
        if payload:
            ex._h2_pending = payload
            ex._h2_pending_off = 0
            ex._h2_body_done = True
            prd.source.ptr = <void*>ex
            prd.read_callback = _h2_data_source_read
            rv = nghttp2_submit_response(
                self.h2, ex._h2_stream_id, nvs, <size_t>nvlen, &prd
            )
        else:
            ex._h2_body_done = True
            rv = nghttp2_submit_response(
                self.h2, ex._h2_stream_id, nvs, <size_t>nvlen, NULL
            )
        if rv != 0:
            self._close_error(400, "Invalid HTTP request")
            return
        self._h2_send()

    def h2_write_headers(
        self,
        RequestExchange ex,
        object nva,
        bint skip_ce=False,
        bint skip_cl=False,
        bint skip_ct=False,
        bint skip_user=False,
    ):
        cdef nghttp2_nv nvs[64]
        cdef int nvlen
        cdef nghttp2_data_provider prd
        cdef int rv
        if self.h2 == NULL or ex is None:
            return
        nvlen = self._h2_fill_nva(nva, nvs, 64)
        if not skip_user:
            nvlen += self._h2_fill_user_nvs(
                ex.headers, nvs + nvlen, 64 - nvlen, skip_ce, skip_cl, skip_ct
            )
        if nva and nva[0][0] == b":status" and nva[0][1] == b"100":
            nghttp2_submit_headers(
                self.h2, NGHTTP2_FLAG_NONE, ex._h2_stream_id, NULL, nvs, <size_t>nvlen, NULL
            )
            self._h2_send()
            return
        if ex._h2_headers_sent:
            return
        ex._h2_headers_sent = True
        prd.source.ptr = <void*>ex
        prd.read_callback = _h2_data_source_read
        rv = nghttp2_submit_response(self.h2, ex._h2_stream_id, nvs, <size_t>nvlen, &prd)
        if rv != 0:
            self._close_error(400, "Invalid HTTP request")
            return
        self._h2_send()

    cdef void _h2_pending_extend(self, RequestExchange ex, object data) noexcept:
        cdef object pending = ex._h2_pending
        cdef Py_ssize_t off
        cdef Py_ssize_t keep
        cdef Py_ssize_t n
        cdef char* dst
        cdef const char* src
        if pending == b"" or pending is None:
            if PyBytes_Check(data) or PyByteArray_Check(data):
                ex._h2_pending = data
            else:
                pending = bytearray()
                pending.extend(data)
                ex._h2_pending = pending
            ex._h2_pending_off = 0
            return
        off = ex._h2_pending_off
        if not PyByteArray_Check(pending):
            n = PyBytes_GET_SIZE(pending) if PyBytes_Check(pending) else 0
            keep = n - off if n > off else 0
            pending = bytearray()
            if keep:
                if PyByteArray_Resize(pending, keep) < 0:
                    PyErr_Clear()
                    return
                memcpy(
                    PyByteArray_AS_STRING(pending),
                    PyBytes_AS_STRING(ex._h2_pending) + off,
                    <size_t>keep,
                )
            ex._h2_pending = pending
            ex._h2_pending_off = 0
            pending.extend(data)
            return
        if off:
            n = PyByteArray_GET_SIZE(pending)
            keep = n - off if n > off else 0
            if keep:
                memmove(
                    PyByteArray_AS_STRING(pending),
                    PyByteArray_AS_STRING(pending) + off,
                    <size_t>keep,
                )
            if PyByteArray_Resize(pending, keep) < 0:
                PyErr_Clear()
                return
            ex._h2_pending_off = 0
        pending.extend(data)

    def h2_write_data(self, RequestExchange ex, object data, bint end):
        if self.h2 == NULL or ex is None:
            return
        if data:
            self._h2_pending_extend(ex, data)
        if end:
            ex._h2_body_done = True
        nghttp2_session_resume_data(self.h2, ex._h2_stream_id)
        self._h2_send()

    def h2_end(self, RequestExchange ex):
        if self.h2 == NULL or ex is None:
            return
        ex._h2_body_done = True
        nghttp2_session_resume_data(self.h2, ex._h2_stream_id)
        self._h2_send()

    def h2_abort(self, RequestExchange ex):
        if self.h2 == NULL or ex is None:
            return
        nghttp2_submit_rst_stream(
            self.h2, NGHTTP2_FLAG_NONE, ex._h2_stream_id, NGHTTP2_INTERNAL_ERROR
        )
        self._h2_send()
