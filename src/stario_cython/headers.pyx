# cython: language_level=3
"""Bytes-backed headers. Parser lowercases and interns names in C."""

from libc.string cimport memcmp, memcpy
from libc.stdlib cimport free, realloc
from libc.stdint cimport uint8_t, uint32_t
from cpython.bytearray cimport (
    PyByteArray_AS_STRING,
    PyByteArray_GET_SIZE,
    PyByteArray_Resize,
)
from cpython.bytes cimport PyBytes_FromStringAndSize

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
    RAW_ARENA_RETAIN_MAX = 8 * 1024
    RAW_HEADERS_RETAIN_MAX = 64

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


cdef void _intern_add(const char* s):
    global _INTERN_COUNT
    cdef Py_ssize_t n
    cdef int i = _INTERN_COUNT
    cdef uint32_t slot
    if i >= INTERN_MAX:
        return
    n = 0
    while s[n] != 0:
        n += 1
    _INTERN_C[i] = s
    _INTERN_N[i] = n
    _INTERN_PY.append(PyBytes_FromStringAndSize(s, n))
    slot = _hash_bytes(s, <size_t>n) & (INTERN_TABLE_SIZE - 1)
    while _INTERN_SLOT[slot] != 0:
        slot = (slot + 1) & (INTERN_TABLE_SIZE - 1)
    _INTERN_SLOT[slot] = <uint8_t>(i + 1)
    _INTERN_COUNT = i + 1


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
    cdef uint8_t c
    for i in range(n):
        c = <uint8_t>src[i]
        if 65 <= c <= 90:
            c += 32
        dst[i] = <char>c


cdef inline bint _token_equals(
    const char* value,
    size_t start,
    size_t end,
    const char* token,
    size_t token_len,
) noexcept:
    cdef size_t i
    cdef unsigned char c
    if end - start != token_len:
        return False
    for i in range(token_len):
        c = <unsigned char>value[start + i]
        if 65 <= c <= 90:
            c += 32
        if c != <unsigned char>token[i]:
            return False
    return True


