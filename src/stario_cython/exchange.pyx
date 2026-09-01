# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True
"""One request's lifecycle: headers, arena-backed request, body, and response.

``Headers`` lives here (one extension). ``RequestExchange`` is pooled. The
protocol appends URL and header fragments into the arena; this module indexes
Host/Cookie/Authorization at parse time, keeps ``RequestHeaders`` read-only,
indexes query pairs on first read, scans cookies on demand, and writes the
response (including native compression).
"""

import asyncio
import http

from libc.stddef cimport size_t
from libc.stdint cimport int32_t, uint8_t, uint32_t, uint64_t
from libc.stdlib cimport free, malloc, realloc
from libc.stdio cimport sprintf
from libc.string cimport memcmp, memcpy
from cpython.bytearray cimport (
    PyByteArray_AS_STRING,
    PyByteArray_GET_SIZE,
    PyByteArray_Resize,
)
from cpython.bytes cimport (
    PyBytes_AS_STRING,
    PyBytes_FromStringAndSize,
    PyBytes_GET_SIZE,
)
from cpython.list cimport PyList_GET_ITEM, PyList_GET_SIZE
from cpython.exc cimport PyErr_Clear, PyErr_Occurred
from cpython.mem cimport PyMem_Free, PyMem_Malloc, PyMem_Realloc
from cpython.unicode cimport (
    PyUnicode_AsUTF8AndSize,
    PyUnicode_DecodeLatin1,
    PyUnicode_DecodeUTF8,
    PyUnicode_ReadChar,
)

