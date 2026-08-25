# cython: language_level=3
"""Globally pooled state for one live request handler and its response."""

import asyncio

from libc.stddef cimport size_t
from libc.stdio cimport sprintf
from libc.stdlib cimport free, realloc
from libc.string cimport memcpy, memmove
from cpython.bytearray cimport (
    PyByteArray_AS_STRING,
    PyByteArray_GET_SIZE,
    PyByteArray_Resize,
)
from cpython.bytes cimport PyBytes_FromStringAndSize

from stario.exceptions import (
    ClientDisconnected,
    HttpException,
    StarioError,
    StarioRuntime,
)
from stario.http.compression import (
    DEFAULT_BROTLI_LEVEL,
    DEFAULT_GZIP_LEVEL,
    DEFAULT_MIN_SIZE,
    content_type_is_compressible,
    negotiate_content_encoding,
)
from stario.http.context import EMPTY_ROUTE_MATCH, _Alive
from stario.http.writer import get_status_line

from stario_cython.compression_buf cimport (
    stario_brotli_block_borrowed,
    stario_brotli_finish_borrowed,
    stario_brotli_free,
    stario_brotli_new,
    stario_gzip_block_borrowed,
    stario_gzip_finish_borrowed,
    stario_gzip_free,
    stario_gzip_new,
)
from stario_cython.headers cimport Headers
from stario_cython.request cimport Request

# Stream backpressure window. max_chunk must be strictly below HIGH_WATER.
cdef int LOW_WATER = 64 * 1024
cdef int HIGH_WATER = 256 * 1024
# Default stream() yield size — keep bytes in _acc until this big (or message done).
cdef int DEFAULT_STREAM_CHUNK = 64 * 1024
cdef double DEFAULT_TIMEOUT = 30.0
cdef int POOL_MAX = 1024

cdef int CONSUMED_NONE = 0
cdef int CONSUMED_BODY = 1
cdef int CONSUMED_STREAM = 2

cdef int ABORT_NONE = 0
cdef int ABORT_TOO_LARGE = 1
cdef int ABORT_DISCONNECTED = 2
cdef int ABORT_TIMEOUT = 3

cdef bytes STATUS_200 = b"HTTP/1.1 200 OK\r\n"
cdef bytes STATUS_204 = b"HTTP/1.1 204 No Content\r\n"
cdef bytes STATUS_304 = b"HTTP/1.1 304 Not Modified\r\n"
cdef bytes STATUS_400 = b"HTTP/1.1 400 Bad Request\r\n"
cdef bytes STATUS_404 = b"HTTP/1.1 404 Not Found\r\n"
cdef bytes STATUS_405 = b"HTTP/1.1 405 Method Not Allowed\r\n"
cdef bytes STATUS_413 = b"HTTP/1.1 413 Payload Too Large\r\n"
cdef bytes STATUS_431 = b"HTTP/1.1 431 Request Header Fields Too Large\r\n"
cdef bytes STATUS_500 = b"HTTP/1.1 500 Internal Server Error\r\n"
cdef bytes ZERO_CL = b"content-length: 0\r\n\r\n"
cdef bytes CT_PREFIX = b"content-type: "
cdef bytes CL_PREFIX = b"\r\ncontent-length: "
cdef bytes CRLF2 = b"\r\n\r\n"
cdef bytes CRLF = b"\r\n"
cdef bytes CHUNK_END = b"0\r\n\r\n"

cdef object STARTED_ERROR = (
    "Response already started (headers sent). "
    "Set headers via w.headers.set() before the first write or one-shot respond()."
)

cdef list _POOL = []
cdef object _UNBOUND = object()


cdef inline bint _may_have_body(int status) noexcept:
    if status == 204 or status == 304:
        return False
    return not (100 <= status < 200)


cdef object _status_line(int status):
    if status == 200:
        return STATUS_200
    if status == 204:
        return STATUS_204
    if status == 304:
        return STATUS_304
    if status == 400:
        return STATUS_400
    if status == 404:
        return STATUS_404
    if status == 405:
        return STATUS_405
    if status == 413:
        return STATUS_413
    if status == 431:
        return STATUS_431
    if status == 500:
        return STATUS_500
    return get_status_line(status)


cdef object _dec(size_t n):
    cdef char buf[16]
    cdef int i = sprintf(buf, "%zu", n)
    return PyBytes_FromStringAndSize(buf, i)


