# cython: language_level=3
"""Dict-backed response headers and the shared application-facing API.

Request wire storage belongs to ``RequestExchange``. Its ``RequestHeaders``
subclass overrides the lazy read/materialization hooks while this base remains
the compact mutable representation used for responses.
"""

from libc.stdint cimport uint8_t, uint32_t
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
    def __init__(self, raw_header_data=None):
        self._data = raw_header_data if raw_header_data is not None else {}

    cdef void _materialize(self):
        """Overridden by exchange-backed request headers."""

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
        self._materialize()
        self._data.clear()

    cdef bint c_empty(self):
        self._materialize()
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
        self._materialize()
        for name, value in self._data.items():
            if skip_mode and (
                name == b"content-type" or name == b"content-length"
            ):
                continue
            if skip_mode == 2 and (
                name == b"content-encoding" or name == b"vary"
            ):
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
