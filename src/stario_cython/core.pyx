# cython: language_level=3
"""Cython HTTP core: llhttp protocol, request exchange, and headers."""

import asyncio
from types import MappingProxyType

from libc.stddef cimport size_t
from libc.stdint cimport uint8_t, uint16_t, uint32_t, uint64_t
from libc.stdlib cimport free, realloc
from libc.stdio cimport sprintf
from libc.string cimport memcmp, memcpy
from cpython.bytearray cimport (
    PyByteArray_AS_STRING,
    PyByteArray_GET_SIZE,
    PyByteArray_Resize,
)
from cpython.bytes cimport PyBytes_AS_STRING, PyBytes_FromStringAndSize

from stario import cookies as cookie_helpers
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
)
from stario.http.context import EMPTY_ROUTE_MATCH, _Alive
from stario.http.query import ParsedQuery
from stario.http.request import DEFAULT_BODY_TIMEOUT, host_without_port
from stario.http.wire import decode_path
from stario.http.writer import get_status_line
from stario.telemetry.noop import NoOpTracer

from stario_cython.compression_buf cimport (
    StarioBrotli,
    StarioGzip,
    stario_brotli_block_borrowed,
    stario_brotli_acquire,
    stario_brotli_finish_borrowed,
    stario_brotli_release,
    stario_gzip_block_borrowed,
    stario_gzip_acquire,
    stario_gzip_finish_borrowed,
    stario_gzip_release,
)
from stario_cython.llhttp cimport *