cdef bint _contains_token(
    const char* value,
    size_t length,
    const char* token,
    size_t token_len,
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
        if _token_equals(value, start, end, token, token_len):
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
    cdef unsigned char c
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
        c = <unsigned char>value[start]
        if c < 48 or c > 57:
            return 0
        q = q * 10 + c - 48
        digits += 1
        start += 1
    if start != end:
        return 0
    while digits < 3:
        q *= 10
        digits += 1
    return q


cdef object _intern_name(const char* src, size_t n):
    cdef char buf[NAME_STACK]
    cdef const char* p
    cdef uint32_t slot
    cdef uint8_t entry
    cdef int i
    if n >= NAME_STACK:
        raise ValueError("Invalid header name: too long")
    _lower_copy(buf, src, n)
    p = buf
    if n == 4 and memcmp(p, "host", 4) == 0:
        return _INTERN_PY[0]
    if n == 10 and memcmp(p, "connection", 10) == 0:
        return _INTERN_PY[1]
    if n == 15 and memcmp(p, "accept-encoding", 15) == 0:
        return _INTERN_PY[13]
    if n == 6 and memcmp(p, "expect", 6) == 0:
        return _INTERN_PY[27]
    slot = _hash_bytes(p, n) & (INTERN_TABLE_SIZE - 1)
    while True:
        entry = _INTERN_SLOT[slot]
        if entry == 0:
            return PyBytes_FromStringAndSize(p, <Py_ssize_t>n)
        i = <int>entry - 1
        if _INTERN_N[i] == <Py_ssize_t>n and memcmp(_INTERN_C[i], p, n) == 0:
            return _INTERN_PY[i]
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
    def __cinit__(self):
        self._raw_arena = NULL
        self._raw_len = 0
        self._raw_cap = 0
        self._raw_headers = NULL
        self._raw_count = 0
        self._raw_headers_cap = 0
        self._pending_header = False
        self._request_host_index = -1

    def __init__(self, raw_header_data=None):
        self._data = raw_header_data if raw_header_data is not None else {}
        self._materialized = raw_header_data is not None
        self._request_connection_close = False
        self._request_expect_continue = False
        self._request_accept_present = False
        self._request_br_q = -1
        self._request_gzip_q = -1
        self._request_wildcard_q = -1
        self._request_identity_q = -1

    def __dealloc__(self):
        if self._raw_arena != NULL:
            free(self._raw_arena)
        if self._raw_headers != NULL:
            free(self._raw_headers)

    cdef int _reserve_raw(self, Py_ssize_t bytes_needed) except -1:
        cdef Py_ssize_t need = self._raw_len + bytes_needed
        cdef Py_ssize_t cap
        cdef char* arena
        if need <= self._raw_cap:
            return 0
        cap = 256 if self._raw_cap == 0 else self._raw_cap * 2
        if cap < need:
            cap = need
        arena = <char*>realloc(self._raw_arena, <size_t>cap)
        if arena == NULL:
            raise MemoryError()
        self._raw_arena = arena
        self._raw_cap = cap
        return 0

    cdef int _reserve_raw_headers(self) except -1:
        cdef Py_ssize_t cap
        cdef RawHeader* headers
        if self._raw_count < self._raw_headers_cap:
            return 0
        cap = 16 if self._raw_headers_cap == 0 else self._raw_headers_cap * 2
        headers = <RawHeader*>realloc(
            self._raw_headers,
            <size_t>cap * sizeof(RawHeader),
        )
        if headers == NULL:
            raise MemoryError()
        self._raw_headers = headers
        self._raw_headers_cap = cap
        return 0

    cdef void start_raw_header(self):
        if self._pending_header:
            self.finish_raw_header()
        self._pending_header = True
        self._pending_name_offset = self._raw_len
        self._pending_name_length = 0
        self._pending_value_offset = -1
        self._pending_value_length = 0

    cdef void append_raw_name(self, const char* data, size_t length):
        if not self._pending_header:
            self.start_raw_header()
        if self._pending_value_offset >= 0:
            raise ValueError("Invalid fragmented header field")
        if self._pending_name_length + <Py_ssize_t>length >= NAME_STACK:
            raise ValueError("Invalid header name: too long")
        self._reserve_raw(<Py_ssize_t>length)
        _lower_copy(self._raw_arena + self._raw_len, data, length)
        self._raw_len += <Py_ssize_t>length
        self._pending_name_length += <Py_ssize_t>length

    cdef void append_raw_value(self, const char* data, size_t length):
        if not self._pending_header:
            raise ValueError("Invalid header value without field")
        if self._pending_value_offset < 0:
            self._pending_value_offset = self._raw_len
        self._reserve_raw(<Py_ssize_t>length)
        if length:
            memcpy(self._raw_arena + self._raw_len, data, length)
        self._raw_len += <Py_ssize_t>length
        self._pending_value_length += <Py_ssize_t>length

    cdef void finish_raw_header(self):
        cdef RawHeader* header
        cdef const char* name
        cdef const char* value
        cdef size_t nlen
        cdef size_t vlen
        cdef object key
        cdef object materialized_value
        if not self._pending_header:
            return
        if self._pending_name_length == 0:
            raise ValueError("Invalid header name: empty")
        if self._pending_value_offset < 0:
            self._pending_value_offset = self._raw_len
        name = self._raw_arena + self._pending_name_offset
        value = self._raw_arena + self._pending_value_offset
        nlen = <size_t>self._pending_name_length
        vlen = <size_t>self._pending_value_length
        self._reserve_raw_headers()
        header = &self._raw_headers[self._raw_count]
        header.name_offset = <uint32_t>self._pending_name_offset
        header.name_length = <uint32_t>nlen
        header.value_offset = <uint32_t>self._pending_value_offset
        header.value_length = <uint32_t>vlen
        if _token_equals(name, 0, nlen, "host", 4):
            if self._request_host_index < 0:
                self._request_host_index = self._raw_count
        elif _token_equals(name, 0, nlen, "connection", 10):
            if _contains_token(value, vlen, "close", 5):
                self._request_connection_close = True
        elif _token_equals(name, 0, nlen, "expect", 6):
            if _contains_token(value, vlen, "100-continue", 12):
                self._request_expect_continue = True
        elif _token_equals(name, 0, nlen, "accept-encoding", 15):
            self._scan_request_accept_encoding(value, vlen)
        if self._materialized:
            key = _intern_name(name, nlen)
            materialized_value = PyBytes_FromStringAndSize(value, vlen)
            self.c_add(key, materialized_value)
        self._raw_count += 1
        self._pending_header = False

    cdef void _materialize(self):
        cdef Py_ssize_t i
        cdef RawHeader* header
        cdef object key
        cdef object value
        if self._materialized:
            return
        self._materialized = True
        for i in range(self._raw_count):
            header = &self._raw_headers[i]
            key = _intern_name(
                self._raw_arena + header.name_offset,
                header.name_length,
            )
            value = PyBytes_FromStringAndSize(
                self._raw_arena + header.value_offset,
                header.value_length,
            )
            self.c_add(key, value)

    cdef object c_request_host(self):
        cdef RawHeader* header
        if self._materialized:
            return self.c_get(b"host")
        if self._request_host_index < 0:
            return None
        header = &self._raw_headers[self._request_host_index]
        return PyBytes_FromStringAndSize(
            self._raw_arena + header.value_offset,
            header.value_length,
        )

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
        if not self._request_accept_present:
            self._request_accept_present = True
            self._request_br_q = -1
            self._request_gzip_q = -1
            self._request_wildcard_q = -1
            self._request_identity_q = -1
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
                while param_end < segment_end and value[param_end] != <char>59:
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
                self._request_br_q = q
            elif _token_equals(value, start, token_end, "gzip", 4):
                self._request_gzip_q = q
            elif _token_equals(value, start, token_end, "*", 1):
                self._request_wildcard_q = q
            elif _token_equals(value, start, token_end, "identity", 8):
                self._request_identity_q = q
            start = segment_end + 1

    cdef bint c_request_connection_close(self) noexcept:
        return self._request_connection_close

    cdef bint c_request_expect_continue(self) noexcept:
        return self._request_expect_continue

    cdef int c_select_request_encoding(
        self,
        bint brotli_enabled,
        bint gzip_enabled,
    ) noexcept:
        cdef int wildcard
        cdef int br_q
        cdef int gzip_q
        cdef int best_q = 0
        cdef int selected = 0
        if not self._request_accept_present:
            return 0
        wildcard = (
            self._request_wildcard_q
            if self._request_wildcard_q >= 0
            else 0
        )
        br_q = self._request_br_q if self._request_br_q >= 0 else wildcard
        gzip_q = (
            self._request_gzip_q
            if self._request_gzip_q >= 0
            else wildcard
        )
        if brotli_enabled and br_q > best_q:
            best_q = br_q
            selected = 1
        if gzip_enabled and gzip_q > best_q:
            best_q = gzip_q
            selected = 2
        if self._request_identity_q >= best_q:
            return 0
        return selected

    cdef object c_get(self, object name):
        cdef object value
        self._materialize()
        value = self._data.get(name)
        if value is None:
            return None
        if type(value) is bytes:
            return value
        return value[0]

    cdef void c_set(self, object name, object value):
        self._materialize()
        self._data[name] = value

    cdef void c_add(self, object name, object value):
        cdef object existing
        self._materialize()
        if name not in self._data:
            self._data[name] = value
            return
        existing = self._data[name]
        if type(existing) is list:
            existing.append(value)
        else:
            self._data[name] = [existing, value]

    cdef void c_remove(self, object name):
        self._materialize()
        self._data.pop(name, None)

    cdef void c_clear(self):
        self._data.clear()
        if self._raw_arena != NULL and self._raw_cap > RAW_ARENA_RETAIN_MAX:
            free(self._raw_arena)
            self._raw_arena = NULL
            self._raw_cap = 0
        if (
            self._raw_headers != NULL
            and self._raw_headers_cap > RAW_HEADERS_RETAIN_MAX
        ):
            free(self._raw_headers)
            self._raw_headers = NULL
            self._raw_headers_cap = 0
        self._raw_len = 0
        self._raw_count = 0
        self._pending_header = False
        self._request_host_index = -1
        self._materialized = False
        self._request_connection_close = False
        self._request_expect_continue = False
        self._request_accept_present = False

    cdef bint c_empty(self):
        return self._raw_count == 0 and not self._data

    cdef int _add_ba(self, object buf, Py_ssize_t* length, const char* src, Py_ssize_t n) except -1:
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

    cdef int _write_pair(self, object buf, Py_ssize_t* length, object name, object value) except -1:
        cdef const char* p = <const char*>name
        self._add_ba(buf, length, p, <Py_ssize_t>len(name))
        self._add_ba(buf, length, <const char*>b": ", 2)
        p = <const char*>value
        self._add_ba(buf, length, p, <Py_ssize_t>len(value))
        return self._add_ba(buf, length, <const char*>b"\r\n", 2)

    cdef int c_write_wire_ba(self, object buf, Py_ssize_t* length) except -1:
        cdef object name
        cdef object value
        cdef object header_value
        self._materialize()
        for name, value in self._data.items():
            if type(value) is bytes:
                self._write_pair(buf, length, name, value)
                continue
            for header_value in value:
                self._write_pair(buf, length, name, header_value)
        return 0

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
        cdef object val
        if existing is not None:
            return existing.decode("latin-1")
        val = _encode_value(value)
        self.c_set(key, val)
        return val.decode("latin-1")

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
        return [v.decode("latin-1") for v in self.unsafe_getlist(_encode_name(name))]

    def unsafe_getlist(self, name):
        cdef object value
        self._materialize()
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
        cdef object v
        self._materialize()
        for name, value in self._data.items():
            if type(value) is list:
                for v in value:
                    result.append((name, v))
            else:
                result.append((name, value))
        return result

    def __contains__(self, name):
        try:
            self._materialize()
            return _encode_name(name) in self._data
        except ValueError:
            return False

    def __len__(self):
        self._materialize()
        return len(self._data)

    def __repr__(self):
        self._materialize()
        return f"Headers({self._data!r})"
