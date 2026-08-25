# cython: language_level=3
"""Bytes-backed headers. Parser lowercases and interns names in C."""

from libc.string cimport memcmp, memcpy
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
    NAME_STACK = 256

cdef const char* _INTERN_C[INTERN_MAX]
cdef Py_ssize_t _INTERN_N[INTERN_MAX]
cdef list _INTERN_PY = []
cdef int _INTERN_COUNT = 0


cdef void _intern_add(const char* s):
    global _INTERN_COUNT
    cdef Py_ssize_t n
    cdef int i = _INTERN_COUNT
    if i >= INTERN_MAX:
        return
    n = 0
    while s[n] != 0:
        n += 1
    _INTERN_C[i] = s
    _INTERN_N[i] = n
    _INTERN_PY.append(PyBytes_FromStringAndSize(s, n))
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

cdef object _CONNECTION_NAME = _INTERN_PY[1]
cdef object _ACCEPT_ENCODING_NAME = _INTERN_PY[13]
cdef object _EXPECT_NAME = _INTERN_PY[26]


cdef inline void _lower_copy(char* dst, const char* src, size_t n) noexcept:
    cdef size_t i
    cdef unsigned char c
    for i in range(n):
        c = <unsigned char>src[i]
        if 65 <= c <= 90:
            dst[i] = <char>(c + 32)
        else:
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


cdef object _intern_name(const char* src, size_t n):
    cdef char buf[NAME_STACK]
    cdef const char* p
    cdef int i
    if n >= NAME_STACK:
        raise ValueError("Invalid header name: too long")
    _lower_copy(buf, src, n)
    p = buf
    for i in range(_INTERN_COUNT):
        if _INTERN_N[i] == <Py_ssize_t>n and memcmp(_INTERN_C[i], p, n) == 0:
            return _INTERN_PY[i]
    return PyBytes_FromStringAndSize(p, <Py_ssize_t>n)


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
    def __init__(self, raw_header_data=None):
        self._data = raw_header_data if raw_header_data is not None else {}
        self._request_accept_encoding = None
        self._request_connection_close = False
        self._request_expect_continue = False

    cdef void add_raw(self, const char* name, size_t nlen, const char* value, size_t vlen):
        cdef object key = _intern_name(name, nlen)
        cdef object val = PyBytes_FromStringAndSize(value, <Py_ssize_t>vlen)
        if key is _CONNECTION_NAME:
            if _contains_token(value, vlen, "close", 5):
                self._request_connection_close = True
        elif key is _EXPECT_NAME:
            if _contains_token(value, vlen, "100-continue", 12):
                self._request_expect_continue = True
        elif key is _ACCEPT_ENCODING_NAME and self._request_accept_encoding is None:
            self._request_accept_encoding = val
        self.c_add(key, val)

    cdef object c_request_accept_encoding(self):
        return self._request_accept_encoding

    cdef bint c_request_connection_close(self) noexcept:
        return self._request_connection_close

    cdef bint c_request_expect_continue(self) noexcept:
        return self._request_expect_continue

    cdef object c_get(self, object name):
        cdef object value = self._data.get(name)
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
        self._request_accept_encoding = None
        self._request_connection_close = False
        self._request_expect_continue = False

    cdef bint c_empty(self):
        return not self._data

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

    cdef int c_write_wire_ba(self, object buf, Py_ssize_t* length) except -1:
        cdef object name
        cdef object value
        cdef object header_value
        cdef const char* p
        cdef Py_ssize_t n
        for name, value in self._data.items():
            if type(value) is bytes:
                p = name
                self._add_ba(buf, length, p, <Py_ssize_t>len(name))
                self._add_ba(buf, length, <const char*>b": ", 2)
                p = value
                self._add_ba(buf, length, p, <Py_ssize_t>len(value))
                self._add_ba(buf, length, <const char*>b"\r\n", 2)
                continue
            for header_value in value:
                p = name
                self._add_ba(buf, length, p, <Py_ssize_t>len(name))
                self._add_ba(buf, length, <const char*>b": ", 2)
                p = header_value
                self._add_ba(buf, length, p, <Py_ssize_t>len(header_value))
                self._add_ba(buf, length, <const char*>b"\r\n", 2)
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
        cdef object value = self._data.get(name)
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
        for name, value in self._data.items():
            if type(value) is list:
                for v in value:
                    result.append((name, v))
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