cdef class RequestExchange:
    def __cinit__(self):
        self._brotli = NULL
        self._gzip = NULL
        self._out_buf = None
        self._out_hold = None
        self._out_len = 0
        self._status_code = -1
        self._declared_length = -1
        self._bytes_written = 0
        self._completed = False
        self._date_box = None
        self._acc = NULL
        self._acc_len = 0
        self._acc_cap = 0
        self._acc_pos = 0
        self._expected_body = -1

    def __init__(self):
        self.headers = Headers()
        self.request_headers = Headers()
        self.req = Request()
        self._chunks = None
        self._cached = None
        self._data_ready = None
        self._stall_handle = None
        self._compression = _UNBOUND
        self._available = ()
        self._compress_min_size = DEFAULT_MIN_SIZE
        self._compress_enabled = False
        self._state = None
        self.in_pool = False
        self._body_active = False
        self._read_max_size = -1
        self._stream_max_chunk = DEFAULT_STREAM_CHUNK

    def __dealloc__(self):
        self._free_compressors()
        if self._acc != NULL:
            free(self._acc)
            self._acc = NULL

    cdef void reset_response(self, object accept_encoding):
        self._free_compressors()
        self._accept = accept_encoding
        self._status_code = -1
        self._declared_length = -1
        self._bytes_written = 0
        self._completed = False
        self._out_len = 0
        self.headers.c_clear()

    cdef void _apply_compression(self, object compression):
        cdef object window
        self._brotli_level = DEFAULT_BROTLI_LEVEL
        self._brotli_window = 0
        self._gzip_level = DEFAULT_GZIP_LEVEL
        self._available = ()
        self._compress_min_size = DEFAULT_MIN_SIZE
        self._compress_enabled = False
        if compression is None:
            return
        # Snapshot user CompressionConfig onto C fields so hot paths don't
        # re-enter Python for min_size / enabled-encodings on every respond().
        self._brotli_level = compression.brotli_level
        window = compression.brotli_window_log
        self._brotli_window = 0 if window is None else window
        self._gzip_level = compression.gzip_level
        self._compress_min_size = compression.min_size
        self._available = tuple(
            enc
            for enc in compression.enabled_encodings()
            if enc == b"br" or enc == b"gzip"
        )
        self._compress_enabled = len(self._available) > 0

    cdef int _ensure_brotli(self) except -1:
        if self._brotli != NULL:
            return 0
        self._brotli = stario_brotli_new(self._brotli_level, self._brotli_window)
        if self._brotli == NULL:
            raise StarioError("brotli stream init failed")
        return 0

    cdef int _ensure_gzip(self) except -1:
        if self._gzip != NULL:
            return 0
        self._gzip = stario_gzip_new(self._gzip_level)
        if self._gzip == NULL:
            raise StarioError("gzip stream init failed")
        return 0

    cdef void _free_compressors(self):
        if self._brotli != NULL:
            stario_brotli_free(self._brotli)
            self._brotli = NULL
        if self._gzip != NULL:
            stario_gzip_free(self._gzip)
            self._gzip = NULL

    cdef int _buf_add(self, const char* src, Py_ssize_t n) except -1:
        cdef bytearray buf
        cdef Py_ssize_t need
        cdef Py_ssize_t cap
        cdef Py_ssize_t next_cap
        if n <= 0:
            return 0
        if self._out_buf is None:
            self._out_buf = bytearray(256)
        buf = self._out_buf
        need = self._out_len + n
        cap = PyByteArray_GET_SIZE(buf)
        if need > cap:
            next_cap = cap * 2 if cap else 256
            if next_cap < need:
                next_cap = need
            if PyByteArray_Resize(buf, next_cap) < 0:
                raise MemoryError()
        memcpy(PyByteArray_AS_STRING(buf) + self._out_len, src, <size_t>n)
        self._out_len = need
        return 0

    cdef int _buf_bytes(self, object data) except -1:
        cdef Py_ssize_t n = len(data)
        cdef const char* p
        if n == 0:
            return 0
        p = data
        return self._buf_add(p, n)

    cdef Py_ssize_t _body_nbytes(self, object body) except -2:
        """Total byte length of ``bytes`` or a list/tuple of bytes-like parts."""
        cdef Py_ssize_t total
        cdef object part
        if body is None:
            return 0
        if isinstance(body, (bytes, bytearray, memoryview)):
            return <Py_ssize_t>len(body)
        if isinstance(body, (list, tuple)):
            total = 0
            for part in body:
                if not isinstance(part, (bytes, bytearray, memoryview)):
                    raise TypeError(
                        "body parts must be bytes-like, got "
                        + type(part).__name__
                    )
                total += <Py_ssize_t>len(part)
            return total
        raise TypeError(
            "body must be bytes-like or a list/tuple of bytes-like objects"
        )

    cdef object _body_as_bytes(self, object body):
        """Contiguous bytes for one-shot compression (joins list/tuple once)."""
        if body is None:
            return b""
        if isinstance(body, bytes):
            return body
        if isinstance(body, (bytearray, memoryview)):
            return bytes(body)
        if isinstance(body, (list, tuple)):
            if not body:
                return b""
            if len(body) == 1 and isinstance(body[0], bytes):
                return body[0]
            return b"".join(body)
        raise TypeError(
            "body must be bytes-like or a list/tuple of bytes-like objects"
        )

    cdef int _buf_body(self, object body) except -1:
        """Append body bytes or each list/tuple part into ``_out_buf`` (no join)."""
        cdef object part
        if body is None:
            return 0
        if isinstance(body, (bytes, bytearray, memoryview)):
            return self._buf_bytes(body)
        if isinstance(body, (list, tuple)):
            for part in body:
                if not isinstance(part, (bytes, bytearray, memoryview)):
                    raise TypeError(
                        "body parts must be bytes-like, got "
                        + type(part).__name__
                    )
                self._buf_bytes(part)
            return 0
        raise TypeError(
            "body must be bytes-like or a list/tuple of bytes-like objects"
        )

    cdef int _write_body_raw(self, object body) except -1:
        """Write body parts directly to the transport (known Content-Length)."""
        cdef object part
        if body is None:
            return 0
        if isinstance(body, (bytes, bytearray, memoryview)):
            if len(body):
                self._transport.write(body)
            return 0
        if isinstance(body, (list, tuple)):
            for part in body:
                if not isinstance(part, (bytes, bytearray, memoryview)):
                    raise TypeError(
                        "body parts must be bytes-like, got "
                        + type(part).__name__
                    )
                if len(part):
                    self._transport.write(part)
            return 0
        raise TypeError(
            "body must be bytes-like or a list/tuple of bytes-like objects"
        )

    cdef int _buf_uint(self, size_t n, int base) except -1:
        cdef char tmp[16]
        cdef int i
        if base == 16:
            i = sprintf(tmp, "%x", <unsigned int>n)
        else:
            i = sprintf(tmp, "%zu", n)
        return self._buf_add(tmp, i)

    cdef void _flush(self):
        cdef object view
        cdef object done
        if self._out_len == 0:
            return
        done = self._out_buf
        self._out_buf = self._out_hold
        self._out_hold = done
        if self._out_buf is None:
            self._out_buf = bytearray(256)
        view = memoryview(done)[:self._out_len]
        self._out_len = 0
        self._transport.write(view)

    @property
    def status_code(self):
        if self._status_code < 0:
            return None
        return self._status_code

    @property
    def started(self):
        return self._status_code >= 0

    @property
    def completed(self):
        return self._completed

    cdef bint _may_compress(self, object data, object content_type, bint streaming):
        cdef Py_ssize_t n
        # Snapshotted from CompressionConfig in _apply_compression — respects
        # user-defined min_size / enabled encodings without a Python round-trip
        # on every tiny respond().
        if not self._compress_enabled or self._accept is None:
            return False
        if content_type is not None and not content_type_is_compressible(content_type):
            return False
        if not streaming:
            if data is None:
                return False
            n = self._body_nbytes(data)
            if n < self._compress_min_size:
                return False
        return True

    cdef int _frame(
        self,
        object data,
        object encoding,
        const unsigned char** out,
        size_t* out_len,
    ) except -1:
        cdef const char* ptr = data
        cdef size_t n = <size_t>len(data)
        if encoding == b"br":
            self._ensure_brotli()
            if stario_brotli_finish_borrowed(
                self._brotli, <const unsigned char*>ptr, n, out, out_len
            ) != 0:
                raise StarioError("brotli compression failed")
            return 0
        self._ensure_gzip()
        if stario_gzip_finish_borrowed(
            self._gzip, <const unsigned char*>ptr, n, out, out_len
        ) != 0:
            raise StarioError("gzip compression failed")
        return 0

    cdef int _block(self, object data, const unsigned char** out, size_t* out_len) except -1:
        cdef const char* ptr = data
        cdef size_t n = <size_t>len(data)
        if self._brotli != NULL:
            if stario_brotli_block_borrowed(
                self._brotli, <const unsigned char*>ptr, n, out, out_len
            ) != 0:
                raise StarioError("brotli stream failed")
        elif self._gzip != NULL:
            if stario_gzip_block_borrowed(
                self._gzip, <const unsigned char*>ptr, n, out, out_len
            ) != 0:
                raise StarioError("gzip stream failed")
        else:
            return 0
        if out_len[0] == 0:
            raise StarioError("compression flush produced no output")
        return 1

    cdef int _finish(self, const unsigned char** out, size_t* out_len) except -1:
        if self._brotli != NULL:
            if stario_brotli_finish_borrowed(
                self._brotli, NULL, 0, out, out_len
            ) != 0:
                raise StarioError("brotli finish failed")
            return 1
        if self._gzip != NULL:
            if stario_gzip_finish_borrowed(
                self._gzip, NULL, 0, out, out_len
            ) != 0:
                raise StarioError("gzip finish failed")
            return 1
        return 0

    cdef int _write_native_chunk(
        self, const unsigned char* data, size_t n
    ) except -1:
        if n == 0:
            return 0
        self._buf_uint(n, 16)
        self._buf_bytes(CRLF)
        self._buf_add(<const char*>data, <Py_ssize_t>n)
        self._buf_bytes(CRLF)
        self._flush()
        return 0

    cdef object _select(self, object data, object content_type, bint streaming):
        if not self._may_compress(data, content_type, streaming):
            return None
        return negotiate_content_encoding(self._accept, self._available)

    cdef void reset(
        self,
        object connection,
        object app,
        object transport,
        list date_box,
        object compression,
        int max_body_size,
    ):
        self.in_pool = False
        if self._connection is not connection:
            self._connection = connection
            self.app = app
            self._transport = transport
            self._date_box = date_box
            self._max_size = max_body_size
            self._timeout = DEFAULT_TIMEOUT
            if self._compression is not compression:
                self._compression = compression
                self._apply_compression(compression)
        self.span = None
        self.route = EMPTY_ROUTE_MATCH
        self._state = None
        self.request_headers.c_clear()
        self._req_accept_encoding = None
        self._req_connection = None
        self._req_expect = None
        self._req_content_type = None
        self._req_host = None
        self._req_expect_continue = False
        self._req_connection_close = False
        self._resp_connection_close = False
        self.handler_done = False
        self.handler_started = False

    cdef void cache_hot_request_headers(self):
        """Snapshot hot request headers for protocol/compression (dict API unchanged)."""
        cdef Headers h = self.request_headers
        cdef object v
        self._req_accept_encoding = h.c_get(b"accept-encoding")
        v = h.c_get(b"connection")
        if v is None:
            self._req_connection = None
            self._req_connection_close = False
        else:
            self._req_connection = v.lower()
            self._req_connection_close = self._req_connection == b"close"
        v = h.c_get(b"expect")
        if v is None:
            self._req_expect = None
            self._req_expect_continue = False
        else:
            self._req_expect = v.lower()
            self._req_expect_continue = self._req_expect == b"100-continue"
        self._req_content_type = h.c_get(b"content-type")
        self._req_host = h.c_get(b"host")

    cdef void start_response(self):
        self.handler_started = True
        self._resp_connection_close = False
        self.reset_response(self._req_accept_encoding)

    cdef void handler_finished(self):
        self.handler_done = True
        self._maybe_recycle()

    cdef void cancel_before_start(self):
        if self.handler_started or self.in_pool:
            return
        self.c_abort()
        self._completed = True
        self.handler_done = True
        self._maybe_recycle()

    cdef void _maybe_recycle(self):
        if (
            not self.in_pool
            and self._completed
            and self.handler_done
            and (not self._body_active or self._body_complete)
        ):
            self._connection.recycle_exchange(self)

    cdef void park(self):
        if self.in_pool:
            return
        self.in_pool = True
        self._cached = None
        self._data_ready = None
        self._cancel_stall_timer()
        self._acc_len = 0
        self._acc_pos = 0
        self._body_active = False
        self._read_max_size = -1
        self._req_accept_encoding = None
        self._req_connection = None
        self._req_expect = None
        self._req_content_type = None
        self._req_host = None
        self._req_expect_continue = False
        self._req_connection_close = False
        self._resp_connection_close = False
        self._stream_max_chunk = DEFAULT_STREAM_CHUNK
        if self._chunks is not None:
            self._chunks.clear()

    cdef void release_global(self):
        self._free_compressors()
        self.headers.c_clear()
        self.request_headers.c_clear()
        self.req.reset("GET", "/", b"", "1.1", True, None, None)
        self.span = None
        self.route = EMPTY_ROUTE_MATCH
        self._state = None
        self._accept = None
        self._req_accept_encoding = None
        self._req_connection = None
        self._req_expect = None
        self._req_content_type = None
        self._req_host = None
        self._req_expect_continue = False
        self._req_connection_close = False
        self._resp_connection_close = False
        self.app = None
        self._connection = None
        self._transport = None
        if len(_POOL) < POOL_MAX:
            _POOL.append(self)

    cdef void _done(self):
        self._connection.response_completed(self)
        self._maybe_recycle()

    def respond(self, body, content_type, int status=200):
        cdef Headers h = self.headers
        cdef object encoding
        cdef object flat
        cdef Py_ssize_t nbytes
        cdef const unsigned char* native_out = NULL
        cdef size_t native_len = 0
        if self._transport.is_closing():
            if not self._completed:
                self._completed = True
                self._done()
            return
        if self._status_code >= 0:
            raise StarioRuntime(
                STARTED_ERROR,
                help_text=(
                    "Send the response once: use respond(), or write_headers() "
                    "then write()/end()."
                ),
            )
        if not _may_have_body(status):
            body = b""
            nbytes = 0
        else:
            nbytes = self._body_nbytes(body)
        self._declared_length = nbytes
        self._bytes_written = 0
        # Empty custom headers + no compression: writelines of existing buffers.
        # Avoids (a) b"".join copying the entity and (b) _out_buf/memoryview churn
        # that regressed tiny plaintext/json vs the old join path.
        if h.c_empty() and (
            not _may_have_body(status)
            or not self._may_compress(body, content_type, False)
        ):
            if not _may_have_body(status):
                self._transport.writelines(
                    (_status_line(status), self._date_box[0], ZERO_CL)
                )
            else:
                flat = [
                    _status_line(status),
                    self._date_box[0],
                    CT_PREFIX,
                    content_type,
                    CL_PREFIX,
                    _dec(<size_t>nbytes),
                    CRLF2,
                ]
                if isinstance(body, (list, tuple)):
                    flat.extend(body)
                else:
                    flat.append(body)
                self._transport.writelines(flat)
        else:
            if not _may_have_body(status):
                body = b""
                h.c_set(b"content-length", b"0")
            elif h.c_get(b"content-encoding") is None:
                encoding = self._select(body, content_type, False)
                if encoding is not None:
                    # One-shot compressors need contiguous input; join list once.
                    flat = self._body_as_bytes(body)
                    try:
                        self._frame(flat, encoding, &native_out, &native_len)
                        h.c_set(b"content-encoding", encoding)
                        h.c_merge_vary(b"accept-encoding")
                        h.c_set(b"content-type", content_type)
                        h.c_set(b"content-length", _dec(native_len))
                        self._declared_length = <Py_ssize_t>native_len
                        self._bytes_written = 0
                        self._buf_bytes(_status_line(status))
                        self._buf_bytes(self._date_box[0])
                        if self._out_buf is None:
                            self._out_buf = bytearray(256)
                        h.c_write_wire_ba(self._out_buf, &self._out_len)
                        self._buf_bytes(CRLF)
                        self._buf_add(<const char*>native_out, <Py_ssize_t>native_len)
                        self._flush()
                    finally:
                        self._free_compressors()
                    self._status_code = status
                    self._bytes_written = self._declared_length
                    self._completed = True
                    self._done()
                    return
            h.c_set(b"content-type", content_type)
            h.c_set(b"content-length", _dec(<size_t>nbytes))
            self._declared_length = nbytes
            self._bytes_written = 0
            # Custom headers: one _out_buf assemble + flush (headers + body parts).
            self._buf_bytes(_status_line(status))
            self._buf_bytes(self._date_box[0])
            if self._out_buf is None:
                self._out_buf = bytearray(256)
            h.c_write_wire_ba(self._out_buf, &self._out_len)
            self._buf_bytes(CRLF)
            self._buf_body(body)
            self._flush()
        self._status_code = status
        self._bytes_written = self._declared_length if self._declared_length > 0 else 0
        self._completed = True
        self._done()

    def abort(self):
        if self._completed:
            return
        self._free_compressors()
        self._completed = True
        self._resp_connection_close = True
        self.headers.c_set(b"connection", b"close")
        self._transport.close()
        self._done()

    def write_headers(self, int status_code):
        cdef Headers headers = self.headers
        cdef object raw_length
        cdef object parsed_length
        cdef object encoding
        if self._transport.is_closing():
            return self
        if self._status_code >= 0:
            raise StarioRuntime(
                STARTED_ERROR,
                help_text=(
                    "Send the response once: use respond(), or write_headers() "
                    "then write()/end()."
                ),
            )
        if not _may_have_body(status_code):
            headers.c_remove(b"transfer-encoding")
            headers.c_set(b"content-length", b"0")
            self._declared_length = 0
            self._bytes_written = 0
        elif headers.c_get(b"content-length") is not None:
            headers.c_remove(b"transfer-encoding")
            raw_length = headers.c_get(b"content-length")
            try:
                parsed_length = int(raw_length)
                if parsed_length < 0:
                    raise ValueError()
                self._declared_length = parsed_length
                self._bytes_written = 0
            except (TypeError, ValueError, OverflowError) as exc:
                raise StarioError(
                    "Invalid Content-Length header",
                    context={"content-length": raw_length},
                    help_text="Set Content-Length to a non-negative integer before write_headers().",
                ) from exc
        else:
            headers.c_set(b"transfer-encoding", b"chunked")
            if headers.c_get(b"content-encoding") is None:
                encoding = self._select(
                    None, headers.c_get(b"content-type"), True
                )
                if encoding is not None:
                    if encoding == b"br":
                        self._ensure_brotli()
                    else:
                        self._ensure_gzip()
                    headers.c_set(b"content-encoding", encoding)
                    headers.c_merge_vary(b"accept-encoding")
        self._buf_bytes(_status_line(status_code))
        self._buf_bytes(self._date_box[0])
        if self._out_buf is None:
            self._out_buf = bytearray(256)
        headers.c_write_wire_ba(self._out_buf, &self._out_len)
        self._buf_bytes(CRLF)
        self._flush()
        self._status_code = status_code
        return self

    def write(self, data):
        cdef Py_ssize_t n
        cdef object part
        cdef const unsigned char* native_out = NULL
        cdef size_t native_len = 0
        if self._transport.is_closing():
            self._free_compressors()
            return self
        if self._completed:
            raise StarioRuntime(
                "Cannot write after response is completed. "
                "This happens after calling w.end() or a response helper has "
                "already finalized the writer. "
                "Each handler should only send one response.",
                help_text=(
                    "Send one response per handler: stream with write()/end(), "
                    "or finish with a response helper — not both."
                ),
            )
        if not data:
            return self
        if self._status_code < 0:
            self.write_headers(200)
        if self._status_code >= 0 and not _may_have_body(self._status_code):
            raise StarioRuntime(
                f"Cannot write a body for HTTP {self._status_code} responses.",
                help_text=(
                    "204/304 and 1xx responses must not include a message body."
                ),
            )
        n = self._body_nbytes(data)
        if n == 0:
            return self
        if self._declared_length >= 0:
            # Known Content-Length: write parts directly (no _out_buf copy).
            self._bytes_written += n
            self._write_body_raw(data)
            return self
        if self._brotli != NULL or self._gzip != NULL:
            # Chunked + compressed: feed each part to the streaming compressor.
            if isinstance(data, (list, tuple)):
                for part in data:
                    if not isinstance(part, (bytes, bytearray, memoryview)):
                        raise TypeError(
                            "body parts must be bytes-like, got "
                            + type(part).__name__
                        )
                    if not part:
                        continue
                    self._block(part, &native_out, &native_len)
                    self._write_native_chunk(native_out, native_len)
            else:
                self._block(data, &native_out, &native_len)
                self._write_native_chunk(native_out, native_len)
            return self
        # Chunked identity: one chunk framed around all parts (no join).
        self._buf_uint(<size_t>n, 16)
        self._buf_bytes(CRLF)
        self._buf_body(data)
        self._buf_bytes(CRLF)
        self._flush()
        return self

    def end(self, data=None):
        cdef object cl
        cdef const unsigned char* native_out = NULL
        cdef size_t native_len = 0
        if self._completed:
            return
        if self._transport.is_closing():
            self._free_compressors()
            self._completed = True
            self._done()
            return
        if self._status_code < 0:
            cl = _dec(
                <size_t>(
                    self._body_nbytes(data) if data is not None else 0
                )
            )
            self.headers.c_set(b"content-length", cl)
            self.write_headers(200 if data is not None else 204)
        if data:
            self.write(data)
        if self._declared_length >= 0 and self._bytes_written != self._declared_length:
            raise StarioRuntime(
                "Response body length mismatch: wrote "
                f"{self._bytes_written} bytes, Content-Length is {self._declared_length}",
                help_text=(
                    "When Content-Length is set, write exactly that many bytes "
                    "before w.end()."
                ),
            )
        if self._declared_length < 0:
            if self._brotli != NULL or self._gzip != NULL:
                self._finish(&native_out, &native_len)
                self._write_native_chunk(native_out, native_len)
                self._free_compressors()
            self._buf_bytes(CHUNK_END)
            self._flush()
        self._completed = True
        self._done()

    @property
    def state(self):
        if self._state is None:
            self._state = {}
        return self._state

    @state.setter
    def state(self, value):
        self._state = value

    @property
    def disconnect(self):
        return self._connection.ensure_disconnect()

    @property
    def disconnected(self):
        cdef object connection = self._connection
        cdef object future
        if connection is None:
            return True
        if connection.closed:
            return True
        future = connection.disconnect
        return future is not None and future.done()

    @property
    def shutting_down(self):
        return self.app.shutting_down

    @property
    def closing(self):
        return self.disconnected or self.shutting_down

    def alive(self, source=None):
        return _Alive(self, source)

    cdef void reset_body(self, bint expect_continue):
        self._body_active = True
        if self._chunks is not None:
            self._chunks.clear()
        self._cached = None
        self._data_ready = None
        self._cancel_stall_timer()
        self._expect_continue = expect_continue
        self._buffered = 0
        self._total_read = 0
        self._read_max_size = -1
        self._consumed_as = CONSUMED_NONE
        self._abort_reason = ABORT_NONE
        self._body_complete = False
        self._waiting = False
        self._acc_len = 0
        self._acc_pos = 0
        self._expected_body = -1
        self._stream_max_chunk = DEFAULT_STREAM_CHUNK

    cdef void prepare_body_capacity(self, Py_ssize_t expected_size):
        """Pre-size the C accumulator for a known Content-Length (handler already running)."""
        self._expected_body = expected_size
        if expected_size > 0:
            self._acc_reserve(expected_size)

    cdef int _acc_compact(self) except -1:
        cdef Py_ssize_t unread
        if self._acc_pos == 0:
            return 0
        unread = self._acc_len - self._acc_pos
        if unread == 0:
            self._acc_pos = 0
            self._acc_len = 0
            return 0
        memmove(self._acc, self._acc + self._acc_pos, <size_t>unread)
        self._acc_len = unread
        self._acc_pos = 0
        return 0

    cdef int _acc_reserve(self, Py_ssize_t need) except -1:
        cdef Py_ssize_t next_cap
        cdef char* p
        # ``need`` is the absolute write-end offset (_acc_len after the next append).
        if need <= self._acc_cap:
            return 0
        next_cap = 64 if self._acc_cap == 0 else self._acc_cap * 2
        if next_cap < need:
            next_cap = need
        p = <char*>realloc(self._acc, <size_t>next_cap)
        if p == NULL:
            raise MemoryError()
        self._acc = p
        self._acc_cap = next_cap
        return 0

    cdef int _acc_add(self, const char* at, size_t length) except -1:
        cdef Py_ssize_t need
        if (
            self._acc_pos > 0
            and self._acc_len + <Py_ssize_t>length > self._acc_cap
        ):
            self._acc_compact()
        need = self._acc_len + <Py_ssize_t>length
        self._acc_reserve(need)
        memcpy(self._acc + self._acc_len, at, length)
        self._acc_len = need
        return 0

    cdef object _acc_to_bytes(self):
        cdef object out
        cdef Py_ssize_t unread = self._acc_len - self._acc_pos
        if unread <= 0:
            self._acc_pos = 0
            self._acc_len = 0
            return b""
        out = PyBytes_FromStringAndSize(self._acc + self._acc_pos, unread)
        self._acc_pos = 0
        self._acc_len = 0
        return out

    cdef void _emit_from_acc(self, Py_ssize_t n):
        """Peel ``n`` unread bytes from ``_acc`` into the stream chunk list (no slide)."""
        cdef Py_ssize_t unread
        cdef object chunk
        unread = self._acc_len - self._acc_pos
        if n <= 0 or unread <= 0:
            return
        if n > unread:
            n = unread
        if self._chunks is None:
            self._chunks = []
        chunk = PyBytes_FromStringAndSize(self._acc + self._acc_pos, n)
        self._chunks.append(chunk)
        self._buffered += n
        self._acc_pos += n
        if self._acc_pos >= self._acc_len:
            self._acc_pos = 0
            self._acc_len = 0

    cdef Py_ssize_t _acc_unread(self) noexcept:
        return self._acc_len - self._acc_pos

    cdef void _raise_abort(self):
        if self._abort_reason == ABORT_TOO_LARGE:
            raise HttpException(413, "Request body too large")
        if self._abort_reason == ABORT_TIMEOUT:
            raise HttpException(
                408,
                "Request timeout: body upload too slow. "
                "This may indicate a slowloris attack or very poor connection.",
            )
        if self._abort_reason == ABORT_DISCONNECTED:
            raise ClientDisconnected()

    cdef void _wake(self):
        if self._data_ready is not None:
            self._data_ready.set()

    cdef void _cancel_stall_timer(self):
        cdef object handle = self._stall_handle
        if handle is not None:
            handle.cancel()
            self._stall_handle = None

    cdef void _reset_stall_timer(self):
        """Arm/refresh slowloris stall timeout while a body consumer is waiting."""
        cdef object loop
        self._cancel_stall_timer()
        if not self._waiting or self._body_complete or self._timeout <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._stall_handle = loop.call_later(self._timeout, self._on_stall_timeout)

    def _on_stall_timeout(self):
        self._stall_handle = None
        if self._abort_reason != ABORT_NONE or self._body_complete:
            return
        self._abort_reason = ABORT_TIMEOUT
        if self._chunks is not None:
            self._chunks.clear()
        self._wake()

    cdef object _take_chunk(self, int index):
        cdef object chunk = self._chunks[index]
        cdef object transport
        self._buffered -= len(chunk)
        if self._buffered < LOW_WATER:
            transport = self._transport
            if transport is not None and not transport.is_closing():
                transport.resume_reading()
        return index + 1, chunk

    cdef void _maybe_continue(self):
        if self._expect_continue:
            self._expect_continue = False
            if self._transport is not None and not self._transport.is_closing():
                self._transport.write(b"HTTP/1.1 100 Continue\r\n\r\n")

    cdef void _maybe_pause(self):
        cdef object transport
        # Backpressure only while a stream consumer is draining — body() keeps one
        # contiguous _acc up to max_body_bytes / Content-Length.
        if self._consumed_as != CONSUMED_STREAM:
            return
        if self._buffered + self._acc_unread() > HIGH_WATER:
            transport = self._transport
            if transport is not None and not transport.is_closing():
                transport.pause_reading()

    cdef void c_feed(self, const char* at, size_t length):
        cdef Py_ssize_t max_chunk
        if not self._body_active:
            self.reset_body(False)
        self._total_read += <int>length
        if self._total_read > self._max_size:
            self._abort_reason = ABORT_TOO_LARGE
            self._cancel_stall_timer()
            self._wake()
            return
        # Always append into the C accumulator. body() materializes once at complete;
        # stream() peels max_chunk-sized Python bytes.
        self._acc_add(at, length)
        if (
            self._read_max_size >= 0
            and self._acc_unread() > self._read_max_size
        ):
            self._abort_reason = ABORT_TOO_LARGE
            self._cancel_stall_timer()
            self._wake()
            return
        if self._consumed_as == CONSUMED_STREAM:
            max_chunk = self._stream_max_chunk
            while self._acc_unread() >= max_chunk:
                self._emit_from_acc(max_chunk)
                self._wake()
            # Progress resets stall; do not rely on wait_for.
            if self._waiting:
                self._reset_stall_timer()
            self._maybe_pause()
            return
        # body() waits only for message complete — refresh stall on progress,
        # but do not Event-wake on every TCP segment.
        if self._waiting:
            self._reset_stall_timer()

    cdef void c_complete(self):
        if not self._body_active:
            return
        self._body_complete = True
        self._cancel_stall_timer()
        if self._consumed_as == CONSUMED_STREAM:
            if self._acc_unread() > 0:
                self._emit_from_acc(self._acc_unread())
            self._wake()
            self._maybe_recycle()
            return
        if self._abort_reason != ABORT_NONE:
            self._acc_pos = 0
            self._acc_len = 0
            self._wake()
            self._maybe_recycle()
            return
        # body() already waiting: materialize once. Otherwise leave bytes in _acc
        # until body() (or never — then park drops them).
        if self._consumed_as == CONSUMED_BODY:
            if self._cached is None:
                self._cached = self._acc_to_bytes()
            self._buffered = 0
        self._wake()
        self._maybe_recycle()

    cdef void c_abort(self):
        if self._abort_reason != ABORT_NONE:
            return
        self._free_compressors()
        self._abort_reason = ABORT_DISCONNECTED
        self._body_complete = True
        self._cancel_stall_timer()
        self._wake()
        self._maybe_recycle()

    async def _wait_for_body_data(self):
        if self._abort_reason != ABORT_NONE:
            if self._chunks is not None:
                self._chunks.clear()
            self._raise_abort()
        if self.disconnected:
            if self._chunks is not None:
                self._chunks.clear()
            self._abort_reason = ABORT_DISCONNECTED
            self._raise_abort()
        if self._data_ready is None:
            self._data_ready = asyncio.Event()
        self._waiting = True
        self._reset_stall_timer()
        try:
            await self._data_ready.wait()
        finally:
            self._waiting = False
            self._cancel_stall_timer()
        self._data_ready.clear()
        if self._abort_reason != ABORT_NONE:
            if self._chunks is not None:
                self._chunks.clear()
            self._raise_abort()

    async def stream(self, max_chunk=None):
        cdef int index
        cdef object chunk
        cdef Py_ssize_t chunk_size
        if self._abort_reason != ABORT_NONE:
            self._raise_abort()
        if self._consumed_as == CONSUMED_BODY:
            raise StarioRuntime(
                "Body already read with body(). "
                "Use the returned bytes from body(); request bodies cannot switch to streaming after buffering.",
                help_text="Choose body() or stream() once per request — not both.",
            )
        if self._consumed_as == CONSUMED_STREAM:
            raise StarioRuntime(
                "Body already streaming. Each request body can only be streamed once.",
                help_text="Call stream() only once per request.",
            )
        if max_chunk is None:
            chunk_size = DEFAULT_STREAM_CHUNK
        else:
            chunk_size = <Py_ssize_t>max_chunk
            if chunk_size <= 0:
                raise ValueError("max_chunk must be positive")
            if chunk_size >= HIGH_WATER:
                raise ValueError(
                    f"max_chunk ({chunk_size}) must be lower than "
                    f"high water mark ({HIGH_WATER})"
                )
        self._stream_max_chunk = chunk_size
        self._consumed_as = CONSUMED_STREAM
        if self._cached is not None:
            yield self._cached
            return
        self._maybe_continue()
        # Emit any already-buffered full batches; leave a partial in _acc.
        while self._acc_unread() >= chunk_size:
            self._emit_from_acc(chunk_size)
        index = 0
        while True:
            while self._chunks is not None and index < len(self._chunks):
                index, chunk = self._take_chunk(index)
                yield chunk
            if self._body_complete:
                if self._acc_unread() > 0:
                    self._emit_from_acc(self._acc_unread())
                    continue
                if self._chunks is not None:
                    self._chunks.clear()
                return
            await self._wait_for_body_data()

    async def read(self, max_size=None):
        if max_size is not None and max_size < 0:
            raise ValueError("max_size must be non-negative.")
        if self._abort_reason != ABORT_NONE:
            self._raise_abort()
        if self._consumed_as == CONSUMED_STREAM:
            raise StarioRuntime(
                "Body already streamed. Each request body can only be consumed once.",
                help_text="Choose body() or stream() once per request.",
            )
        if self._cached is not None:
            if max_size is not None and len(self._cached) > max_size:
                raise HttpException(413, "Request body too large")
            self._consumed_as = CONSUMED_BODY
            return self._cached
        self._consumed_as = CONSUMED_BODY
        self._read_max_size = -1 if max_size is None else <Py_ssize_t>max_size
        self._maybe_continue()
        # Stall-aware Event wait until message complete (wake only from
        # c_complete / abort — not every TCP segment), then one materialization.
        while not self._body_complete:
            if self._abort_reason != ABORT_NONE:
                self._raise_abort()
            if (
                self._read_max_size >= 0
                and self._acc_unread() > self._read_max_size
            ):
                raise HttpException(413, "Request body too large")
            await self._wait_for_body_data()
        if self._abort_reason != ABORT_NONE:
            self._raise_abort()
        if self._cached is None:
            self._cached = self._acc_to_bytes()
        if max_size is not None and len(self._cached) > max_size:
            raise HttpException(413, "Request body too large")
        self._buffered = 0
        return self._cached


cdef RequestExchange acquire_exchange(
    object connection,
    object app,
    object transport,
    list date_box,
    object compression,
    int max_body_size,
):
    cdef RequestExchange exchange
    if _POOL:
        exchange = _POOL.pop()
    else:
        exchange = RequestExchange()
    exchange.reset(
        connection,
        app,
        transport,
        date_box,
        compression,
        max_body_size,
    )
    return exchange