from stario.exceptions import (
    ClientDisconnected,
    RequestBodyError,
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
from stario.http.invoke import on_handler_done

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
from stario_cython.timeouts import TIMEOUT_MODE as _PY_TIMEOUT_MODE

include "headers.pxi"

cdef int LOW_WATER = 128 * 1024
cdef int HIGH_WATER = 512 * 1024
cdef int BODY_HIGH_WATER = 64 * 1024
cdef int STREAM_CHUNK_LIMIT = 1024 * 1024
cdef int STREAM_CHUNK_CL = 256 * 1024
# Same bound as protocol SMALL_BODY_COMPLETE_DISPATCH: a declared oversize
# body this small can be discarded on keep-alive; larger or unknown lengths
# close the HTTP/1 connection (unbounded read-DoS).
cdef int SMALL_BODY_DRAIN = 256 * 1024
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

cdef int _TIMEOUT_MODE = 1
cdef int _TIMEOUT_OFF = 0


def _bind_timeout_mode():
    global _TIMEOUT_MODE
    _TIMEOUT_MODE = <int>_PY_TIMEOUT_MODE


_bind_timeout_mode()

cdef bytes STATUS_200 = b"HTTP/1.1 200 OK\r\n"
cdef bytes STATUS_204 = b"HTTP/1.1 204 No Content\r\n"
cdef bytes STATUS_304 = b"HTTP/1.1 304 Not Modified\r\n"
cdef bytes STATUS_308 = b"HTTP/1.1 308 Permanent Redirect\r\n"
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


cdef inline bint _token_equals(
    const char* value,
    size_t start,
    size_t end,
    const char* token,
    size_t token_length,
) noexcept:
    return _range_equals_ci(
        value + start,
        <Py_ssize_t>(end - start),
        token,
        <Py_ssize_t>token_length,
    )


cdef Py_ssize_t _parse_content_length(const char* s, size_t n) noexcept:
    cdef uint64_t value = 0
    cdef size_t i
    cdef unsigned int digit
    if n == 0:
        return -1
    for i in range(n):
        digit = <unsigned int>(<unsigned char>s[i] - 48)
        if digit > 9:
            return -1
        if value > (<uint64_t>9223372036854775807 - digit) / 10:
            return -1
        value = value * 10 + digit
    return <Py_ssize_t>value


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


cdef inline object _encoding_wire(int enc) noexcept:
    if enc == ENCODING_BR:
        return b"br"
    return b"gzip"


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
    if status == 308:
        return STATUS_308
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
    try:
        phrase = http.HTTPStatus(status).phrase.encode("ascii")
    except ValueError:
        phrase = b""
    return b"HTTP/1.1 %d %s\r\n" % (status, phrase)


cdef object _dec(size_t n):
    cdef char buf[16]
    cdef int i
    if n < 256:
        return _DEC_SMALL[n]
    i = sprintf(buf, "%zu", n)
    return PyBytes_FromStringAndSize(buf, i)


cdef inline int _hex_nibble(unsigned char c) noexcept:
    if 48 <= c <= 57:
        return c - 48
    if 65 <= c <= 70:
        return c - 55
    if 97 <= c <= 102:
        return c - 87
    return -1


cdef object _decode_latin1(const char* s, Py_ssize_t n):
    if n <= 0:
        return ""
    return PyUnicode_DecodeLatin1(s, n, NULL)


cdef int _latin1_stack(
    object name,
    char* buf,
    Py_ssize_t cap,
    Py_ssize_t* out_n,
) except -1:
    """Write a Latin-1 ``str`` into ``buf``. No Python ``bytes`` object."""
    cdef Py_ssize_t i
    cdef Py_ssize_t n = len(name)
    cdef unsigned int ch
    if n > cap:
        raise ValueError("name is too long")
    for i in range(n):
        ch = PyUnicode_ReadChar(name, i)
        if ch > 255:
            raise UnicodeEncodeError(
                "latin-1", name, i, i + 1, "ordinal not in range(256)"
            )
        buf[i] = <char>ch
    out_n[0] = n
    return 0


cdef object _unquote_plus_component(const char* s, Py_ssize_t n):
    """Match ``urllib.parse.unquote_plus`` on a Latin-1 query component."""
    cdef Py_ssize_t i = 0
    cdef Py_ssize_t w
    cdef int h1
    cdef int h2
    cdef unsigned char c
    cdef char* buf
    cdef list parts
    cdef object chunk
    if n <= 0:
        return ""
    buf = <char*>PyMem_Malloc(<size_t>n)
    if buf == NULL:
        raise MemoryError()
    parts = []
    try:
        while i < n:
            if <unsigned char>s[i] >= 128:
                parts.append(_decode_latin1(s + i, 1))
                i += 1
                continue
            w = 0
            while i < n and <unsigned char>s[i] < 128:
                c = <unsigned char>s[i]
                if c == 43:
                    buf[w] = 32
                    w += 1
                    i += 1
                elif c == 37 and i + 2 < n:
                    h1 = _hex_nibble(<unsigned char>s[i + 1])
                    h2 = _hex_nibble(<unsigned char>s[i + 2])
                    if h1 >= 0 and h2 >= 0:
                        buf[w] = <char>((h1 << 4) | h2)
                        w += 1
                        i += 3
                    else:
                        buf[w] = 37
                        w += 1
                        i += 1
                else:
                    buf[w] = <char>c
                    w += 1
                    i += 1
            if w:
                chunk = PyUnicode_DecodeUTF8(buf, w, <const char*>b"replace")
                parts.append(chunk)
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        return "".join(parts)
    finally:
        PyMem_Free(buf)


cdef inline bint _span_needs_unquote(const char* s, Py_ssize_t n) noexcept:
    cdef Py_ssize_t i
    for i in range(n):
        if s[i] == 37 or s[i] == 43:
            return True
    return False


cdef inline bint _span_all_ascii(const char* s, Py_ssize_t n) noexcept:
    cdef Py_ssize_t i
    for i in range(n):
        if <unsigned char>s[i] >= 128:
            return False
    return True


cdef object _decode_query_component(const char* s, Py_ssize_t n):
    if n <= 0:
        return ""
    if _span_needs_unquote(s, n):
        return _unquote_plus_component(s, n)
    return _decode_latin1(s, n)


cdef object _decode_utf8_replace(const char* s, Py_ssize_t n):
    if n <= 0:
        return ""
    return PyUnicode_DecodeUTF8(s, n, <const char*>b"replace")


cdef Py_ssize_t _query_unquote_ascii_inplace(char* s, Py_ssize_t n) noexcept:
    """Percent-decode ``+`` / ``%XX`` in place. Output length is always ≤ n."""
    cdef Py_ssize_t r = 0
    cdef Py_ssize_t w = 0
    cdef int h1
    cdef int h2
    cdef unsigned char c
    while r < n:
        c = <unsigned char>s[r]
        if c == 43:
            s[w] = 32
            r += 1
            w += 1
        elif c == 37 and r + 2 < n:
            h1 = _hex_nibble(<unsigned char>s[r + 1])
            h2 = _hex_nibble(<unsigned char>s[r + 2])
            if h1 >= 0 and h2 >= 0:
                s[w] = <char>((h1 << 4) | h2)
                r += 3
                w += 1
            else:
                s[w] = 37
                r += 1
                w += 1
        else:
            s[w] = <char>c
            r += 1
            w += 1
    return w


cdef int _next_query_pair(
    const char* s,
    Py_ssize_t n,
    Py_ssize_t* i,
    Py_ssize_t* name_start,
    Py_ssize_t* name_end,
    Py_ssize_t* val_start,
    Py_ssize_t* val_end,
) noexcept:
    """Advance ``i`` to the next ``name[=value]`` pair. 1 found, 0 done."""
    cdef Py_ssize_t start
    cdef Py_ssize_t eq
    cdef Py_ssize_t end
    while i[0] < n:
        start = i[0]
        eq = -1
        while i[0] < n and s[i[0]] != 38:
            if eq < 0 and s[i[0]] == 61:
                eq = i[0]
            i[0] += 1
        end = i[0]
        if i[0] < n and s[i[0]] == 38:
            i[0] += 1
        if start >= end:
            continue
        if eq < 0:
            name_start[0] = start
            name_end[0] = end
            val_start[0] = end
            val_end[0] = end
        else:
            name_start[0] = start
            name_end[0] = eq
            val_start[0] = eq + 1
            val_end[0] = end
        return 1
    return 0


cdef enum:
    QUERY_PARAM_INIT = 16
    QUERY_NAME_INPLACE = 1


ctypedef struct RawQueryParam:
    Py_ssize_t name_off
    Py_ssize_t name_len
    Py_ssize_t value_off
    Py_ssize_t value_len
    int flags


cdef bint _query_name_matches(
    const char* buf,
    RawQueryParam* param,
    const char* key,
    Py_ssize_t klen,
    object key_obj,
) except -1:
    cdef const char* name = buf + param.name_off
    cdef Py_ssize_t nlen = param.name_len
    if param.flags & QUERY_NAME_INPLACE:
        if _span_all_ascii(name, nlen):
            return nlen == klen and memcmp(name, key, <size_t>klen) == 0
        return _decode_utf8_replace(name, nlen) == key_obj
    if not _span_needs_unquote(name, nlen):
        if _span_all_ascii(name, nlen):
            return nlen == klen and memcmp(name, key, <size_t>klen) == 0
        return _decode_latin1(name, nlen) == key_obj
    return _decode_query_component(name, nlen) == key_obj


cdef class ParsedQuery:
    """Query index. First read fills C name/value spans.

    Plain ASCII names stay on the original bytes (arena or ``bytes``) with no
    memcpy. Names that are ASCII ``+`` / ``%XX`` need a private copy so they
    can decode in place (output never grows; leftover tail is ignored via the
    stored length). ``get`` memcmps typical ASCII names like headers, then
    decodes only that value. First ``name=`` wins. ``items`` / ``as_dict`` /
    ``as_lists`` still decode everything.
    """

    cdef bytes _raw
    cdef object _owner
    cdef Py_ssize_t _off
    cdef Py_ssize_t _len
    cdef char* _copy
    cdef Py_ssize_t _copy_len
    cdef Py_ssize_t _copy_cap
    cdef RawQueryParam* _params
    cdef Py_ssize_t _pcount
    cdef Py_ssize_t _pcap
    cdef bint _indexed

    def __cinit__(self):
        self._raw = None
        self._owner = None
        self._off = 0
        self._len = 0
        self._copy = NULL
        self._copy_len = 0
        self._copy_cap = 0
        self._params = NULL
        self._pcount = 0
        self._pcap = 0
        self._indexed = False

    def __dealloc__(self):
        if self._copy != NULL:
            PyMem_Free(self._copy)
            self._copy = NULL
        if self._params != NULL:
            PyMem_Free(self._params)
            self._params = NULL

    def __init__(self, raw=b""):
        self.bind_bytes(raw)

    cdef void _reset_view(self) noexcept:
        self._indexed = False
        self._pcount = 0
        self._copy_len = 0

    cdef void bind_bytes(self, object raw) noexcept:
        self._raw = raw if raw else b""
        self._owner = None
        self._off = 0
        self._len = PyBytes_GET_SIZE(self._raw) if self._raw else 0
        self._reset_view()

    cdef void bind_span(self, object owner, Py_ssize_t off, Py_ssize_t n) noexcept:
        """Point at request-arena query bytes. Copied only if a name needs unquote."""
        self._raw = None
        self._owner = owner
        self._off = off
        self._len = n
        self._reset_view()

    cdef bint _resolve_span(self, const char** out, Py_ssize_t* n) noexcept:
        cdef RequestExchange owner
        n[0] = self._len
        if n[0] <= 0:
            out[0] = NULL
            return False
        if self._raw is not None:
            out[0] = PyBytes_AS_STRING(self._raw)
            return True
        if self._owner is not None:
            owner = <RequestExchange>self._owner
            if owner._req_arena != NULL:
                out[0] = owner._req_arena + self._off
                return True
        out[0] = NULL
        return False

    cdef int _grow_params(self, Py_ssize_t need) except -1:
        cdef Py_ssize_t cap = self._pcap
        cdef void* p
        if cap >= need:
            return 0
        if cap == 0:
            cap = QUERY_PARAM_INIT
        while cap < need:
            cap *= 2
        if self._params == NULL:
            p = PyMem_Malloc(<size_t>cap * sizeof(RawQueryParam))
        else:
            p = PyMem_Realloc(self._params, <size_t>cap * sizeof(RawQueryParam))
        if p == NULL:
            raise MemoryError()
        self._params = <RawQueryParam*>p
        self._pcap = cap
        return 0

    cdef int _ensure_copy(self, const char* src, Py_ssize_t n) except -1:
        cdef void* p
        if n <= 0:
            self._copy_len = 0
            return 0
        if self._copy_cap < n:
            if self._copy == NULL:
                p = PyMem_Malloc(<size_t>n)
            else:
                p = PyMem_Realloc(self._copy, <size_t>n)
            if p == NULL:
                raise MemoryError()
            self._copy = <char*>p
            self._copy_cap = n
        memcpy(self._copy, src, <size_t>n)
        self._copy_len = n
        return 0

    cdef const char* _query_buf(self) noexcept:
        cdef const char* src = NULL
        cdef Py_ssize_t n = 0
        if self._copy_len > 0:
            return self._copy
        if self._resolve_span(&src, &n):
            return src
        return NULL

    cdef void _drop_query_owner(self) noexcept:
        self._raw = None
        self._owner = None
        self._off = 0

    cdef void _ensure_index(self) except *:
        cdef const char* src = NULL
        cdef Py_ssize_t n = 0
        cdef Py_ssize_t i = 0
        cdef Py_ssize_t name_start
        cdef Py_ssize_t name_end
        cdef Py_ssize_t val_start
        cdef Py_ssize_t val_end
        cdef Py_ssize_t name_len
        cdef Py_ssize_t p
        cdef bint need_copy = False
        cdef char* buf
        cdef RawQueryParam* param
        if self._indexed:
            return
        self._pcount = 0
        if self._resolve_span(&src, &n) and src != NULL and n > 0:
            while _next_query_pair(
                src, n, &i, &name_start, &name_end, &val_start, &val_end
            ):
                self._grow_params(self._pcount + 1)
                param = &self._params[self._pcount]
                name_len = name_end - name_start
                param.name_off = name_start
                param.name_len = name_len
                param.value_off = val_start
                param.value_len = val_end - val_start
                param.flags = 0
                if (
                    name_len > 0
                    and _span_needs_unquote(src + name_start, name_len)
                    and _span_all_ascii(src + name_start, name_len)
                ):
                    need_copy = True
                self._pcount += 1
            if need_copy:
                self._ensure_copy(src, n)
                buf = self._copy
                for p in range(self._pcount):
                    param = &self._params[p]
                    name_len = param.name_len
                    if (
                        name_len > 0
                        and _span_needs_unquote(buf + param.name_off, name_len)
                        and _span_all_ascii(buf + param.name_off, name_len)
                    ):
                        param.name_len = _query_unquote_ascii_inplace(
                            buf + param.name_off, name_len
                        )
                        param.flags = QUERY_NAME_INPLACE
                self._drop_query_owner()
        else:
            self._drop_query_owner()
        self._indexed = True

    cdef object _decode_name_at(self, Py_ssize_t i):
        cdef RawQueryParam* param = &self._params[i]
        cdef const char* buf = self._query_buf()
        cdef const char* name
        cdef Py_ssize_t nlen = param.name_len
        if buf == NULL:
            return ""
        name = buf + param.name_off
        if param.flags & QUERY_NAME_INPLACE:
            if _span_all_ascii(name, nlen):
                return _decode_latin1(name, nlen)
            return _decode_utf8_replace(name, nlen)
        return _decode_query_component(name, nlen)

    cdef object _decode_value_at(self, Py_ssize_t i):
        cdef RawQueryParam* param = &self._params[i]
        cdef const char* buf = self._query_buf()
        if buf == NULL:
            return ""
        return _decode_query_component(
            buf + param.value_off, param.value_len
        )

    cdef bint _name_eq(self, Py_ssize_t i, const char* kbuf, Py_ssize_t klen, object key) except -1:
        cdef const char* buf = self._query_buf()
        if buf == NULL:
            return False
        return _query_name_matches(buf, &self._params[i], kbuf, klen, key)

    cdef object _index_get(self, object key, object default):
        cdef const char* kbuf
        cdef Py_ssize_t klen
        cdef Py_ssize_t i
        self._ensure_index()
        if self._pcount == 0:
            return default
        kbuf = PyUnicode_AsUTF8AndSize(key, &klen)
        if kbuf == NULL:
            raise
        for i in range(self._pcount):
            if self._name_eq(i, kbuf, klen, key):
                return self._decode_value_at(i)
        return default

    cdef list _index_getlist(self, object key):
        cdef const char* kbuf
        cdef Py_ssize_t klen
        cdef Py_ssize_t i
        cdef list out = []
        self._ensure_index()
        if self._pcount == 0:
            return out
        kbuf = PyUnicode_AsUTF8AndSize(key, &klen)
        if kbuf == NULL:
            raise
        for i in range(self._pcount):
            if self._name_eq(i, kbuf, klen, key):
                out.append(self._decode_value_at(i))
        return out

    cdef bint _index_contains(self, object key) except -1:
        cdef const char* kbuf
        cdef Py_ssize_t klen
        cdef Py_ssize_t i
        self._ensure_index()
        if self._pcount == 0:
            return False
        kbuf = PyUnicode_AsUTF8AndSize(key, &klen)
        if kbuf == NULL:
            raise
        for i in range(self._pcount):
            if self._name_eq(i, kbuf, klen, key):
                return True
        return False

    cdef bint _has_any(self) except -1:
        self._ensure_index()
        return self._pcount != 0

    def get(self, key, default=None):
        if not isinstance(key, str):
            return default
        return self._index_get(key, default)

    def getlist(self, key):
        if not isinstance(key, str):
            return []
        return self._index_getlist(key)

    def items(self):
        cdef Py_ssize_t i
        cdef list out = []
        self._ensure_index()
        for i in range(self._pcount):
            out.append((self._decode_name_at(i), self._decode_value_at(i)))
        return out

    def as_dict(self, *, last=False):
        cdef dict out = {}
        cdef Py_ssize_t i
        cdef object key
        self._ensure_index()
        for i in range(self._pcount):
            key = self._decode_name_at(i)
            if last or key not in out:
                out[key] = self._decode_value_at(i)
        return out

    def as_lists(self):
        cdef dict out = {}
        cdef Py_ssize_t i
        cdef object key
        cdef list existing
        self._ensure_index()
        for i in range(self._pcount):
            key = self._decode_name_at(i)
            existing = out.get(key)
            if existing is None:
                out[key] = [self._decode_value_at(i)]
            else:
                existing.append(self._decode_value_at(i))
        return out

    def __contains__(self, key):
        if not isinstance(key, str):
            return False
        return self._index_contains(key)

    def __bool__(self):
        return self._has_any()

    def __len__(self):
        cdef set seen = set()
        cdef Py_ssize_t i
        self._ensure_index()
        for i in range(self._pcount):
            seen.add(self._decode_name_at(i))
        return len(seen)

    def __eq__(self, other):
        if isinstance(other, ParsedQuery):
            return self.items() == other.items()
        return NotImplemented

    def __repr__(self):
        return f"ParsedQuery({self.as_lists()!r})"


cdef inline bint _is_ows(char c) noexcept:
    return c == 32 or c == 9


cdef void _strip_span(
    const char* s,
    Py_ssize_t* start,
    Py_ssize_t* end,
) noexcept:
    while start[0] < end[0] and _is_ows(s[start[0]]):
        start[0] += 1
    while end[0] > start[0] and _is_ows(s[end[0] - 1]):
        end[0] -= 1


cdef bint _cookie_attr_name(const char* s, Py_ssize_t n) noexcept:
    cdef char buf[16]
    cdef Py_ssize_t i
    cdef unsigned char c
    if n == 0 or n > 8:
        return False
    for i in range(n):
        c = <unsigned char>s[i]
        if 65 <= c <= 90:
            c += 32
        buf[i] = <char>c
    if n == 4 and memcmp(buf, "path", 4) == 0:
        return True
    if n == 6 and (
        memcmp(buf, "domain", 6) == 0 or memcmp(buf, "secure", 6) == 0
    ):
        return True
    if n == 7 and (
        memcmp(buf, "expires", 7) == 0
        or memcmp(buf, "max-age", 7) == 0
        or memcmp(buf, "comment", 7) == 0
        or memcmp(buf, "version", 7) == 0
    ):
        return True
    if n == 8 and (
        memcmp(buf, "httponly", 8) == 0 or memcmp(buf, "samesite", 8) == 0
    ):
        return True
    return False


cdef object _cookie_unquote(const char* s, Py_ssize_t n):
    cdef Py_ssize_t i
    cdef Py_ssize_t w
    cdef unsigned char d1
    cdef unsigned char d2
    cdef char* buf
    cdef bint escaped = False
    if n >= 2 and s[0] == 34 and s[n - 1] == 34:
        s += 1
        n -= 2
        for i in range(n):
            if s[i] == 92:
                escaped = True
                break
        if not escaped:
            return _decode_latin1(s, n)
        buf = <char*>PyMem_Malloc(<size_t>n)
        if buf == NULL:
            raise MemoryError()
        try:
            i = 0
            w = 0
            while i < n:
                if s[i] == 92 and i + 1 < n:
                    d1 = <unsigned char>s[i + 1]
                    if (
                        48 <= d1 <= 51
                        and i + 3 < n
                        and 48 <= <unsigned char>s[i + 2] <= 55
                        and 48 <= <unsigned char>s[i + 3] <= 55
                    ):
                        buf[w] = <char>(
                            ((d1 - 48) << 6)
                            | ((<unsigned char>s[i + 2] - 48) << 3)
                            | (<unsigned char>s[i + 3] - 48)
                        )
                        w += 1
                        i += 4
                    else:
                        buf[w] = <char>d1
                        w += 1
                        i += 2
                else:
                    buf[w] = s[i]
                    w += 1
                    i += 1
            return _decode_latin1(buf, w)
        finally:
            PyMem_Free(buf)
    return _decode_latin1(s, n)


cdef int _next_cookie_pair(
    const char* s,
    Py_ssize_t n,
    Py_ssize_t* i,
    Py_ssize_t* name_start,
    Py_ssize_t* name_end,
    Py_ssize_t* val_start,
    Py_ssize_t* val_end,
) noexcept:
    """Advance ``i`` to the next ``name=value`` pair. 1 found, 0 done."""
    cdef Py_ssize_t j
    while i[0] < n:
        while i[0] < n and _is_ows(s[i[0]]):
            i[0] += 1
        if i[0] >= n:
            return 0
        name_start[0] = i[0]
        while i[0] < n and s[i[0]] != 59 and s[i[0]] != 61:
            i[0] += 1
        if i[0] >= n or s[i[0]] == 59:
            while i[0] < n and s[i[0]] != 59:
                i[0] += 1
            if i[0] < n:
                i[0] += 1
            continue
        name_end[0] = i[0]
        _strip_span(s, name_start, name_end)
        i[0] += 1
        while i[0] < n and _is_ows(s[i[0]]):
            i[0] += 1
        val_start[0] = i[0]
        if i[0] < n and s[i[0]] == 34:
            j = i[0] + 1
            while j < n:
                if s[j] == 92 and j + 1 < n:
                    j += 2
                    continue
                if s[j] == 34:
                    j += 1
                    break
                j += 1
            val_end[0] = j
            while j < n and s[j] != 59:
                j += 1
            i[0] = j
        else:
            while i[0] < n and s[i[0]] != 59:
                i[0] += 1
            val_end[0] = i[0]
            _strip_span(s, val_start, val_end)
        if i[0] < n and s[i[0]] == 59:
            i[0] += 1
        if name_end[0] <= name_start[0]:
            continue
        if s[name_start[0]] == 36:
            continue
        if _cookie_attr_name(s + name_start[0], name_end[0] - name_start[0]):
            continue
        return 1
    return 0


cdef object _cookie_decode_value(const char* s, Py_ssize_t start, Py_ssize_t end):
    if end > start and s[start] == 34:
        return _cookie_unquote(s + start, end - start)
    return _decode_latin1(s + start, end - start)


cdef object _cookie_find_in_line(
    const char* s,
    Py_ssize_t n,
    const char* name,
    Py_ssize_t nlen,
):
    """Last ``name=`` in this header, or ``None``. Decodes only that value."""
    cdef Py_ssize_t i = 0
    cdef Py_ssize_t name_start
    cdef Py_ssize_t name_end
    cdef Py_ssize_t val_start
    cdef Py_ssize_t val_end
    cdef Py_ssize_t found_start = -1
    cdef Py_ssize_t found_end = 0
    while _next_cookie_pair(
        s, n, &i, &name_start, &name_end, &val_start, &val_end
    ):
        if name_end - name_start == nlen and memcmp(
            s + name_start, name, <size_t>nlen
        ) == 0:
            found_start = val_start
            found_end = val_end
    if found_start < 0:
        return None
    return _cookie_decode_value(s, found_start, found_end)


cdef void _parse_cookie_line(
    const char* s,
    Py_ssize_t n,
    dict out,
) except *:
    cdef Py_ssize_t i = 0
    cdef Py_ssize_t name_start
    cdef Py_ssize_t name_end
    cdef Py_ssize_t val_start
    cdef Py_ssize_t val_end
    while _next_cookie_pair(
        s, n, &i, &name_start, &name_end, &val_start, &val_end
    ):
        out[_decode_latin1(s + name_start, name_end - name_start)] = (
            _cookie_decode_value(s, val_start, val_end)
        )


cdef class ParsedCookies:
    """Cookie scan. ``get`` walks C pairs and decodes only the name you ask for.

    Later ``Cookie`` headers win; within one header, later pairs win. ``get``
    starts at the last header so unused earlier cookies stay untouched.
    ``as_dict`` / ``items`` parse everything.
    """

    def __cinit__(self):
        self._headers = None
        self._lines = None

    def __init__(self, lines=None):
        self._headers = None
        self._lines = []
        if lines:
            self._extend_lines(lines)

    cdef void bind_request_headers(self, object headers) noexcept:
        self._headers = headers
        self._lines = None

    cdef void _extend_lines(self, object lines) except *:
        cdef object line
        if self._lines is None:
            self._lines = []
        for line in lines:
            if isinstance(line, str):
                self._lines.append((<str>line).encode("latin-1"))
            elif line:
                self._lines.append(line)

    cdef list _cookie_lines(self):
        cdef object headers
        cdef list values
        cdef list out
        cdef object value
        if self._lines is not None:
            return self._lines
        headers = self._headers
        out = []
        if headers is None:
            self._lines = out
            return out
        if isinstance(headers, RequestHeaders):
            values = (<RequestHeaders>headers).c_getlist_n("cookie", 6)
            out.extend(values)
        else:
            for value in headers.getlist("cookie"):
                if isinstance(value, str):
                    out.append((<str>value).encode("latin-1"))
                elif value:
                    out.append(value)
        self._lines = out
        return out

    cdef object _get_arena(self, const char* name, Py_ssize_t nlen):
        cdef RequestHeaders headers
        cdef RequestExchange owner
        cdef RawHeader* header
        cdef Py_ssize_t index
        cdef object found
        headers = <RequestHeaders>self._headers
        owner = <RequestExchange>headers._owner
        if owner._req_cookie_index < 0:
            return None
        for index in range(owner._req_raw_count - 1, -1, -1):
            header = &owner._req_raw_headers[index]
            if header.name_length != 6 or memcmp(
                owner._req_arena + header.name_offset,
                "cookie",
                6,
            ) != 0:
                continue
            found = _cookie_find_in_line(
                owner._req_arena + header.value_offset,
                <Py_ssize_t>header.value_length,
                name,
                nlen,
            )
            if found is not None:
                return found
        return None

    cdef object _get_lines(self, const char* name, Py_ssize_t nlen):
        cdef list lines
        cdef Py_ssize_t i
        cdef bytes raw
        cdef object found
        lines = self._cookie_lines()
        i = PyList_GET_SIZE(lines)
        while i > 0:
            i -= 1
            raw = <bytes>lines[i]
            found = _cookie_find_in_line(
                PyBytes_AS_STRING(raw),
                PyBytes_GET_SIZE(raw),
                name,
                nlen,
            )
            if found is not None:
                return found
        return None

    def get(self, name, default=None):
        cdef char buf[HEADER_NAME_STACK]
        cdef Py_ssize_t n
        cdef object found
        if not isinstance(name, str):
            return default
        _latin1_stack(name, buf, HEADER_NAME_STACK, &n)
        if self._headers is not None and isinstance(self._headers, RequestHeaders):
            found = self._get_arena(buf, n)
        else:
            found = self._get_lines(buf, n)
        if found is None:
            return default
        return found

    def as_dict(self):
        cdef dict out = {}
        cdef RequestHeaders headers
        cdef bytes raw
        cdef object value
        if self._headers is not None and isinstance(self._headers, RequestHeaders):
            headers = <RequestHeaders>self._headers
            headers.c_parse_cookies(out)
            return out
        for raw in self._cookie_lines():
            _parse_cookie_line(
                PyBytes_AS_STRING(raw),
                PyBytes_GET_SIZE(raw),
                out,
            )
        return out

    def items(self):
        return list(self.as_dict().items())

    def keys(self):
        return list(self.as_dict().keys())

    def values(self):
        return list(self.as_dict().values())

    def __iter__(self):
        return iter(self.as_dict())

    def __len__(self):
        return len(self.as_dict())

    cdef bint _has_any(self):
        cdef list lines
        cdef bytes raw
        cdef RequestHeaders headers
        cdef RequestExchange owner
        cdef RawHeader* header
        cdef Py_ssize_t index
        cdef Py_ssize_t i
        cdef Py_ssize_t name_start
        cdef Py_ssize_t name_end
        cdef Py_ssize_t val_start
        cdef Py_ssize_t val_end
        if self._headers is not None and isinstance(self._headers, RequestHeaders):
            headers = <RequestHeaders>self._headers
            owner = <RequestExchange>headers._owner
            if owner._req_cookie_index < 0:
                return False
            for index in range(owner._req_raw_count):
                header = &owner._req_raw_headers[index]
                if header.name_length != 6 or memcmp(
                    owner._req_arena + header.name_offset,
                    "cookie",
                    6,
                ) != 0:
                    continue
                i = 0
                if _next_cookie_pair(
                    owner._req_arena + header.value_offset,
                    <Py_ssize_t>header.value_length,
                    &i,
                    &name_start,
                    &name_end,
                    &val_start,
                    &val_end,
                ):
                    return True
            return False
        lines = self._cookie_lines()
        for raw in lines:
            i = 0
            if _next_cookie_pair(
                PyBytes_AS_STRING(raw),
                PyBytes_GET_SIZE(raw),
                &i,
                &name_start,
                &name_end,
                &val_start,
                &val_end,
            ):
                return True
        return False

    def __bool__(self):
        return self._has_any()

    def __contains__(self, name):
        cdef object sentinel = object()
        if not isinstance(name, str):
            return False
        return self.get(name, sentinel) is not sentinel

    def __getitem__(self, name):
        cdef object sentinel = object()
        cdef object found = self.get(name, sentinel)
        if found is sentinel:
            raise KeyError(name)
        return found

    def __eq__(self, other):
        if isinstance(other, ParsedCookies):
            return self.as_dict() == other.as_dict()
        if isinstance(other, dict):
            return self.as_dict() == other
        return NotImplemented

    def __repr__(self):
        return f"ParsedCookies({self.as_dict()!r})"


cdef void _raise_readonly_request_headers() except *:
    raise StarioRuntime(
        "Request headers are read-only",
        help_text=(
            "Read incoming headers from req.headers. "
            "Set outgoing headers on the Writer (w.headers)."
        ),
    )


cdef inline bint _is_host_ws(char c) noexcept:
    return (
        c == 32
        or c == 9
        or c == 10
        or c == 13
        or c == 11
        or c == 12
    )


cdef inline bint _span_digits(const char* s, Py_ssize_t n) noexcept:
    cdef Py_ssize_t i
    if n <= 0:
        return False
    for i in range(n):
        if <unsigned char>s[i] < 48 or <unsigned char>s[i] > 57:
            return False
    return True


cdef object _ascii_lower_latin1(const char* s, Py_ssize_t n):
    """Lowercased ASCII copy of ``s`` as a Latin-1 Unicode string."""
    cdef char stack[512]
    cdef char* buf
    cdef bint heap = False
    cdef Py_ssize_t i
    cdef unsigned char c
    cdef object out
    if n <= 0:
        return ""
    if n <= 512:
        buf = stack
    else:
        buf = <char*>malloc(<size_t>n)
        if buf == NULL:
            return ""
        heap = True
    for i in range(n):
        c = <unsigned char>s[i]
        if 65 <= c <= 90:
            c += 32
        buf[i] = <char>c
    out = PyUnicode_DecodeLatin1(buf, n, NULL)
    if heap:
        free(buf)
    return out


cdef object _host_without_port_n(const char* s, Py_ssize_t n):
    """Match ``stario.http.host.host_without_port`` on a wire Host value."""
    cdef Py_ssize_t start = 0
    cdef Py_ssize_t end = n
    cdef Py_ssize_t i
    cdef Py_ssize_t bracket_end
    cdef Py_ssize_t colon
    cdef Py_ssize_t rest
    while start < end and _is_host_ws(s[start]):
        start += 1
    while end > start and _is_host_ws(s[end - 1]):
        end -= 1
    if start >= end:
        return ""
    if s[start] == 91:
        bracket_end = -1
        for i in range(start + 1, end):
            if s[i] == 93:
                bracket_end = i
                break
        if bracket_end < 0:
            return _ascii_lower_latin1(s + start, end - start)
        rest = bracket_end + 1
        if rest < end and (
            s[rest] != 58 or not _span_digits(s + rest + 1, end - rest - 1)
        ):
            return _ascii_lower_latin1(s + start, end - start)
        return _ascii_lower_latin1(s + start, bracket_end - start + 1)
    colon = -1
    for i in range(end - 1, start - 1, -1):
        if s[i] == 58:
            colon = i
            break
    if (
        colon > start
        and _span_digits(s + colon + 1, end - colon - 1)
    ):
        return _ascii_lower_latin1(s + start, colon - start)
    return _ascii_lower_latin1(s + start, end - start)


cdef class Request:
    """Request view. Same handler API as stario.http.request.Request."""

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
        self._query_bytes = query_bytes
        self._q_owner = None
        self._q_off = 0
        self._q_len = 0
        self._body = body
        self._cookies = None
        self._host = None
        self._rebind_query(query_bytes)

    cdef void _rebind_query(self, object query_bytes) noexcept:
        cdef ParsedQuery parsed
        if self._query is None:
            parsed = ParsedQuery.__new__(ParsedQuery)
            self._query = parsed
        else:
            parsed = <ParsedQuery>self._query
        if query_bytes is not None:
            parsed.bind_bytes(query_bytes)
        else:
            parsed.bind_span(None, 0, 0)

    cdef void bind_query_span(self, object owner, Py_ssize_t off, Py_ssize_t n) noexcept:
        cdef ParsedQuery parsed
        self._q_owner = owner
        self._q_off = off
        self._q_len = n
        self._query_bytes = None
        if self._query is None:
            parsed = ParsedQuery.__new__(ParsedQuery)
            self._query = parsed
        else:
            parsed = <ParsedQuery>self._query
        parsed.bind_span(owner, off, n)

    cdef void prefetch_host(self) noexcept:
        cdef RequestHeaders req_headers
        cdef RequestExchange owner
        cdef Headers hdrs
        cdef RawHeader* header
        cdef object host_wire
        cdef object host_str
        cdef const char* p
        cdef Py_ssize_t n = 0
        if self._host is not None:
            return
        if isinstance(self.headers, RequestHeaders):
            req_headers = <RequestHeaders>self.headers
            owner = <RequestExchange>req_headers._owner
            if owner._req_host_index < 0 or owner._req_arena == NULL:
                self._host = ""
                return
            header = &owner._req_raw_headers[owner._req_host_index]
            self._host = _host_without_port_n(
                owner._req_arena + header.value_offset,
                <Py_ssize_t>header.value_length,
            )
            return
        if isinstance(self.headers, Headers):
            hdrs = <Headers>self.headers
            host_wire = hdrs.c_get(b"host")
            if host_wire is None:
                self._host = ""
                return
            self._host = _host_without_port_n(
                PyBytes_AS_STRING(host_wire),
                PyBytes_GET_SIZE(host_wire),
            )
            return
        host_str = ""
        if self.headers is not None:
            host_str = self.headers.get("host") or ""
        if host_str is None:
            self._host = ""
            return
        if not isinstance(host_str, str):
            host_str = str(host_str)
        p = PyUnicode_AsUTF8AndSize(host_str, &n)
        if p == NULL:
            PyErr_Clear()
            self._host = ""
            return
        self._host = _host_without_port_n(p, n)

    cdef object _materialize_query(self):
        cdef RequestExchange owner
        if self._query_bytes is not None:
            return self._query_bytes
        if self._q_len <= 0 or self._q_owner is None:
            self._query_bytes = b""
            return self._query_bytes
        owner = <RequestExchange>self._q_owner
        self._query_bytes = PyBytes_FromStringAndSize(
            owner._req_arena + self._q_off,
            self._q_len,
        )
        return self._query_bytes

    @property
    def query_bytes(self):
        return self._materialize_query()

    @property
    def host(self):
        if self._host is None:
            self.prefetch_host()
            if self._host is None:
                self._host = ""
        return self._host

    @property
    def query(self):
        if self._query is None:
            self._rebind_query(self._query_bytes)
        return self._query

    @property
    def cookies(self):
        cdef ParsedCookies parsed
        if self._cookies is None:
            parsed = ParsedCookies.__new__(ParsedCookies)
            if isinstance(self.headers, RequestHeaders):
                parsed.bind_request_headers(<RequestHeaders>self.headers)
            else:
                parsed.__init__(self.headers.getlist("cookie"))
            self._cookies = parsed
        return self._cookies

    async def body(self, max_size=None):
        if max_size is not None and max_size < 0:
            raise ValueError("max_size must be non-negative.")
        if self._body is None:
            return b""
        if type(self._body) is bytes:
            if max_size is not None and len(self._body) > max_size:
                raise RequestBodyError(413, "Request body too large")
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
    def __cinit__(self):
        self._req_arena = NULL
        self._req_raw_headers = NULL
        self._req_url_offset = 0
        self._req_url_length = 0
        self._req_host_index = -1
        self._req_cookie_index = -1
        self._req_authorization_index = -1
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
        self._http2 = False
        self._h2_stream_id = 0
        self._h2_pending = b""
        self._h2_pending_off = 0
        self._h2_body_done = False
        self._h2_method = None
        self._h2_got_method = False
        self._h2_got_path = False
        self._h2_got_authority = False
        self._h2_dispatched = False
        self._h2_headers_done = False
        self._h2_headers_sent = False
        self._h2_headers_too_large = False
        self._h2_head_bytes = 0
        self._h2_awaiting_headers = False
        self._h2_header_deadline = 0.0
        self._h2_date_line = None
        self._h2_date_bare = None
        self._req_content_length = -1

    def __init__(self):
        self.headers = Headers()
        self.request_headers = RequestHeaders(self)
        self.req = Request()
        self._chunks = None
        self._cached = None
        self._data_ready = None
        self._stall_deadline = 0.0
        self._stall_touch = 0
        self._stall_seen = 0
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

    cdef object _h2_date_value(self):
        cdef object line = self._date_box[0]
        cdef bytes raw
        cdef Py_ssize_t n
        if line is self._h2_date_line and self._h2_date_bare is not None:
            return self._h2_date_bare
        raw = <bytes>line
        n = PyBytes_GET_SIZE(raw)
        if n > 8 and raw[0] == 100 and raw[n - 2] == 13:
            self._h2_date_bare = raw[6 : n - 2]
        else:
            self._h2_date_bare = raw
        self._h2_date_line = line
        return self._h2_date_bare

    cdef void _h2_respond(
        self,
        object body,
        object content_type,
        int status,
        Py_ssize_t nbytes,
    ):
        cdef Headers h = self.headers
        cdef object encoding
        cdef object flat
        cdef object scanned
        cdef object existing_ce = None
        cdef object existing_cl = None
        cdef const unsigned char* native_out = NULL
        cdef size_t native_len = 0
        cdef list nva
        cdef object payload = body
        if not h.c_empty():
            scanned = h.c_scan_respond(content_type)
            existing_ce = scanned[0]
            existing_cl = scanned[1]
        if not _may_have_body(status):
            payload = b""
            nbytes = 0
        elif existing_ce is None and self._may_compress(body, content_type, False, nbytes):
            encoding = _encoding_wire(self._req_encoding)
            flat = self._body_as_bytes(body)
            try:
                self._frame(flat, encoding, &native_out, &native_len)
                nbytes = <Py_ssize_t>native_len
                if existing_cl is not None:
                    h.c_require_respond_length(existing_cl, _dec(native_len))
                payload = PyBytes_FromStringAndSize(<char*>native_out, <Py_ssize_t>native_len)
                nva = [
                    (b":status", _dec(<size_t>status)),
                    (b"date", self._h2_date_value()),
                    (b"content-type", content_type),
                    (b"content-length", _dec(native_len)),
                    (b"content-encoding", encoding),
                ]
                if not h.c_vary_contains(b"accept-encoding"):
                    nva.append((b"vary", b"accept-encoding"))
                self._connection.h2_respond(self, nva, payload, True, True, True)
            finally:
                self._free_compressors()
            self._status_code = status
            self._declared_length = nbytes
            self._bytes_written = nbytes
            self._completed = True
            self._done()
            return
        if existing_cl is not None:
            h.c_require_respond_length(existing_cl, _dec(<size_t>nbytes))
        nva = [
            (b":status", _dec(<size_t>status)),
            (b"date", self._h2_date_value()),
        ]
        if _may_have_body(status):
            nva.append((b"content-type", content_type))
            nva.append((b"content-length", _dec(<size_t>nbytes)))
        if isinstance(payload, (list, tuple)):
            payload = b"".join(payload) if payload else b""
        self._connection.h2_respond(
            self, nva, payload if payload is not None else b"", False, True, True
        )
        self._status_code = status
        self._declared_length = nbytes
        self._bytes_written = nbytes if nbytes > 0 else 0
        self._completed = True
        self._done()

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
        if self._http2:
            self._connection.h2_write_data(self, view, False)
            return
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

    cdef int append_request_header(
        self,
        const char* name,
        size_t name_length,
        const char* value,
        size_t value_length,
        bint names_already_lower,
    ) noexcept:
        """Complete header in one pass (H2 delivers name and value together)."""
        if name_length == 0 or name_length >= <size_t>REQUEST_NAME_MAX:
            return -1
        if self._req_pending_header and self.finish_request_header() != 0:
            return -1
        if self._reserve_request_arena(<Py_ssize_t>(name_length + value_length)) != 0:
            return -1
        self._req_pending_name_offset = self._req_arena_len
        if names_already_lower:
            memcpy(self._req_arena + self._req_arena_len, name, name_length)
        else:
            _lower_copy(self._req_arena + self._req_arena_len, name, name_length)
        self._req_arena_len += <Py_ssize_t>name_length
        self._req_pending_name_length = <Py_ssize_t>name_length
        self._req_pending_value_offset = self._req_arena_len
        if value_length:
            memcpy(self._req_arena + self._req_arena_len, value, value_length)
        self._req_arena_len += <Py_ssize_t>value_length
        self._req_pending_value_length = <Py_ssize_t>value_length
        self._req_pending_header = True
        return self._commit_request_header()

    cdef bint header_value_equals(
        self,
        Py_ssize_t index,
        const char* value,
        size_t n,
    ) noexcept:
        cdef RawHeader* header
        if index < 0 or index >= self._req_raw_count:
            return False
        header = &self._req_raw_headers[index]
        if <size_t>header.value_length != n:
            return False
        if n == 0:
            return True
        return memcmp(
            self._req_arena + header.value_offset,
            value,
            n,
        ) == 0

    cdef int finish_request_header(self) noexcept:
        if not self._req_pending_header:
            return 0
        if self._req_pending_name_length == 0:
            return -1
        if self._req_pending_value_offset < 0:
            self._req_pending_value_offset = self._req_arena_len
        return self._commit_request_header()

    cdef int _commit_request_header(self) noexcept:
        cdef RawHeader* header
        cdef const char* name
        cdef const char* value
        cdef size_t name_length
        cdef size_t value_length
        cdef Py_ssize_t parsed
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
        elif name_length == 6:
            if memcmp(name, "cookie", 6) == 0:
                if self._req_cookie_index < 0:
                    self._req_cookie_index = self._req_raw_count
            elif memcmp(name, "expect", 6) == 0:
                if _contains_token(value, value_length, "100-continue", 12):
                    self._req_expect_continue = True
        elif name_length == 10 and memcmp(name, "connection", 10) == 0:
            if _contains_token(value, value_length, "close", 5):
                self._req_connection_close = True
        elif name_length == 13 and memcmp(name, "authorization", 13) == 0:
            if self._req_authorization_index < 0:
                self._req_authorization_index = self._req_raw_count
        elif name_length == 14 and memcmp(name, "content-length", 14) == 0:
            parsed = _parse_content_length(value, value_length)
            if parsed < 0:
                return -1
            if self._req_content_length >= 0 and self._req_content_length != parsed:
                return -1
            self._req_content_length = parsed
        elif name_length == 15 and memcmp(name, "accept-encoding", 15) == 0:
            if self._brotli_enabled or self._gzip_enabled:
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

    cdef void _clear_request_headers(self) noexcept:
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
        self._req_cookie_index = -1
        self._req_authorization_index = -1
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
        double body_timeout,
    ) noexcept:
        self.in_pool = False
        if self._connection is not connection:
            self._connection = connection
            self.app = app
            self._transport = transport
            self._date_box = date_box
            if self._compression is not compression:
                self._compression = compression
                self._apply_compression(compression)
        self._max_size = max_body_size
        self._timeout = body_timeout
        self.span = None
        self.route = EMPTY_ROUTE_MATCH
        self._state = None
        self._clear_request_headers()
        self.handler_done = False
        self.handler_started = False
        self._http2 = False
        self._h2_stream_id = 0
        self._h2_pending = b""
        self._h2_pending_off = 0
        self._h2_body_done = False
        self._h2_method = None
        self._h2_got_method = False
        self._h2_got_path = False
        self._h2_got_authority = False
        self._h2_dispatched = False
        self._h2_headers_done = False
        self._h2_headers_sent = False
        self._h2_headers_too_large = False
        self._h2_head_bytes = 0
        self._h2_awaiting_headers = False
        self._h2_header_deadline = 0.0

    cdef void bind_http2(self, int32_t stream_id) noexcept:
        self._http2 = True
        self._h2_stream_id = stream_id
        self._h2_got_method = False
        self._h2_got_path = False
        self._h2_got_authority = False
        self._h2_headers_too_large = False
        self._h2_head_bytes = 0
        self._h2_awaiting_headers = True
        self._h2_header_deadline = 0.0

    cdef void _clear_hot_request_headers(self) noexcept:
        self._req_encoding = ENCODING_NONE
        self._req_expect_continue = False
        self._req_connection_close = False
        self._req_content_length = -1
        self._req_accept_present = False
        self._req_br_q = -1
        self._req_gzip_q = -1
        self._req_wildcard_q = -1
        self._req_identity_q = -1

    cdef void cache_hot_request_headers(self) noexcept:
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

    def on_handler_done(self, task):
        """Log/abort on failure, then recycle after the handler task."""
        on_handler_done(self, self, task)
        self.handler_finished()

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
        cdef object scanned
        cdef object existing_ce
        cdef object existing_cl
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
        if self._http2:
            self._h2_respond(body, content_type, status, nbytes)
            return
        # Empty headers + no compression: writelines of interned pieces
        # (status, Date, type, length, body). No join, no cross-request cache.
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
            existing_ce = None
            existing_cl = None
            if not h.c_empty():
                scanned = h.c_scan_respond(content_type)
                existing_ce = scanned[0]
                existing_cl = scanned[1]
            if not _may_have_body(status):
                body = b""
            elif existing_ce is None:
                encoding = None
                if self._may_compress(body, content_type, False, nbytes):
                    encoding = _encoding_wire(self._req_encoding)
                if encoding is not None:
                    flat = self._body_as_bytes(body)
                    try:
                        self._frame(flat, encoding, &native_out, &native_len)
                        self._declared_length = <Py_ssize_t>native_len
                        self._bytes_written = 0
                        if existing_cl is not None:
                            h.c_require_respond_length(
                                existing_cl, _dec(native_len)
                            )
                        self._buf_bytes(_status_line(status))
                        self._buf_bytes(self._date_box[0])
                        if self._out_buf is None:
                            self._out_buf = bytearray(256)
                        h.c_write_respond_pairs(
                            self._out_buf,
                            &self._out_len,
                            True,
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
            if existing_cl is not None:
                h.c_require_respond_length(existing_cl, _dec(<size_t>nbytes))
            self._buf_bytes(_status_line(status))
            self._buf_bytes(self._date_box[0])
            if self._out_buf is None:
                self._out_buf = bytearray(256)
            h.c_write_respond_pairs(self._out_buf, &self._out_len, False)
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
        if self._http2:
            self._connection.h2_abort(self)
        else:
            self._transport.close()
        self._done()

    def write_headers(self, int status_code):
        cdef Headers headers = self.headers
        cdef object raw_length
        cdef object parsed_length
        cdef object encoding
        cdef list nva
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
        elif not self._http2:
            headers.c_set(b"transfer-encoding", b"chunked")
            if headers.c_get(b"content-encoding") is None:
                encoding = None
                if self._may_compress(
                    None, headers.c_get(b"content-type"), True, -1
                ):
                    encoding = _encoding_wire(self._req_encoding)
                if encoding is not None:
                    if encoding == b"br":
                        self._ensure_brotli()
                    else:
                        self._ensure_gzip()
                    headers.c_set(b"content-encoding", encoding)
                    headers.c_merge_vary(b"accept-encoding")
        if self._http2:
            nva = [
                (b":status", _dec(<size_t>status_code)),
                (b"date", self._h2_date_value()),
            ]
            self._connection.h2_write_headers(self, nva, False, False, False)
            self._status_code = status_code
            return self
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
        if self._http2:
            self._bytes_written += n
            if isinstance(data, (list, tuple)):
                for part in data:
                    if part:
                        self._connection.h2_write_data(self, part, False)
            else:
                self._connection.h2_write_data(self, data, False)
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
        if self._http2:
            self._connection.h2_end(self)
            self._completed = True
            self._done()
            return
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
        """Context: stop handler work (client gone or app draining)."""
        return self.disconnected or self.shutting_down

    def alive(self, source=None):
        return _Alive(self, source)

    cdef void reset_body(self, bint expect_continue, Py_ssize_t expected_size) noexcept:
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

    cdef void mark_nobody(self) noexcept:
        """GET/HEAD/empty POST: no upload, body() is b'' without Event waits."""
        self._clear_body_storage()
        self._cached = b""
        self._data_ready = None
        self._cancel_stall_timer()
        self._expect_continue = False
        self._total_read = 0
        self._read_max_size = -1
        self._consumed_as = CONSUMED_NONE
        self._abort_reason = ABORT_NONE
        self._body_active = False
        self._body_complete = True
        self._waiting = False
        self._discard_body = False
        self._expected_size = 0

    cdef void _clear_body_storage(self) noexcept:
        if self._chunks is not None:
            self._chunks.clear()
        self._body_tail = None
        self._tail_used = 0
        self._tail_cap = 0
        self._buffered = 0

    cdef int _ensure_body_tail(self, Py_ssize_t received_before) noexcept:
        cdef Py_ssize_t cap
        cdef Py_ssize_t remaining
        cdef object tail
        if self._body_tail is not None:
            return 0
        cap = self._stream_max_chunk
        # Buffered body() wants one Content-Length-sized object so complete
        # does not b"".join ~32x64KiB tails. Do not wait for CONSUMED_BODY:
        # the first body bytes often win that race (same llhttp_execute as
        # headers-complete, or a pipelined request still queued). stream()
        # sets CONSUMED_STREAM before feeding so it keeps stream-sized tails.
        if (
            self._consumed_as != CONSUMED_STREAM
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
        tail = PyBytes_FromStringAndSize(NULL, cap)
        if tail is None:
            PyErr_Clear()
            return -1
        self._body_tail = tail
        self._tail_used = 0
        self._tail_cap = cap
        return 0

    cdef int _adopt_expected_body_buffer(self) noexcept:
        """Compact already-fed stream-sized tails into one Content-Length buffer.

        Used when body() starts after some bytes already landed in stream-sized
        pieces. Later c_feed memcpy's into this tail; complete skips join.
        """
        cdef Py_ssize_t have
        cdef Py_ssize_t offset
        cdef object dest
        cdef object chunk
        cdef object chunks
        if self._expected_size <= 0 or self._consumed_as == CONSUMED_STREAM:
            return 0
        if self._body_complete:
            return 0
        have = self._buffered + self._tail_used
        if have > self._expected_size:
            return 0
        if (
            self._body_tail is not None
            and self._tail_cap == self._expected_size
            and (self._chunks is None or not self._chunks)
        ):
            return 0
        dest = PyBytes_FromStringAndSize(NULL, self._expected_size)
        if dest is None:
            PyErr_Clear()
            return -1
        offset = 0
        chunks = self._chunks
        if chunks is not None:
            for chunk in chunks:
                memcpy(
                    PyBytes_AS_STRING(dest) + offset,
                    PyBytes_AS_STRING(chunk),
                    <size_t>len(chunk),
                )
                offset += len(chunk)
            chunks.clear()
        if self._body_tail is not None and self._tail_used > 0:
            memcpy(
                PyBytes_AS_STRING(dest) + offset,
                PyBytes_AS_STRING(self._body_tail),
                <size_t>self._tail_used,
            )
            offset += self._tail_used
        self._body_tail = dest
        self._tail_used = offset
        self._tail_cap = self._expected_size
        self._buffered = 0
        return 0

    cdef int _seal_body_tail(self) noexcept:
        cdef object chunk
        cdef object chunks
        if self._body_tail is None or self._tail_used == 0:
            return 0
        if self._tail_used == self._tail_cap:
            chunk = self._body_tail
        else:
            chunk = PyBytes_FromStringAndSize(
                PyBytes_AS_STRING(self._body_tail),
                self._tail_used,
            )
            if chunk is None:
                PyErr_Clear()
                return -1
        chunks = self._chunks
        if chunks is None:
            chunks = []
            self._chunks = chunks
        chunks.append(chunk)
        self._buffered += self._tail_used
        self._body_tail = None
        self._tail_used = 0
        self._tail_cap = 0
        return 0

    cdef object _body_to_bytes(self):
        cdef object out
        if self._seal_body_tail() != 0:
            raise MemoryError()
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
            raise RequestBodyError(413, "Request body too large")
        if self._abort_reason == ABORT_TIMEOUT:
            raise RequestBodyError(
                408,
                "Request timeout: body upload too slow. "
                "This may indicate a slowloris attack or very poor connection.",
            )
        if self._abort_reason == ABORT_DISCONNECTED:
            raise ClientDisconnected()

    cdef void _wake(self):
        if self._data_ready is not None:
            self._data_ready.set()

    cdef void _cancel_stall_timer(self) noexcept:
        self._stall_deadline = 0.0
        self._stall_seen = self._stall_touch

    cdef void _reset_stall_timer(self) noexcept:
        """Arm/refresh slowloris stall timeout while a body consumer is waiting.

        Sweep mode only bumps a generation counter. The connection sweeper
        stores ``now + timeout`` once per wake — body chunks do not call
        ``loop.time()`` or ``call_later``.
        """
        if not self._waiting or self._body_complete or self._timeout <= 0:
            self._cancel_stall_timer()
            return
        if _TIMEOUT_MODE == _TIMEOUT_OFF:
            self._cancel_stall_timer()
            return
        self._stall_touch += 1

    cdef void fire_body_stall(self):
        self._stall_deadline = 0.0
        self._stall_seen = self._stall_touch
        if self._abort_reason != ABORT_NONE or self._body_complete:
            return
        self._abort_reason = ABORT_TIMEOUT
        self._clear_body_storage()
        if self._connection is not None:
            self._connection.set_body_paused(self, False)
        self._wake()

    cdef void _maybe_continue(self):
        if self._expect_continue:
            self._expect_continue = False
            if self._transport is None or self._transport.is_closing():
                return
            if self._http2:
                self._connection.h2_write_headers(self, [(b":status", b"100")], True, True, True, True)
                return
            self._transport.write(b"HTTP/1.1 100 Continue\r\n\r\n")

    cdef void _maybe_pause(self):
        if self._consumed_as == CONSUMED_STREAM:
            if self._buffered + self._tail_used > HIGH_WATER:
                self._connection.set_body_paused(self, True)
            return
        if (
            self._consumed_as == CONSUMED_BODY
            and self._buffered + self._tail_used > BODY_HIGH_WATER
        ):
            self._connection.set_body_paused(self, True)

    cdef int c_feed(self, const char* at, size_t length) noexcept:
        cdef Py_ssize_t new_total
        cdef Py_ssize_t offset
        cdef Py_ssize_t available
        cdef Py_ssize_t take
        cdef bint emitted
        if not self._body_active:
            self.reset_body(False, -1)
        new_total = self._total_read + <Py_ssize_t>length
        # Discard leftover DATA on HTTP/2, or a bounded HTTP/1 body after a
        # keep-alive 413, without closing the socket. Unknown-length HTTP/1
        # overflow after the handler finished still closes (read-DoS).
        if self._discard_body:
            if (
                self._http2
                or (
                    self._expected_size >= 0
                    and self._expected_size <= SMALL_BODY_DRAIN
                )
            ):
                self._total_read = new_total
                return 0
        if new_total > self._max_size or (
            self._read_max_size >= 0 and new_total > self._read_max_size
        ):
            self._abort_reason = ABORT_TOO_LARGE
            self._cancel_stall_timer()
            self._clear_body_storage()
            self._connection.set_body_paused(self, False)
            self._wake()
            if (
                not self._http2
                and self._discard_body
                and self._transport is not None
                and not self._transport.is_closing()
            ):
                self._transport.close()
            return 0
        if self._discard_body:
            self._total_read = new_total
            return 0
        emitted = False
        offset = 0
        while offset < <Py_ssize_t>length:
            if self._ensure_body_tail(self._total_read + offset) != 0:
                return -1
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
                if self._seal_body_tail() != 0:
                    return -1
                emitted = True
        self._total_read = new_total
        if self._consumed_as == CONSUMED_STREAM:
            if emitted:
                self._wake()
            if self._waiting:
                self._reset_stall_timer()
            self._maybe_pause()
            return 0
        # body(): refresh stall on progress; wake on complete/abort, and also
        # when over the high-water mark so the waiter can unpause the transport.
        # Without that, _pump_data keeps ingesting parser quantums and wrk's
        # 32 connections dump full 2MB bodies without yielding.
        if self._waiting:
            self._reset_stall_timer()
        self._maybe_pause()
        if (
            self._consumed_as == CONSUMED_BODY
            and self._waiting
            and self._buffered + self._tail_used > BODY_HIGH_WATER
        ):
            self._wake()
        return 0

    cdef int c_complete(self) noexcept:
        if not self._body_active:
            return 0
        self._body_complete = True
        self._cancel_stall_timer()
        if self._connection is not None:
            self._connection.set_body_paused(self, False)
        if self._discard_body:
            self._clear_body_storage()
            self._maybe_recycle()
            return 0
        if self._seal_body_tail() != 0:
            return -1
        if self._consumed_as == CONSUMED_STREAM:
            self._wake()
            self._maybe_recycle()
            return 0
        if self._abort_reason != ABORT_NONE:
            self._clear_body_storage()
            self._wake()
            self._maybe_recycle()
            return 0
        if self._consumed_as != CONSUMED_STREAM and self._cached is None:
            try:
                self._cached = self._body_to_bytes()
            except Exception:
                PyErr_Clear()
                return -1
        self._wake()
        self._maybe_recycle()
        return 0

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
            if self._expected_size > 0:
                chunk_size = self._expected_size
                if chunk_size > STREAM_CHUNK_CL:
                    chunk_size = STREAM_CHUNK_CL
            else:
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
        if (
            self._body_tail is not None
            and self._tail_cap != chunk_size
            and self._seal_body_tail() != 0
        ):
            raise MemoryError()
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
                raise RequestBodyError(413, "Request body too large")
            self._consumed_as = CONSUMED_BODY
            return self._cached
        self._consumed_as = CONSUMED_BODY
        self._read_max_size = -1 if max_size is None else <Py_ssize_t>max_size
        if self._adopt_expected_body_buffer() != 0:
            raise MemoryError()
        self._maybe_continue()
        while not self._body_complete:
            if self._abort_reason != ABORT_NONE:
                self._raise_abort()
            if (
                self._read_max_size >= 0
                and self._total_read > self._read_max_size
            ):
                raise RequestBodyError(413, "Request body too large")
            if self._connection is not None:
                self._connection.set_body_paused(self, False)
            if self._body_complete:
                break
            await self._wait_for_body_data()
        if self._connection is not None:
            self._connection.set_body_paused(self, False)
        if self._abort_reason != ABORT_NONE:
            self._raise_abort()
        if self._cached is None:
            self._cached = self._body_to_bytes()
        if max_size is not None and len(self._cached) > max_size:
            raise RequestBodyError(413, "Request body too large")
        return self._cached


cdef class RequestHeaders(Headers):
    """Read-only request headers backed by the owning exchange arena."""

    def __init__(self, RequestExchange owner):
        Headers.__init__(self)
        self._owner = owner

    cdef object c_get(self, object name):
        cdef bytes key = name
        return self.c_get_n(<const char*>key, <Py_ssize_t>len(key))

    cdef object c_request_indexed(self, Py_ssize_t index):
        cdef RequestExchange owner
        cdef RawHeader* header
        if index < 0:
            return None
        owner = <RequestExchange>self._owner
        header = &owner._req_raw_headers[index]
        return PyBytes_FromStringAndSize(
            owner._req_arena + header.value_offset,
            header.value_length,
        )

    cdef object c_value_str(self, Py_ssize_t index):
        cdef RequestExchange owner
        cdef RawHeader* header
        if index < 0:
            return None
        owner = <RequestExchange>self._owner
        header = &owner._req_raw_headers[index]
        return _decode_latin1(
            owner._req_arena + header.value_offset,
            <Py_ssize_t>header.value_length,
        )

    cdef Py_ssize_t c_find_n(self, const char* query, Py_ssize_t query_length) noexcept:
        cdef RequestExchange owner
        cdef RawHeader* header
        cdef Py_ssize_t index
        owner = <RequestExchange>self._owner
        if query_length == 4 and memcmp(query, "host", 4) == 0:
            return owner._req_host_index
        if query_length == 6 and memcmp(query, "cookie", 6) == 0:
            return owner._req_cookie_index
        if query_length == 13 and memcmp(query, "authorization", 13) == 0:
            return owner._req_authorization_index
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
                return index
        return -1

    cdef object c_get_n(self, const char* query, Py_ssize_t query_length):
        return self.c_request_indexed(self.c_find_n(query, query_length))

    cdef object c_getlist_n(self, const char* query, Py_ssize_t query_length):
        cdef RequestExchange owner
        cdef RawHeader* header
        cdef Py_ssize_t index
        cdef Py_ssize_t start = 0
        cdef list result
        owner = <RequestExchange>self._owner
        if query_length == 4 and memcmp(query, "host", 4) == 0:
            start = owner._req_host_index
        elif query_length == 6 and memcmp(query, "cookie", 6) == 0:
            start = owner._req_cookie_index
        elif query_length == 13 and memcmp(query, "authorization", 13) == 0:
            start = owner._req_authorization_index
        if start < 0:
            return []
        result = []
        for index in range(start, owner._req_raw_count):
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

    cdef void c_set(self, object name, object value):
        _raise_readonly_request_headers()

    cdef void c_add(self, object name, object value):
        _raise_readonly_request_headers()

    cdef void c_remove(self, object name):
        _raise_readonly_request_headers()

    cdef void c_clear(self):
        _raise_readonly_request_headers()

    cdef void c_parse_cookies(self, dict out) except *:
        cdef RequestExchange owner = <RequestExchange>self._owner
        cdef RawHeader* header
        cdef Py_ssize_t index
        cdef Py_ssize_t start = owner._req_cookie_index
        if start < 0:
            return
        for index in range(start, owner._req_raw_count):
            header = &owner._req_raw_headers[index]
            if header.name_length == 6 and memcmp(
                owner._req_arena + header.name_offset,
                "cookie",
                6,
            ) == 0:
                _parse_cookie_line(
                    owner._req_arena + header.value_offset,
                    <Py_ssize_t>header.value_length,
                    out,
                )

    def get(self, str name, default=None):
        cdef char buf[HEADER_NAME_STACK]
        cdef Py_ssize_t n
        cdef object value
        _fold_header_name(name, buf, &n)
        value = self.c_value_str(self.c_find_n(buf, n))
        if value is None:
            return default
        return value

    def unsafe_get(self, name, default=None):
        cdef object value = self.c_get(name)
        if value is None:
            return default
        return value

    def getlist(self, str name):
        cdef char buf[HEADER_NAME_STACK]
        cdef Py_ssize_t n
        cdef RequestExchange owner
        cdef RawHeader* header
        cdef Py_ssize_t index
        cdef Py_ssize_t start
        cdef list result
        _fold_header_name(name, buf, &n)
        start = self.c_find_n(buf, n)
        if start < 0:
            return []
        owner = <RequestExchange>self._owner
        result = []
        for index in range(start, owner._req_raw_count):
            header = &owner._req_raw_headers[index]
            if (
                header.name_length == <uint32_t>n
                and memcmp(
                    owner._req_arena + header.name_offset,
                    buf,
                    <size_t>n,
                ) == 0
            ):
                result.append(
                    _decode_latin1(
                        owner._req_arena + header.value_offset,
                        <Py_ssize_t>header.value_length,
                    )
                )
        return result

    def unsafe_getlist(self, name):
        cdef bytes key = name
        return self.c_getlist_n(<const char*>key, <Py_ssize_t>len(key))

    def items(self):
        return [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in self.unsafe_items()
        ]

    def unsafe_items(self):
        cdef RequestExchange owner = <RequestExchange>self._owner
        cdef RawHeader* header
        cdef Py_ssize_t index
        cdef list result = []
        for index in range(owner._req_raw_count):
            header = &owner._req_raw_headers[index]
            result.append((
                PyBytes_FromStringAndSize(
                    owner._req_arena + header.name_offset,
                    header.name_length,
                ),
                PyBytes_FromStringAndSize(
                    owner._req_arena + header.value_offset,
                    header.value_length,
                ),
            ))
        return result

    def __contains__(self, name):
        cdef char buf[HEADER_NAME_STACK]
        cdef Py_ssize_t n
        try:
            _fold_header_name(name, buf, &n)
        except ValueError:
            return False
        return self.c_find_n(buf, n) >= 0

    def __len__(self):
        cdef RequestExchange owner = <RequestExchange>self._owner
        cdef RawHeader* header
        cdef Py_ssize_t index
        cdef set seen = set()
        for index in range(owner._req_raw_count):
            header = &owner._req_raw_headers[index]
            seen.add(
                _intern_name(
                    owner._req_arena + header.name_offset,
                    header.name_length,
                )
            )
        return len(seen)

    def __bool__(self):
        return (<RequestExchange>self._owner)._req_raw_count != 0

    def __repr__(self):
        return f"RequestHeaders({self.items()!r})"

    @property
    def materialized(self):
        """Always false: request headers stay an arena scan (never copied to a dict)."""
        return False


cdef RequestExchange acquire_exchange(
    object connection,
    object app,
    object transport,
    list date_box,
    object compression,
    int max_body_size,
    double body_timeout,
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
        body_timeout,
    )
    return exchange