cdef bytes _VALID_NAME = (
    b"!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)
cdef bytes _VALID_VALUE = bytes(
    b for b in range(256) if b == 0x09 or (b >= 0x20 and b != 0x7F)
)

cdef enum:
    INTERN_MAX = 36
    INTERN_TABLE_SIZE = 64
    NAME_STACK = 256

cdef const char* _INTERN_C[INTERN_MAX]
cdef Py_ssize_t _INTERN_N[INTERN_MAX]
cdef uint8_t _INTERN_SLOT[INTERN_TABLE_SIZE]
cdef list _INTERN_PY = []
cdef int _INTERN_COUNT = 0


cdef inline uint32_t _hash_bytes(const char* src, size_t n) noexcept:
    cdef uint32_t value = <uint32_t>2166136261
    cdef size_t i
    for i in range(n):
        value = (
            value ^ <uint8_t>src[i]
        ) * <uint32_t>16777619
    return value


cdef void _intern_add(const char* src):
    global _INTERN_COUNT
    cdef Py_ssize_t n = 0
    cdef int index = _INTERN_COUNT
    cdef uint32_t slot
    if index >= INTERN_MAX:
        return
    while src[n] != 0:
        n += 1
    _INTERN_C[index] = src
    _INTERN_N[index] = n
    _INTERN_PY.append(PyBytes_FromStringAndSize(src, n))
    slot = _hash_bytes(src, <size_t>n) & (INTERN_TABLE_SIZE - 1)
    while _INTERN_SLOT[slot] != 0:
        slot = (slot + 1) & (INTERN_TABLE_SIZE - 1)
    _INTERN_SLOT[slot] = <uint8_t>(index + 1)
    _INTERN_COUNT = index + 1


cdef void _init_intern() noexcept:
    if _INTERN_COUNT:
        return
    _intern_add("host")
    _intern_add("connection")
    _intern_add("cache-control")
    _intern_add("sec-ch-ua")
    _intern_add("sec-ch-ua-mobile")
    _intern_add("sec-ch-ua-platform")
    _intern_add("upgrade-insecure-requests")
    _intern_add("user-agent")
    _intern_add("accept")
    _intern_add("sec-fetch-site")
    _intern_add("sec-fetch-mode")
    _intern_add("sec-fetch-user")
    _intern_add("sec-fetch-dest")
    _intern_add("accept-encoding")
    _intern_add("accept-language")
    _intern_add("cookie")
    _intern_add("referer")
    _intern_add("origin")
    _intern_add("dnt")
    _intern_add("priority")
    _intern_add("pragma")
    _intern_add("sec-gpc")
    _intern_add("x-requested-with")
    _intern_add("content-type")
    _intern_add("content-length")
    _intern_add("content-encoding")
    _intern_add("transfer-encoding")
    _intern_add("expect")
    _intern_add("vary")
    _intern_add("location")
    _intern_add("allow")
    _intern_add("last-event-id")
    _intern_add("date")


_init_intern()


cdef inline void _lower_copy(
    char* dst,
    const char* src,
    size_t n,
) noexcept:
    cdef size_t i
    cdef uint8_t ch
    for i in range(n):
        ch = <uint8_t>src[i]
        if 65 <= ch <= 90:
            ch += 32
        dst[i] = <char>ch


cdef object _intern_name(const char* src, size_t n):
    cdef char buf[NAME_STACK]
    cdef const char* normalized
    cdef uint32_t slot
    cdef uint8_t entry
    cdef int index
    if n >= NAME_STACK:
        raise ValueError("Invalid header name: too long")
    _lower_copy(buf, src, n)
    normalized = buf
    if n == 4 and memcmp(normalized, "host", 4) == 0:
        return _INTERN_PY[0]
    if n == 10 and memcmp(normalized, "connection", 10) == 0:
        return _INTERN_PY[1]
    if n == 15 and memcmp(normalized, "accept-encoding", 15) == 0:
        return _INTERN_PY[13]
    if n == 6 and memcmp(normalized, "expect", 6) == 0:
        return _INTERN_PY[27]
    slot = _hash_bytes(normalized, n) & (INTERN_TABLE_SIZE - 1)
    while True:
        entry = _INTERN_SLOT[slot]
        if entry == 0:
            return PyBytes_FromStringAndSize(normalized, <Py_ssize_t>n)
        index = <int>entry - 1
        if (
            _INTERN_N[index] == <Py_ssize_t>n
            and memcmp(_INTERN_C[index], normalized, n) == 0
        ):
            return _INTERN_PY[index]
        slot = (slot + 1) & (INTERN_TABLE_SIZE - 1)


cdef object _encode_name(str name):
    cdef bytes raw
    try:
        raw = name.encode("latin-1")
    except UnicodeEncodeError:
        raise ValueError(f"Invalid header name: {name}")
    if not raw:
        raise ValueError("Invalid header name: empty")
    if raw.translate(None, _VALID_NAME):
        raise ValueError(f"Invalid header name: {name}")
    return _intern_name(<const char*>raw, <size_t>len(raw))


cdef object _encode_value(str value):
    cdef bytes raw
    try:
        raw = value.encode("latin-1")
    except UnicodeEncodeError:
        raise ValueError(f"Invalid header value: {value}")
    if raw.translate(None, _VALID_VALUE):
        raise ValueError(f"Invalid header value: {value}")
    return raw


cdef class Headers:
    cdef dict _data

    def __init__(self, raw_header_data=None):
        self._data = raw_header_data if raw_header_data is not None else {}

    cdef object c_get(self, object name):
        cdef object value
        value = self._data.get(name)
        if value is None:
            return None
        if type(value) is bytes:
            return value
        return value[0]

    cdef void c_set(self, object name, object value):
        self._data[name] = value

    cdef void c_add(self, object name, object value):
        cdef object existing
        if name not in self._data:
            self._data[name] = value
            return
        existing = self._data[name]
        if type(existing) is list:
            existing.append(value)
        else:
            self._data[name] = [existing, value]

    cdef void c_remove(self, object name):
        self._data.pop(name, None)

    cdef void c_clear(self):
        self._data.clear()

    cdef bint c_empty(self):
        return not self._data

    cdef bint c_vary_contains(self, object token):
        cdef object existing = self._data.get(b"vary")
        cdef object value
        cdef object part
        cdef object token_lower = token.lower()
        if existing is None:
            return False
        values = (existing,) if type(existing) is bytes else existing
        for value in values:
            for raw_part in value.split(b","):
                part = raw_part.strip()
                if part == b"*" or part.lower() == token_lower:
                    return True
        return False

    cdef void c_merge_vary(self, object token):
        cdef object existing = self.c_get(b"vary")
        cdef object stripped
        cdef object part
        cdef object token_lower
        cdef bint has_value
        if existing is None:
            self.c_set(b"vary", token)
            return
        stripped = existing.strip()
        if not stripped:
            self.c_set(b"vary", token)
            return
        if stripped == b"*":
            return
        token_lower = token.lower()
        has_value = False
        for raw_part in existing.split(b","):
            part = raw_part.strip()
            if not part:
                continue
            has_value = True
            if part == b"*" or part.lower() == token_lower:
                return
        if has_value:
            self.c_set(b"vary", existing.rstrip() + b", " + token)
        else:
            self.c_set(b"vary", token)

    cdef int _add_ba(
        self,
        object buf,
        Py_ssize_t* length,
        const char* src,
        Py_ssize_t n,
    ) except -1:
        cdef bytearray ba = buf
        cdef Py_ssize_t need = length[0] + n
        cdef Py_ssize_t cap = PyByteArray_GET_SIZE(ba)
        cdef Py_ssize_t next_cap
        if n <= 0:
            return 0
        if need > cap:
            next_cap = 256 if cap == 0 else cap * 2
            if next_cap < need:
                next_cap = need
            if PyByteArray_Resize(ba, next_cap) < 0:
                raise MemoryError()
        memcpy(PyByteArray_AS_STRING(ba) + length[0], src, <size_t>n)
        length[0] = need
        return 0

    cdef int _write_pairs(
        self,
        object buf,
        Py_ssize_t* length,
        int skip_mode,
    ) except -1:
        cdef object name
        cdef object value
        cdef object header_value
        cdef const char* pointer
        for name, value in self._data.items():
            if skip_mode and (
                name == b"content-type" or name == b"content-length"
            ):
                continue
            if skip_mode == 2 and name == b"content-encoding":
                continue
            if type(value) is bytes:
                pointer = name
                self._add_ba(buf, length, pointer, <Py_ssize_t>len(name))
                self._add_ba(buf, length, <const char*>b": ", 2)
                pointer = value
                self._add_ba(buf, length, pointer, <Py_ssize_t>len(value))
                self._add_ba(buf, length, <const char*>b"\r\n", 2)
                continue
            for header_value in value:
                pointer = name
                self._add_ba(buf, length, pointer, <Py_ssize_t>len(name))
                self._add_ba(buf, length, <const char*>b": ", 2)
                pointer = header_value
                self._add_ba(
                    buf,
                    length,
                    pointer,
                    <Py_ssize_t>len(header_value),
                )
                self._add_ba(buf, length, <const char*>b"\r\n", 2)
        return 0

    cdef int c_write_wire_ba(
        self,
        object buf,
        Py_ssize_t* length,
    ) except -1:
        return self._write_pairs(buf, length, 0)

    cdef int c_write_response_wire_ba(
        self,
        object buf,
        Py_ssize_t* length,
    ) except -1:
        return self._write_pairs(buf, length, 1)

    cdef int c_write_compressed_response_wire_ba(
        self,
        object buf,
        Py_ssize_t* length,
    ) except -1:
        return self._write_pairs(buf, length, 2)

    def add(self, str name, str value):
        self.c_add(_encode_name(name), _encode_value(value))

    def unsafe_add(self, name, value):
        self.c_add(name, value)

    def set(self, str name, str value):
        self.c_set(_encode_name(name), _encode_value(value))

    def unsafe_set(self, name, value):
        self.c_set(name, value)

    def setdefault(self, str name, str value):
        cdef object key = _encode_name(name)
        cdef object existing = self.c_get(key)
        cdef object encoded
        if existing is not None:
            return existing.decode("latin-1")
        encoded = _encode_value(value)
        self.c_set(key, encoded)
        return encoded.decode("latin-1")

    def get(self, str name, default=None):
        cdef object wire = self.c_get(_encode_name(name))
        if wire is None:
            return default
        return wire.decode("latin-1")

    def unsafe_get(self, name, default=None):
        cdef object value = self.c_get(name)
        if value is None:
            return default
        return value

    def getlist(self, str name):
        return [
            value.decode("latin-1")
            for value in self.unsafe_getlist(_encode_name(name))
        ]

    def unsafe_getlist(self, name):
        cdef object value
        value = self._data.get(name)
        if value is None:
            return []
        if type(value) is bytes:
            return [value]
        return list(value)

    def remove(self, str name):
        self.c_remove(_encode_name(name))

    def unsafe_remove(self, name):
        self.c_remove(name)

    def items(self):
        return [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in self.unsafe_items()
        ]

    def unsafe_items(self):
        cdef list result = []
        cdef object name
        cdef object value
        cdef object item
        for name, value in self._data.items():
            if type(value) is list:
                for item in value:
                    result.append((name, item))
            else:
                result.append((name, value))
        return result

    def __contains__(self, name):
        try:
            return _encode_name(name) in self._data
        except ValueError:
            return False

    def __len__(self):
        return len(self._data)

    def __repr__(self):
        return f"Headers({self._data!r})"


ctypedef struct RawHeader:
    uint32_t name_offset
    uint32_t name_length
    uint32_t value_offset
    uint32_t value_length

cdef dict _URL_CACHE = {}
cdef int _URL_CACHE_MAX = 256


cdef tuple _split_request_target(object url):
    cdef object cached
    cdef Py_ssize_t question
    cdef object path_bytes
    cdef object query
    cdef tuple result
    cached = _URL_CACHE.get(url)
    if cached is not None:
        return <tuple>cached
    question = url.find(b"?")
    if question == -1:
        path_bytes, query = url, b""
    else:
        path_bytes, query = url[:question], url[question + 1 :]
    result = (decode_path(path_bytes), query)
    if len(_URL_CACHE) >= _URL_CACHE_MAX:
        _URL_CACHE.clear()
    if <Py_ssize_t>len(url) <= 512:
        _URL_CACHE[url] = result
    return result

# Stream batching limit and independent transport backpressure window.
cdef int LOW_WATER = 128 * 1024
cdef int HIGH_WATER = 512 * 1024
cdef int STREAM_CHUNK_LIMIT = 256 * 1024
cdef int OUTPUT_BUFFER_RETAIN_MAX = 64 * 1024
cdef int DEFAULT_STREAM_CHUNK = 64 * 1024
cdef int POOL_MAX = 1024
cdef int REQUEST_NAME_MAX = 256
cdef int REQUEST_ARENA_RETAIN_MAX = 8 * 1024
cdef int REQUEST_HEADERS_RETAIN_MAX = 64

cdef object BODY_TYPE_ERROR = (
    "body must be bytes-like or a list/tuple of bytes-like objects"
)

cdef int CONSUMED_NONE = 0
cdef int CONSUMED_BODY = 1
cdef int CONSUMED_STREAM = 2

cdef int ENCODING_NONE = 0
cdef int ENCODING_BR = 1
cdef int ENCODING_GZIP = 2

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
cdef bytes CE_PREFIX = b"content-encoding: "
cdef bytes VARY_PREFIX = b"vary: "
cdef bytes CL_HEADER = b"content-length: "
cdef bytes CL_PREFIX = b"\r\ncontent-length: "
cdef bytes CRLF2 = b"\r\n\r\n"
cdef bytes CRLF = b"\r\n"
cdef bytes CHUNK_END = b"0\r\n\r\n"
cdef tuple _DEC_SMALL = tuple(str(i).encode("ascii") for i in range(256))

cdef object STARTED_ERROR = (
    "Response already started (headers sent). "
    "Set headers via w.headers.set() before the first write or one-shot respond()."
)

cdef list _POOL = []
cdef object _UNBOUND = object()


cdef inline bint _is_bytes_like(object obj) noexcept:
    return isinstance(obj, (bytes, bytearray, memoryview))


cdef inline void _require_bytes_like(object part):
    if not _is_bytes_like(part):
        raise TypeError(
            "body parts must be bytes-like, got " + type(part).__name__
        )


cdef inline bint _token_equals(
    const char* value,
    size_t start,
    size_t end,
    const char* token,
    size_t token_length,
) noexcept:
    cdef size_t i
    cdef unsigned char ch
    if end - start != token_length:
        return False
    for i in range(token_length):
        ch = <unsigned char>value[start + i]
        if 65 <= ch <= 90:
            ch += 32
        if ch != <unsigned char>token[i]:
            return False
    return True


cdef bint _contains_token(
    const char* value,
    size_t length,
    const char* token,
    size_t token_length,
) noexcept:
    cdef size_t start = 0
    cdef size_t end
    while start < length:
        while start < length and (
            value[start] == <char>32
            or value[start] == <char>9
            or value[start] == <char>44
        ):
            start += 1
        end = start
        while end < length and value[end] != <char>44:
            end += 1
        while end > start and (
            value[end - 1] == <char>32 or value[end - 1] == <char>9
        ):
            end -= 1
        if _token_equals(value, start, end, token, token_length):
            return True
        start = end + 1
    return False


cdef int _parse_qvalue(
    const char* value,
    size_t start,
    size_t end,
) noexcept:
    cdef int q = 0
    cdef int digits = 0
    cdef unsigned char ch
    while start < end and (
        value[start] == <char>32 or value[start] == <char>9
    ):
        start += 1
    while end > start and (
        value[end - 1] == <char>32 or value[end - 1] == <char>9
    ):
        end -= 1
    if start >= end:
        return 0
    if value[start] == <char>49:
        start += 1
        if start == end:
            return 1000
        if value[start] != <char>46:
            return 0
        start += 1
        while start < end:
            if value[start] != <char>48:
                return 0
            start += 1
        return 1000
    if value[start] != <char>48:
        return 0
    start += 1
    if start == end:
        return 0
    if value[start] != <char>46:
        return 0
    start += 1
    while start < end and digits < 3:
        ch = <unsigned char>value[start]
        if ch < 48 or ch > 57:
            return 0
        q = q * 10 + ch - 48
        digits += 1
        start += 1
    if start != end:
        return 0
    while digits < 3:
        q *= 10
        digits += 1
    return q


cdef inline bint _range_equals_ci(
    const char* value,
    Py_ssize_t length,
    const char* expected,
    Py_ssize_t expected_length,
) noexcept:
    cdef Py_ssize_t i
    cdef unsigned char c
    if length != expected_length:
        return False
    for i in range(length):
        c = <unsigned char>value[i]
        if 65 <= c <= 90:
            c += 32
        if c != <unsigned char>expected[i]:
            return False
    return True


cdef inline bint _range_starts_ci(
    const char* value,
    Py_ssize_t length,
    const char* prefix,
    Py_ssize_t prefix_length,
) noexcept:
    if length < prefix_length:
        return False
    return _range_equals_ci(value, prefix_length, prefix, prefix_length)


cdef bint _content_type_is_compressible(object content_type):
    cdef bytes raw
    cdef const char* value
    cdef Py_ssize_t start
    cdef Py_ssize_t end
    cdef Py_ssize_t i
    if type(content_type) is not bytes:
        return content_type_is_compressible(content_type)
    raw = content_type
    value = raw
    end = len(raw)
    for i in range(end):
        if value[i] == <char>59:
            end = i
            break
    start = 0
    while start < end and (
        value[start] == <char>32
        or 9 <= <unsigned char>value[start] <= 13
    ):
        start += 1
    while end > start and (
        value[end - 1] == <char>32
        or 9 <= <unsigned char>value[end - 1] <= 13
    ):
        end -= 1
    value += start
    end -= start
    if end == 0:
        return False
    if (
        _range_starts_ci(value, end, "image/", 6)
        or _range_starts_ci(value, end, "audio/", 6)
        or _range_starts_ci(value, end, "video/", 6)
    ):
        return False
    if (
        _range_equals_ci(value, end, "application/gzip", 16)
        or _range_equals_ci(value, end, "application/x-gzip", 18)
        or _range_equals_ci(value, end, "application/zip", 15)
        or _range_equals_ci(value, end, "application/x-zip-compressed", 28)
        or _range_equals_ci(value, end, "application/x-7z-compressed", 27)
        or _range_equals_ci(value, end, "application/vnd.rar", 19)
        or _range_equals_ci(value, end, "application/x-rar-compressed", 28)
        or _range_equals_ci(value, end, "application/x-bzip", 18)
        or _range_equals_ci(value, end, "application/x-bzip2", 19)
        or _range_equals_ci(value, end, "application/x-xz", 16)
        or _range_equals_ci(value, end, "application/zstd", 16)
        or _range_equals_ci(value, end, "application/x-zstd", 18)
        or _range_equals_ci(value, end, "font/woff", 9)
        or _range_equals_ci(value, end, "font/woff2", 10)
    ):
        return False
    return True


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
    cdef int i
    if n < 256:
        return _DEC_SMALL[n]
    i = sprintf(buf, "%zu", n)
    return PyBytes_FromStringAndSize(buf, i)


cdef class Request:
    """Request view. Same handler API as stario.http.request.Request."""

    cdef public object method
    cdef public object path
    cdef public object headers
    cdef public object protocol_version
    cdef public bint keep_alive
    cdef public object query_bytes
    cdef public object _body
    cdef object _query
    cdef object _cookies
    cdef object _host

    def __init__(
        self,
        *,
        method="GET",
        path="/",
        query_bytes=b"",
        protocol_version="1.1",
        keep_alive=True,
        headers=None,
        body=None,
    ):
        self.reset(
            method, path, query_bytes, protocol_version, keep_alive, headers, body
        )

    cdef void reset(
        self,
        object method,
        object path,
        object query_bytes,
        object protocol_version,
        bint keep_alive,
        object headers,
        object body,
    ):
        self.method = method
        self.path = path
        self.headers = headers
        self.protocol_version = protocol_version
        self.keep_alive = keep_alive
        self.query_bytes = query_bytes
        self._body = body
        self._query = None
        self._cookies = None
        self._host = None

    @property
    def host(self):
        cdef object host_str
        cdef object host_wire
        if self._host is not None:
            return self._host
        if isinstance(self.headers, RequestHeaders):
            host_wire = (<RequestHeaders>self.headers).c_request_host()
            host_str = (
                host_wire.decode("latin-1") if host_wire is not None else ""
            )
        elif isinstance(self.headers, Headers):
            host_wire = (<Headers>self.headers).c_get(b"host")
            host_str = (
                host_wire.decode("latin-1") if host_wire is not None else ""
            )
        else:
            host_str = self.headers.get("host") or ""
        self._host = host_without_port(host_str)
        return self._host

    @property
    def query(self):
        if self._query is None:
            self._query = ParsedQuery(self.query_bytes)
        return self._query

    @property
    def cookies(self):
        if self._cookies is None:
            self._cookies = cookie_helpers.parse_cookie_headers(
                self.headers.getlist("cookie")
            )
        return MappingProxyType(self._cookies)

    async def body(self, max_size=None):
        if max_size is not None and max_size < 0:
            raise ValueError("max_size must be non-negative.")
        if self._body is None:
            return b""
        if type(self._body) is bytes:
            if max_size is not None and len(self._body) > max_size:
                raise HttpException(413, "Request body too large")
            return self._body
        return await self._body.read(max_size=max_size)

    async def stream(self, max_chunk=None):
        if self._body is None:
            return
        if type(self._body) is bytes:
            yield self._body
            return
        async for chunk in self._body.stream(max_chunk=max_chunk):
            yield chunk


cdef class RequestExchange:
    cdef object _transport
    cdef list _date_box
    cdef object _compression
    cdef int _req_encoding
    cdef bint _req_expect_continue
    cdef bint _req_connection_close
    cdef char* _req_arena
    cdef Py_ssize_t _req_arena_len
    cdef Py_ssize_t _req_arena_cap
    cdef RawHeader* _req_raw_headers
    cdef Py_ssize_t _req_raw_count
    cdef Py_ssize_t _req_raw_headers_cap
    cdef Py_ssize_t _req_pending_name_offset
    cdef Py_ssize_t _req_pending_name_length
    cdef Py_ssize_t _req_pending_value_offset
    cdef Py_ssize_t _req_pending_value_length
    cdef bint _req_pending_header
    cdef Py_ssize_t _req_host_index
    cdef Py_ssize_t _req_url_offset
    cdef Py_ssize_t _req_url_length
    cdef bint _req_accept_present
    cdef int _req_br_q
    cdef int _req_gzip_q
    cdef int _req_wildcard_q
    cdef int _req_identity_q
    cdef public Headers headers
    cdef StarioBrotli* _brotli
    cdef StarioGzip* _gzip
    cdef object _out_buf
    cdef object _out_hold
    cdef Py_ssize_t _out_len
    cdef int _status_code
    cdef Py_ssize_t _declared_length
    cdef Py_ssize_t _bytes_written
    cdef bint _brotli_enabled
    cdef bint _gzip_enabled
    cdef int _brotli_level
    cdef int _brotli_window
    cdef int _gzip_level
    cdef int _gzip_window
    cdef Py_ssize_t _compress_min_size
    cdef bint _completed

    cdef public object app
    cdef public object span
    cdef public object route
    cdef object _connection
    cdef object _state
    cdef public object request_headers
    cdef public Request req
    cdef bint handler_done
    cdef bint handler_started
    cdef bint in_pool

    cdef object _chunks
    cdef object _cached
    cdef object _data_ready
    cdef object _stall_handle
    cdef int _buffered
    cdef int _total_read
    cdef int _max_size
    cdef Py_ssize_t _read_max_size
    cdef double _timeout
    cdef int _consumed_as
    cdef int _abort_reason
    cdef bint _body_active
    cdef bint _body_complete
    cdef bint _expect_continue
    cdef bint _waiting
    cdef bint _discard_body
    cdef object _body_tail
    cdef Py_ssize_t _tail_used
    cdef Py_ssize_t _tail_cap
    cdef Py_ssize_t _expected_size
    cdef Py_ssize_t _stream_max_chunk

    def __cinit__(self):
        self._req_arena = NULL
        self._req_raw_headers = NULL
        self._req_url_offset = 0
        self._req_url_length = 0
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
        self._body_tail = None
        self._tail_used = 0
        self._tail_cap = 0
        self._expected_size = -1

    def __init__(self):
        self.headers = Headers()
        self.request_headers = RequestHeaders(self)
        self.req = Request()
        self._chunks = None
        self._cached = None
        self._data_ready = None
        self._stall_handle = None
        self._compression = _UNBOUND
        self._brotli_enabled = False
        self._gzip_enabled = False
        self._compress_min_size = DEFAULT_MIN_SIZE
        self._state = None
        self.in_pool = False
        self._body_active = False
        self._discard_body = False
        self._read_max_size = -1
        self._stream_max_chunk = DEFAULT_STREAM_CHUNK

    def __dealloc__(self):
        self._free_compressors()
        if self._req_arena != NULL:
            free(self._req_arena)
            self._req_arena = NULL
        if self._req_raw_headers != NULL:
            free(self._req_raw_headers)
            self._req_raw_headers = NULL

    cdef void reset_response(self, int encoding):
        self._free_compressors()
        self._req_encoding = encoding
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
        self._gzip_window = 15
        self._brotli_enabled = False
        self._gzip_enabled = False
        self._compress_min_size = DEFAULT_MIN_SIZE
        if compression is None:
            return
        # Snapshot CompressionConfig so hot paths skip Python attribute access.
        self._brotli_level = compression.brotli_level
        window = compression.brotli_window_log
        self._brotli_window = 0 if window is None else window
        self._gzip_level = compression.gzip_level
        window = compression.gzip_window_bits
        self._gzip_window = 15 if window is None else window
        self._compress_min_size = compression.min_size
        self._brotli_enabled = self._brotli_level >= 0
        self._gzip_enabled = self._gzip_level >= 0

    cdef inline int _ensure_brotli(self) except -1:
        if self._brotli != NULL:
            return 0
        self._brotli = stario_brotli_acquire(
            self._brotli_level,
            self._brotli_window,
        )
        if self._brotli == NULL:
            raise StarioError("brotli stream init failed")
        return 0

    cdef inline int _ensure_gzip(self) except -1:
        if self._gzip != NULL:
            return 0
        self._gzip = stario_gzip_acquire(
            self._gzip_level,
            self._gzip_window,
        )
        if self._gzip == NULL:
            raise StarioError("gzip stream init failed")
        return 0

    cdef void _free_compressors(self):
        if self._brotli != NULL:
            stario_brotli_release(self._brotli)
            self._brotli = NULL
        if self._gzip != NULL:
            stario_gzip_release(self._gzip)
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
        if _is_bytes_like(body):
            return <Py_ssize_t>len(body)
        if isinstance(body, (list, tuple)):
            total = 0
            for part in body:
                _require_bytes_like(part)
                total += <Py_ssize_t>len(part)
            return total
        raise TypeError(BODY_TYPE_ERROR)

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
        raise TypeError(BODY_TYPE_ERROR)

    cdef int _buf_body(self, object body) except -1:
        """Append body bytes or each list/tuple part into ``_out_buf`` (no join)."""
        cdef object part
        if body is None:
            return 0
        if _is_bytes_like(body):
            return self._buf_bytes(body)
        if isinstance(body, (list, tuple)):
            for part in body:
                _require_bytes_like(part)
                self._buf_bytes(part)
            return 0
        raise TypeError(BODY_TYPE_ERROR)

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

    cdef bint _may_compress(
        self,
        object data,
        object content_type,
        bint streaming,
        Py_ssize_t nbytes,
    ):
        if self._req_encoding == ENCODING_NONE:
            return False
        if not streaming:
            if data is None or nbytes < self._compress_min_size:
                return False
        if content_type is not None and not _content_type_is_compressible(content_type):
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

    cdef int _reserve_request_arena(
        self,
        Py_ssize_t bytes_needed,
    ) noexcept:
        cdef Py_ssize_t needed = self._req_arena_len + bytes_needed
        cdef Py_ssize_t cap
        cdef char* arena
        if needed <= self._req_arena_cap:
            return 0
        cap = 256 if self._req_arena_cap == 0 else self._req_arena_cap * 2
        if cap < needed:
            cap = needed
        arena = <char*>realloc(self._req_arena, <size_t>cap)
        if arena == NULL:
            return -1
        self._req_arena = arena
        self._req_arena_cap = cap
        return 0

    cdef int _reserve_request_headers(self) noexcept:
        cdef Py_ssize_t cap
        cdef RawHeader* headers
        if self._req_raw_count < self._req_raw_headers_cap:
            return 0
        cap = (
            16
            if self._req_raw_headers_cap == 0
            else self._req_raw_headers_cap * 2
        )
        headers = <RawHeader*>realloc(
            self._req_raw_headers,
            <size_t>cap * sizeof(RawHeader),
        )
        if headers == NULL:
            return -1
        self._req_raw_headers = headers
        self._req_raw_headers_cap = cap
        return 0

    cdef int append_request_url(self, const char* data, size_t length) noexcept:
        if self._req_url_length == 0:
            self._req_url_offset = self._req_arena_len
        if self._reserve_request_arena(<Py_ssize_t>length) != 0:
            return -1
        if length:
            memcpy(self._req_arena + self._req_arena_len, data, length)
        self._req_arena_len += <Py_ssize_t>length
        self._req_url_length += <Py_ssize_t>length
        return 0


    cdef int append_request_header_name(
        self,
        const char* data,
        size_t length,
    ) noexcept:
        if not self._req_pending_header:
            self._req_pending_header = True
            self._req_pending_name_offset = self._req_arena_len
            self._req_pending_name_length = 0
            self._req_pending_value_offset = -1
            self._req_pending_value_length = 0
        if self._req_pending_value_offset >= 0:
            return -1
        if self._req_pending_name_length + <Py_ssize_t>length >= REQUEST_NAME_MAX:
            return -1
        if self._reserve_request_arena(<Py_ssize_t>length) != 0:
            return -1
        _lower_copy(self._req_arena + self._req_arena_len, data, length)
        self._req_arena_len += <Py_ssize_t>length
        self._req_pending_name_length += <Py_ssize_t>length
        return 0

    cdef int append_request_header_value(
        self,
        const char* data,
        size_t length,
    ) noexcept:
        if not self._req_pending_header:
            return -1
        if self._req_pending_value_offset < 0:
            self._req_pending_value_offset = self._req_arena_len
        if self._reserve_request_arena(<Py_ssize_t>length) != 0:
            return -1
        if length:
            memcpy(self._req_arena + self._req_arena_len, data, length)
        self._req_arena_len += <Py_ssize_t>length
        self._req_pending_value_length += <Py_ssize_t>length
        return 0

    cdef int finish_request_header(self) noexcept:
        cdef RawHeader* header
        cdef const char* name
        cdef const char* value
        cdef size_t name_length
        cdef size_t value_length
        if not self._req_pending_header:
            return 0
        if self._req_pending_name_length == 0:
            return -1
        if self._req_pending_value_offset < 0:
            self._req_pending_value_offset = self._req_arena_len
        name = self._req_arena + self._req_pending_name_offset
        value = self._req_arena + self._req_pending_value_offset
        name_length = <size_t>self._req_pending_name_length
        value_length = <size_t>self._req_pending_value_length
        if self._reserve_request_headers() != 0:
            return -1
        header = &self._req_raw_headers[self._req_raw_count]
        header.name_offset = <uint32_t>self._req_pending_name_offset
        header.name_length = <uint32_t>name_length
        header.value_offset = <uint32_t>self._req_pending_value_offset
        header.value_length = <uint32_t>value_length
        if name_length == 4 and memcmp(name, "host", 4) == 0:
            if self._req_host_index < 0:
                self._req_host_index = self._req_raw_count
        elif name_length == 10 and memcmp(name, "connection", 10) == 0:
            if _contains_token(value, value_length, "close", 5):
                self._req_connection_close = True
        elif name_length == 6 and memcmp(name, "expect", 6) == 0:
            if _contains_token(value, value_length, "100-continue", 12):
                self._req_expect_continue = True
        elif name_length == 15 and memcmp(name, "accept-encoding", 15) == 0:
            self._scan_request_accept_encoding(value, value_length)
        self._req_raw_count += 1
        self._req_pending_header = False
        return 0

    cdef void _scan_request_accept_encoding(
        self,
        const char* value,
        size_t length,
    ) noexcept:
        cdef size_t start = 0
        cdef size_t segment_end
        cdef size_t separator
        cdef size_t token_end
        cdef size_t param_start
        cdef size_t param_end
        cdef size_t equals
        cdef size_t key_start
        cdef size_t key_end
        cdef int q
        if not self._req_accept_present:
            self._req_accept_present = True
            self._req_br_q = -1
            self._req_gzip_q = -1
            self._req_wildcard_q = -1
            self._req_identity_q = -1
        while start < length:
            segment_end = start
            while segment_end < length and value[segment_end] != <char>44:
                segment_end += 1
            while start < segment_end and (
                value[start] == <char>32 or value[start] == <char>9
            ):
                start += 1
            separator = start
            while separator < segment_end and value[separator] != <char>59:
                separator += 1
            token_end = separator
            while token_end > start and (
                value[token_end - 1] == <char>32
                or value[token_end - 1] == <char>9
            ):
                token_end -= 1
            q = 1000
            param_start = separator
            while param_start < segment_end:
                param_start += 1
                param_end = param_start
                while (
                    param_end < segment_end
                    and value[param_end] != <char>59
                ):
                    param_end += 1
                equals = param_start
                while equals < param_end and value[equals] != <char>61:
                    equals += 1
                key_start = param_start
                while key_start < equals and (
                    value[key_start] == <char>32
                    or value[key_start] == <char>9
                ):
                    key_start += 1
                key_end = equals
                while key_end > key_start and (
                    value[key_end - 1] == <char>32
                    or value[key_end - 1] == <char>9
                ):
                    key_end -= 1
                if (
                    equals < param_end
                    and key_end - key_start == 1
                    and (
                        value[key_start] == <char>113
                        or value[key_start] == <char>81
                    )
                ):
                    q = _parse_qvalue(value, equals + 1, param_end)
                    break
                param_start = param_end
            if _token_equals(value, start, token_end, "br", 2):
                self._req_br_q = q
            elif _token_equals(value, start, token_end, "gzip", 4):
                self._req_gzip_q = q
            elif _token_equals(value, start, token_end, "*", 1):
                self._req_wildcard_q = q
            elif _token_equals(value, start, token_end, "identity", 8):
                self._req_identity_q = q
            start = segment_end + 1

    cdef void _clear_request_headers(self):
        (<RequestHeaders>self.request_headers).c_reset()
        if (
            self._req_arena != NULL
            and self._req_arena_cap > REQUEST_ARENA_RETAIN_MAX
        ):
            free(self._req_arena)
            self._req_arena = NULL
            self._req_arena_cap = 0
        if (
            self._req_raw_headers != NULL
            and self._req_raw_headers_cap > REQUEST_HEADERS_RETAIN_MAX
        ):
            free(self._req_raw_headers)
            self._req_raw_headers = NULL
            self._req_raw_headers_cap = 0
        self._req_arena_len = 0
        self._req_raw_count = 0
        self._req_pending_header = False
        self._req_host_index = -1
        self._req_url_offset = 0
        self._req_url_length = 0
        self._clear_hot_request_headers()

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
            self._timeout = DEFAULT_BODY_TIMEOUT
            if self._compression is not compression:
                self._compression = compression
                self._apply_compression(compression)
        self.span = None
        self.route = EMPTY_ROUTE_MATCH
        self._state = None
        self._clear_request_headers()
        self.handler_done = False
        self.handler_started = False

    cdef void _clear_hot_request_headers(self):
        self._req_encoding = ENCODING_NONE
        self._req_expect_continue = False
        self._req_connection_close = False
        self._req_accept_present = False
        self._req_br_q = -1
        self._req_gzip_q = -1
        self._req_wildcard_q = -1
        self._req_identity_q = -1

    cdef void cache_hot_request_headers(self):
        """Select response encoding from exchange-local request-header state."""
        cdef int wildcard
        cdef int brotli_q
        cdef int gzip_q
        cdef int best_q = 0
        if (
            not self._req_accept_present
            or not (self._brotli_enabled or self._gzip_enabled)
        ):
            self._req_encoding = ENCODING_NONE
            return
        wildcard = (
            self._req_wildcard_q
            if self._req_wildcard_q >= 0
            else 0
        )
        brotli_q = self._req_br_q if self._req_br_q >= 0 else wildcard
        gzip_q = (
            self._req_gzip_q
            if self._req_gzip_q >= 0
            else wildcard
        )
        self._req_encoding = ENCODING_NONE
        if self._brotli_enabled and brotli_q > best_q:
            best_q = brotli_q
            self._req_encoding = ENCODING_BR
        if self._gzip_enabled and gzip_q > best_q:
            best_q = gzip_q
            self._req_encoding = ENCODING_GZIP
        if self._req_identity_q >= best_q:
            self._req_encoding = ENCODING_NONE

    cdef void start_response(self):
        self.handler_started = True
        self.reset_response(self._req_encoding)

    cdef void handler_finished(self):
        self.handler_done = True
        if self._body_active and not self._body_complete:
            self._discard_body = True
            self._cached = None
            self._clear_body_storage()
            self._cancel_stall_timer()
            self._connection.set_body_paused(self, False)
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
        self._clear_body_storage()
        self._body_active = False
        self._read_max_size = -1
        self._clear_hot_request_headers()
        self._stream_max_chunk = DEFAULT_STREAM_CHUNK
        if (
            self._out_buf is not None
            and PyByteArray_GET_SIZE(self._out_buf) > OUTPUT_BUFFER_RETAIN_MAX
        ):
            self._out_buf = None
        if (
            self._out_hold is not None
            and PyByteArray_GET_SIZE(self._out_hold) > OUTPUT_BUFFER_RETAIN_MAX
        ):
            self._out_hold = None

    cdef void release_global(self):
        self._free_compressors()
        self.headers.c_clear()
        self._clear_request_headers()
        self.req.reset("GET", "/", b"", "1.1", True, None, None)
        self.span = None
        self.route = EMPTY_ROUTE_MATCH
        self._state = None
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
        # Empty headers + no compression: writelines of existing buffers (no join,
        # no _out_buf churn — keeps tiny plaintext/json competitive).
        if h.c_empty() and (
            not _may_have_body(status)
            or not self._may_compress(body, content_type, False, nbytes)
        ):
            if not _may_have_body(status):
                self._transport.writelines(
                    (_status_line(status), self._date_box[0], ZERO_CL)
                )
            elif isinstance(body, (list, tuple)):
                self._transport.writelines((
                    _status_line(status),
                    self._date_box[0],
                    CT_PREFIX,
                    content_type,
                    CL_PREFIX,
                    _dec(<size_t>nbytes),
                    CRLF2,
                ))
                self._transport.writelines(body)
            else:
                self._transport.writelines((
                    _status_line(status),
                    self._date_box[0],
                    CT_PREFIX,
                    content_type,
                    CL_PREFIX,
                    _dec(<size_t>nbytes),
                    CRLF2,
                    body,
                ))
        else:
            if not _may_have_body(status):
                body = b""
            elif h.c_get(b"content-encoding") is None:
                encoding = None
                if self._may_compress(body, content_type, False, nbytes):
                    encoding = (
                        b"br" if self._req_encoding == ENCODING_BR else b"gzip"
                    )
                if encoding is not None:
                    flat = self._body_as_bytes(body)
                    try:
                        self._frame(flat, encoding, &native_out, &native_len)
                        self._declared_length = <Py_ssize_t>native_len
                        self._bytes_written = 0
                        self._buf_bytes(_status_line(status))
                        self._buf_bytes(self._date_box[0])
                        if self._out_buf is None:
                            self._out_buf = bytearray(256)
                        h.c_write_compressed_response_wire_ba(
                            self._out_buf,
                            &self._out_len,
                        )
                        self._buf_bytes(CE_PREFIX)
                        self._buf_bytes(encoding)
                        self._buf_bytes(CRLF)
                        if not h.c_vary_contains(b"accept-encoding"):
                            self._buf_bytes(VARY_PREFIX)
                            self._buf_bytes(b"accept-encoding")
                            self._buf_bytes(CRLF)
                        self._buf_bytes(CT_PREFIX)
                        self._buf_bytes(content_type)
                        self._buf_bytes(CRLF)
                        self._buf_bytes(CL_HEADER)
                        self._buf_uint(native_len, 10)
                        self._buf_bytes(CRLF2)
                        self._buf_add(<const char*>native_out, <Py_ssize_t>native_len)
                        self._flush()
                    finally:
                        self._free_compressors()
                    self._status_code = status
                    self._bytes_written = self._declared_length
                    self._completed = True
                    self._done()
                    return
            self._declared_length = nbytes
            self._bytes_written = 0
            self._buf_bytes(_status_line(status))
            self._buf_bytes(self._date_box[0])
            if self._out_buf is None:
                self._out_buf = bytearray(256)
            h.c_write_response_wire_ba(self._out_buf, &self._out_len)
            self._buf_bytes(CT_PREFIX)
            self._buf_bytes(content_type)
            self._buf_bytes(CRLF)
            self._buf_bytes(CL_HEADER)
            self._buf_uint(<size_t>nbytes, 10)
            self._buf_bytes(CRLF2)
            self._flush()
            self._status_code = status
            if nbytes:
                if isinstance(body, (list, tuple)):
                    self._transport.writelines(body)
                else:
                    self._transport.write(body)
        self._status_code = status
        self._bytes_written = self._declared_length if self._declared_length > 0 else 0
        self._completed = True
        self._done()

    def abort(self):
        if self._completed:
            return
        self._free_compressors()
        self._completed = True
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
                encoding = None
                if self._may_compress(
                    None, headers.c_get(b"content-type"), True, -1
                ):
                    encoding = (
                        b"br" if self._req_encoding == ENCODING_BR else b"gzip"
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
            self._bytes_written += n
            if isinstance(data, (list, tuple)):
                self._transport.writelines(data)
            else:
                self._transport.write(data)
            return self
        if self._brotli != NULL or self._gzip != NULL:
            if isinstance(data, (list, tuple)):
                for part in data:
                    _require_bytes_like(part)
                    if not part:
                        continue
                    self._block(part, &native_out, &native_len)
                    self._write_native_chunk(native_out, native_len)
            else:
                self._block(data, &native_out, &native_len)
                self._write_native_chunk(native_out, native_len)
            return self
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
            cl = _dec(<size_t>self._body_nbytes(data))
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

    cdef void reset_body(self, bint expect_continue, Py_ssize_t expected_size):
        self._body_active = True
        self._clear_body_storage()
        self._cached = None
        self._data_ready = None
        self._cancel_stall_timer()
        self._expect_continue = expect_continue
        self._total_read = 0
        self._read_max_size = -1
        self._consumed_as = CONSUMED_NONE
        self._abort_reason = ABORT_NONE
        self._body_complete = False
        self._waiting = False
        self._discard_body = False
        self._expected_size = expected_size
        self._stream_max_chunk = DEFAULT_STREAM_CHUNK

    cdef void _clear_body_storage(self):
        if self._chunks is not None:
            self._chunks.clear()
        self._body_tail = None
        self._tail_used = 0
        self._tail_cap = 0
        self._buffered = 0

    cdef int _ensure_body_tail(self, Py_ssize_t received_before) except -1:
        cdef Py_ssize_t cap
        cdef Py_ssize_t remaining
        if self._body_tail is not None:
            return 0
        cap = self._stream_max_chunk
        if (
            self._consumed_as == CONSUMED_BODY
            and self._expected_size > 0
            and received_before == 0
        ):
            cap = self._expected_size
        elif self._consumed_as != CONSUMED_STREAM:
            cap = DEFAULT_STREAM_CHUNK
        if self._expected_size >= 0:
            remaining = self._expected_size - received_before
            if 0 < remaining < cap:
                cap = remaining
        self._body_tail = PyBytes_FromStringAndSize(NULL, cap)
        self._tail_used = 0
        self._tail_cap = cap
        return 0

    cdef int _seal_body_tail(self) except -1:
        cdef object chunk
        if self._body_tail is None or self._tail_used == 0:
            return 0
        if self._tail_used == self._tail_cap:
            chunk = self._body_tail
        else:
            chunk = PyBytes_FromStringAndSize(
                PyBytes_AS_STRING(self._body_tail),
                self._tail_used,
            )
        if self._chunks is None:
            self._chunks = []
        self._chunks.append(chunk)
        self._buffered += self._tail_used
        self._body_tail = None
        self._tail_used = 0
        self._tail_cap = 0
        return 0

    cdef object _body_to_bytes(self):
        cdef object out
        self._seal_body_tail()
        if self._chunks is None or not self._chunks:
            return b""
        if len(self._chunks) == 1:
            out = self._chunks[0]
        else:
            out = b"".join(self._chunks)
        self._chunks.clear()
        self._buffered = 0
        return out

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
        self._clear_body_storage()
        self._connection.set_body_paused(self, False)
        self._wake()

    cdef void _maybe_continue(self):
        if self._expect_continue:
            self._expect_continue = False
            if self._transport is not None and not self._transport.is_closing():
                self._transport.write(b"HTTP/1.1 100 Continue\r\n\r\n")

    cdef void _maybe_pause(self):
        if self._consumed_as != CONSUMED_STREAM:
            return
        if self._buffered + self._tail_used > HIGH_WATER:
            self._connection.set_body_paused(self, True)

    cdef void c_feed(self, const char* at, size_t length):
        cdef Py_ssize_t new_total
        cdef Py_ssize_t offset
        cdef Py_ssize_t available
        cdef Py_ssize_t take
        cdef bint emitted
        if not self._body_active:
            self.reset_body(False, -1)
        new_total = self._total_read + <Py_ssize_t>length
        if new_total > self._max_size or (
            self._read_max_size >= 0 and new_total > self._read_max_size
        ):
            self._abort_reason = ABORT_TOO_LARGE
            self._cancel_stall_timer()
            self._clear_body_storage()
            self._connection.set_body_paused(self, False)
            self._wake()
            if self._discard_body and not self._transport.is_closing():
                self._transport.close()
            return
        if self._discard_body:
            self._total_read = new_total
            return
        emitted = False
        offset = 0
        while offset < <Py_ssize_t>length:
            self._ensure_body_tail(self._total_read + offset)
            available = self._tail_cap - self._tail_used
            take = <Py_ssize_t>length - offset
            if take > available:
                take = available
            memcpy(
                PyBytes_AS_STRING(self._body_tail) + self._tail_used,
                at + offset,
                <size_t>take,
            )
            self._tail_used += take
            offset += take
            if self._tail_used == self._tail_cap:
                self._seal_body_tail()
                emitted = True
        self._total_read = new_total
        if self._consumed_as == CONSUMED_STREAM:
            if emitted:
                self._wake()
            if self._waiting:
                self._reset_stall_timer()
            self._maybe_pause()
            return
        # body(): refresh stall on progress; wake only on complete/abort.
        if self._waiting:
            self._reset_stall_timer()

    cdef void c_complete(self):
        if not self._body_active:
            return
        self._body_complete = True
        self._cancel_stall_timer()
        if self._discard_body:
            self._clear_body_storage()
            self._maybe_recycle()
            return
        self._seal_body_tail()
        if self._consumed_as == CONSUMED_STREAM:
            self._wake()
            self._maybe_recycle()
            return
        if self._abort_reason != ABORT_NONE:
            self._clear_body_storage()
            self._wake()
            self._maybe_recycle()
            return
        if self._consumed_as == CONSUMED_BODY:
            if self._cached is None:
                self._cached = self._body_to_bytes()
        self._wake()
        self._maybe_recycle()

    cdef void c_abort(self):
        if self._abort_reason != ABORT_NONE:
            return
        self._free_compressors()
        self._abort_reason = ABORT_DISCONNECTED
        self._body_complete = True
        self._cancel_stall_timer()
        self._clear_body_storage()
        if self._connection is not None:
            self._connection.set_body_paused(self, False)
        self._wake()
        self._maybe_recycle()

    async def _wait_for_body_data(self):
        if self._abort_reason != ABORT_NONE:
            self._clear_body_storage()
            self._raise_abort()
        if self.disconnected:
            self._clear_body_storage()
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
            self._clear_body_storage()
            self._raise_abort()

    async def stream(self, max_chunk=None):
        cdef int index
        cdef object chunk
        cdef object out
        cdef Py_ssize_t chunk_size
        cdef Py_ssize_t offset
        cdef Py_ssize_t remaining
        cdef Py_ssize_t consumed
        if self._discard_body:
            raise StarioRuntime(
                "Request body is no longer available after its handler finished."
            )
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
            if chunk_size >= STREAM_CHUNK_LIMIT:
                raise ValueError(
                    f"max_chunk ({chunk_size}) must be lower than "
                    f"stream chunk limit ({STREAM_CHUNK_LIMIT})"
                )
        self._stream_max_chunk = chunk_size
        self._consumed_as = CONSUMED_STREAM
        if self._cached is not None:
            yield self._cached
            return
        self._maybe_continue()
        index = 0
        offset = 0
        while True:
            while self._chunks is not None and index < len(self._chunks):
                chunk = self._chunks[index]
                remaining = len(chunk) - offset
                if remaining <= chunk_size:
                    out = chunk if offset == 0 else chunk[offset:]
                    consumed = remaining
                    index += 1
                    offset = 0
                else:
                    out = chunk[offset : offset + chunk_size]
                    consumed = chunk_size
                    offset += chunk_size
                self._buffered -= consumed
                if self._buffered < LOW_WATER:
                    self._connection.set_body_paused(self, False)
                yield out
            if index:
                self._chunks.clear()
                index = 0
            if self._body_complete:
                return
            await self._wait_for_body_data()

    async def read(self, max_size=None):
        if self._discard_body:
            raise StarioRuntime(
                "Request body is no longer available after its handler finished."
            )
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
        while not self._body_complete:
            if self._abort_reason != ABORT_NONE:
                self._raise_abort()
            if (
                self._read_max_size >= 0
                and self._total_read > self._read_max_size
            ):
                raise HttpException(413, "Request body too large")
            await self._wait_for_body_data()
        if self._abort_reason != ABORT_NONE:
            self._raise_abort()
        if self._cached is None:
            self._cached = self._body_to_bytes()
        if max_size is not None and len(self._cached) > max_size:
            raise HttpException(413, "Request body too large")
        return self._cached


cdef class RequestHeaders(Headers):
    """Read-mostly request headers backed by the owning exchange arena."""

    cdef object _owner
    cdef bint _request_materialized

    def __init__(self, RequestExchange owner):
        Headers.__init__(self)
        self._owner = owner
        self._request_materialized = False

    cdef void _materialize(self):
        cdef RequestExchange owner
        cdef RawHeader* header
        cdef Py_ssize_t index
        cdef object key
        cdef object value
        cdef object existing
        if self._request_materialized:
            return
        owner = <RequestExchange>self._owner
        for index in range(owner._req_raw_count):
            header = &owner._req_raw_headers[index]
            key = _intern_name(
                owner._req_arena + header.name_offset,
                header.name_length,
            )
            value = PyBytes_FromStringAndSize(
                owner._req_arena + header.value_offset,
                header.value_length,
            )
            existing = self._data.get(key)
            if existing is None:
                self._data[key] = value
            elif type(existing) is list:
                existing.append(value)
            else:
                self._data[key] = [existing, value]
        self._request_materialized = True

    cdef object c_get(self, object name):
        cdef RequestExchange owner
        cdef RawHeader* header
        cdef bytes key
        cdef const char* query
        cdef Py_ssize_t query_length
        cdef Py_ssize_t index
        cdef object value
        if self._request_materialized:
            value = self._data.get(name)
            if value is None:
                return None
            if type(value) is bytes:
                return value
            return value[0]
        key = name
        query = key
        query_length = len(key)
        owner = <RequestExchange>self._owner
        for index in range(owner._req_raw_count):
            header = &owner._req_raw_headers[index]
            if (
                header.name_length == <uint32_t>query_length
                and memcmp(
                    owner._req_arena + header.name_offset,
                    query,
                    <size_t>query_length,
                ) == 0
            ):
                return PyBytes_FromStringAndSize(
                    owner._req_arena + header.value_offset,
                    header.value_length,
                )
        return None

    cdef void c_set(self, object name, object value):
        self._materialize()
        self._data[name] = value

    cdef void c_add(self, object name, object value):
        cdef object existing
        self._materialize()
        existing = self._data.get(name)
        if existing is None:
            self._data[name] = value
        elif type(existing) is list:
            existing.append(value)
        else:
            self._data[name] = [existing, value]

    cdef void c_remove(self, object name):
        self._materialize()
        self._data.pop(name, None)

    cdef void c_clear(self):
        self._materialize()
        self._data.clear()

    cdef void c_reset(self):
        self._data.clear()
        self._request_materialized = False

    cdef object c_request_host(self):
        cdef RequestExchange owner
        cdef RawHeader* header
        if self._request_materialized:
            return self.c_get(b"host")
        owner = <RequestExchange>self._owner
        if owner._req_host_index < 0:
            return None
        header = &owner._req_raw_headers[owner._req_host_index]
        return PyBytes_FromStringAndSize(
            owner._req_arena + header.value_offset,
            header.value_length,
        )

    def get(self, str name, default=None):
        cdef object wire = self.c_get(_encode_name(name))
        if wire is None:
            return default
        return wire.decode("latin-1")

    def unsafe_get(self, name, default=None):
        cdef object value = self.c_get(name)
        if value is None:
            return default
        return value

    def getlist(self, str name):
        return [
            value.decode("latin-1")
            for value in self.unsafe_getlist(_encode_name(name))
        ]

    def unsafe_getlist(self, name):
        cdef RequestExchange owner
        cdef RawHeader* header
        cdef bytes key
        cdef const char* query
        cdef Py_ssize_t query_length
        cdef Py_ssize_t index
        cdef object value
        cdef list result
        if self._request_materialized:
            value = self._data.get(name)
            if value is None:
                return []
            if type(value) is bytes:
                return [value]
            return list(value)
        key = name
        query = key
        query_length = len(key)
        owner = <RequestExchange>self._owner
        result = []
        for index in range(owner._req_raw_count):
            header = &owner._req_raw_headers[index]
            if (
                header.name_length == <uint32_t>query_length
                and memcmp(
                    owner._req_arena + header.name_offset,
                    query,
                    <size_t>query_length,
                ) == 0
            ):
                result.append(
                    PyBytes_FromStringAndSize(
                        owner._req_arena + header.value_offset,
                        header.value_length,
                    )
                )
        return result

    def items(self):
        return [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in self.unsafe_items()
        ]

    def unsafe_items(self):
        cdef list result = []
        cdef object name
        cdef object value
        cdef object item
        self._materialize()
        for name, value in self._data.items():
            if type(value) is list:
                for item in value:
                    result.append((name, item))
            else:
                result.append((name, value))
        return result

    def __contains__(self, name):
        try:
            return self.c_get(_encode_name(name)) is not None
        except ValueError:
            return False

    def __len__(self):
        self._materialize()
        return len(self._data)

    def __repr__(self):
        self._materialize()
        return f"RequestHeaders({self._data!r})"

    @property
    def materialized(self):
        """Whether a mutating/full-map operation copied the request arena."""
        return self._request_materialized


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

cdef int F_CONTENT_LENGTH = 0x20
cdef int PAUSE_WRITE = 1
cdef int PAUSE_PIPELINE = 2
cdef int PAUSE_BODY = 4
cdef int PARSER_QUANTUM = 512 * 1024
cdef int MAX_PENDING_EXCHANGES = 64
# Handlers always start at headers-complete. Body bytes accumulate on the exchange;
# body() awaits one completion Event, stream() drains with backpressure.

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
    cdef int head_bytes
    cdef int max_header_bytes
    cdef int max_body_bytes
    cdef bint rejected
    cdef bint request_dispatched
    cdef bint request_keep_alive
    cdef int pause_reasons
    cdef object body_pause_owner
    cdef object held_data
    cdef Py_ssize_t held_offset
    cdef bint pump_scheduled

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
        self.head_bytes = 0
        self.max_header_bytes = max_header_bytes
        self.max_body_bytes = max_body_bytes
        self.rejected = False
        self.request_dispatched = False
        self.request_keep_alive = True

    def connection_made(self, transport):
        self.transport = transport
        self.closed = False
        self.disconnect = None
        self.pause_reasons = 0
        self.body_pause_owner = None
        self.held_data = None
        self.held_offset = 0
        self.pump_scheduled = False
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
        if self.held_data is not None:
            self.held_data = self.held_data[self.held_offset :] + data
            self.held_offset = 0
            return
        if self.pause_reasons:
            self.held_data = data
            self.held_offset = 0
            return
        self._pump_data(data, 0)

    cdef void _pump_data(self, object data, Py_ssize_t offset):
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
        if self.rejected:
            return
        try:
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
        except Exception:
            self._close_error(400, "Invalid HTTP request")

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
        cdef Request request
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
            request = self._build_request(exchange, exchange)
            exchange.reset_body(
                exchange._req_expect_continue,
                <Py_ssize_t>content_length if flags & F_CONTENT_LENGTH else -1,
            )
            self._dispatch(exchange, request)
        except Exception:
            self._close_error(400, "Invalid HTTP request")

    cdef void _on_body(self, const char* at, size_t length) noexcept:
        if self.rejected or self.reading_exchange is None:
            return
        try:
            self.reading_exchange.c_feed(at, length)
        except Exception:
            self._close_error(400, "Invalid HTTP request")

    cdef void _on_message_complete(self) noexcept:
        cdef RequestExchange exchange
        if self.rejected:
            return
        exchange = self.reading_exchange
        try:
            if exchange is not None and exchange._body_active:
                exchange.c_complete()
        except Exception:
            self._close_error(400, "Invalid HTTP request")
        self.reading_exchange = None

    cdef Request _build_request(self, RequestExchange exchange, object body):
        cdef object method
        cdef object version
        cdef object url
        cdef tuple split
        cdef Request request = exchange.req
        if exchange._req_url_length > 0:
            url = PyBytes_FromStringAndSize(
                exchange._req_arena + exchange._req_url_offset,
                exchange._req_url_length,
            )
        else:
            url = b""
        split = _split_request_target(url)
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
        if self.noop_span is not None:
            span = self.noop_span
        else:
            span = self.tracer.create("request") if self.tracer is not None else None
        exchange.span = span
        if self.active_exchange is None:
            self._start_exchange(exchange, True)
        else:
            if len(self.pending_exchanges) >= MAX_PENDING_EXCHANGES:
                self._close_error(429, "Too many pipelined requests")
                return
            self.pending_exchanges.append(exchange)
            self._set_pause_reason(PAUSE_PIPELINE, True)

    cdef void _start_exchange(self, RequestExchange exchange, bint eager_start):
        self.active_exchange = exchange
        exchange.start_response()
        self.app.create_task(
            self._run(exchange),
            loop=self.loop,
            eager_start=eager_start,
        )

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
        # User may have set Connection: close on the response Headers dict.
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
            next_exchange = self.pending_exchanges.pop(0)
            self._start_exchange(next_exchange, False)
            if not self.pending_exchanges:
                self._set_pause_reason(PAUSE_PIPELINE, False)
            return
        self._set_pause_reason(PAUSE_PIPELINE, False)

    cdef void _close_error(self, int status, object message) noexcept:
        cdef object transport
        cdef object body
        cdef object date
        if self.rejected:
            return
        self.rejected = True
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
