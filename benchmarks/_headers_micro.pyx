# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: initializedcheck=False
"""Cython backends for the request-header representation microbenchmark.

This intentionally models pooled request state: the arena, entry table, and
dict are retained between iterations while logical contents are reset.
"""

from libc.stddef cimport size_t
from libc.stdint cimport uint32_t, uint8_t
from libc.stdlib cimport free, realloc
from libc.string cimport memcmp, memcpy
from cpython.bytes cimport PyBytes_FromStringAndSize


cdef enum:
    EAGER_DICT = 0
    LAZY_DICT = 1
    ARENA_SCAN = 2
    ADAPTIVE = 3
    INTERN_TABLE_SIZE = 64
    INTERN_MAX = 32


cdef const char* _INTERN_C[INTERN_MAX]
cdef Py_ssize_t _INTERN_N[INTERN_MAX]
cdef uint32_t _INTERN_HASH[INTERN_MAX]
cdef uint8_t _INTERN_SLOT[INTERN_TABLE_SIZE]
cdef list _INTERN_PY = []
cdef int _INTERN_COUNT = 0


ctypedef struct RawHeader:
    uint32_t name_offset
    uint32_t name_length
    uint32_t value_offset
    uint32_t value_length
    uint32_t name_hash


cdef inline uint32_t _hash_bytes(const char* src, Py_ssize_t length) noexcept:
    cdef uint32_t value = <uint32_t>2166136261
    cdef Py_ssize_t i
    for i in range(length):
        value = (value ^ <uint8_t>src[i]) * <uint32_t>16777619
    return value


cdef void _intern_add(const char* src):
    global _INTERN_COUNT
    cdef int index = _INTERN_COUNT
    cdef Py_ssize_t length = 0
    cdef uint32_t name_hash
    cdef uint32_t slot
    if index >= INTERN_MAX:
        return
    while src[length] != 0:
        length += 1
    name_hash = _hash_bytes(src, length)
    _INTERN_C[index] = src
    _INTERN_N[index] = length
    _INTERN_HASH[index] = name_hash
    _INTERN_PY.append(PyBytes_FromStringAndSize(src, length))
    slot = name_hash & (INTERN_TABLE_SIZE - 1)
    while _INTERN_SLOT[slot] != 0:
        slot = (slot + 1) & (INTERN_TABLE_SIZE - 1)
    _INTERN_SLOT[slot] = <uint8_t>(index + 1)
    _INTERN_COUNT = index + 1


cdef void _init_intern() noexcept:
    if _INTERN_COUNT:
        return
    _intern_add("host")
    _intern_add("user-agent")
    _intern_add("accept")
    _intern_add("cookie")
    _intern_add("authorization")
    _intern_add("x-request-id")
    _intern_add("accept-encoding")
    _intern_add("connection")
    _intern_add("accept-language")
    _intern_add("cache-control")
    _intern_add("sec-fetch-site")
    _intern_add("sec-fetch-mode")
    _intern_add("sec-fetch-dest")
    _intern_add("origin")
    _intern_add("referer")
    _intern_add("content-type")
    _intern_add("x-forwarded-for")
    _intern_add("x-forwarded-proto")
    _intern_add("x-real-ip")
    _intern_add("traceparent")
    _intern_add("priority")
    _intern_add("dnt")
    _intern_add("sec-gpc")
    _intern_add("te")


cdef object _intern_name(
    const char* src,
    Py_ssize_t length,
    uint32_t name_hash,
):
    cdef uint32_t slot = name_hash & (INTERN_TABLE_SIZE - 1)
    cdef uint8_t entry
    cdef int index
    while True:
        entry = _INTERN_SLOT[slot]
        if entry == 0:
            return PyBytes_FromStringAndSize(src, length)
        index = <int>entry - 1
        if (
            _INTERN_HASH[index] == name_hash
            and _INTERN_N[index] == length
            and memcmp(_INTERN_C[index], src, <size_t>length) == 0
        ):
            return _INTERN_PY[index]
        slot = (slot + 1) & (INTERN_TABLE_SIZE - 1)


_init_intern()


cdef inline uint32_t _lower_copy_hash(
    char* dst,
    const char* src,
    Py_ssize_t length,
) noexcept:
    cdef uint32_t value = <uint32_t>2166136261
    cdef Py_ssize_t i
    cdef uint8_t ch
    for i in range(length):
        ch = <uint8_t>src[i]
        if 65 <= ch <= 90:
            ch += 32
        dst[i] = <char>ch
        value = (value ^ ch) * <uint32_t>16777619
    return value


cdef inline void _lower_copy(
    char* dst,
    const char* src,
    Py_ssize_t length,
) noexcept:
    cdef Py_ssize_t i
    cdef uint8_t ch
    for i in range(length):
        ch = <uint8_t>src[i]
        if 65 <= ch <= 90:
            ch += 32
        dst[i] = <char>ch


cdef class HeaderStore:
    """One pooled request-header store with a selectable lookup strategy."""

    cdef char* _arena
    cdef Py_ssize_t _arena_len
    cdef Py_ssize_t _arena_cap
    cdef RawHeader* _headers
    cdef Py_ssize_t _count
    cdef Py_ssize_t _headers_cap
    cdef dict _data
    cdef bint _materialized
    cdef int _mode
    cdef int _accesses
    cdef int _promote_after
    cdef public unsigned long materializations

    def __cinit__(self):
        self._arena = NULL
        self._headers = NULL

    def __init__(self, int mode, int promote_after=3):
        if mode < EAGER_DICT or mode > ADAPTIVE:
            raise ValueError("unknown header strategy")
        if promote_after < 1:
            raise ValueError("promote_after must be positive")
        self._arena_len = 0
        self._arena_cap = 0
        self._count = 0
        self._headers_cap = 0
        self._data = {}
        self._materialized = False
        self._mode = mode
        self._accesses = 0
        self._promote_after = promote_after
        self.materializations = 0

    def __dealloc__(self):
        if self._arena != NULL:
            free(self._arena)
        if self._headers != NULL:
            free(self._headers)

    cdef int _reserve_arena(self, Py_ssize_t needed) except -1:
        cdef Py_ssize_t cap
        cdef char* arena
        if needed <= self._arena_cap:
            return 0
        cap = 256 if self._arena_cap == 0 else self._arena_cap * 2
        while cap < needed:
            cap *= 2
        arena = <char*>realloc(self._arena, <size_t>cap)
        if arena == NULL:
            raise MemoryError()
        self._arena = arena
        self._arena_cap = cap
        return 0

    cdef int _reserve_headers(self, Py_ssize_t needed) except -1:
        cdef Py_ssize_t cap
        cdef RawHeader* headers
        if needed <= self._headers_cap:
            return 0
        cap = 16 if self._headers_cap == 0 else self._headers_cap * 2
        while cap < needed:
            cap *= 2
        headers = <RawHeader*>realloc(
            self._headers,
            <size_t>cap * sizeof(RawHeader),
        )
        if headers == NULL:
            raise MemoryError()
        self._headers = headers
        self._headers_cap = cap
        return 0

    cdef void _reset(self):
        self._arena_len = 0
        self._count = 0
        self._data.clear()
        self._materialized = False
        self._accesses = 0

    cdef void _append(self, bytes name, bytes value):
        cdef const char* name_src = name
        cdef const char* value_src = value
        cdef Py_ssize_t name_len = len(name)
        cdef Py_ssize_t value_len = len(value)
        cdef Py_ssize_t name_offset
        cdef Py_ssize_t value_offset
        cdef RawHeader* header
        self._reserve_arena(self._arena_len + name_len + value_len)
        self._reserve_headers(self._count + 1)
        name_offset = self._arena_len
        header = &self._headers[self._count]
        if self._mode == ARENA_SCAN or self._mode == ADAPTIVE:
            header.name_hash = _lower_copy_hash(
                self._arena + name_offset,
                name_src,
                name_len,
            )
        else:
            # Current lazy storage lowercases while parsing but does not need
            # a request-time lookup hash until it materializes.
            _lower_copy(self._arena + name_offset, name_src, name_len)
            header.name_hash = 0
        self._arena_len += name_len
        value_offset = self._arena_len
        if value_len:
            memcpy(self._arena + value_offset, value_src, <size_t>value_len)
        self._arena_len += value_len
        header.name_offset = <uint32_t>name_offset
        header.name_length = <uint32_t>name_len
        header.value_offset = <uint32_t>value_offset
        header.value_length = <uint32_t>value_len
        self._count += 1

    cdef void _load(self, tuple pairs):
        cdef object pair
        cdef bytes name
        cdef bytes value
        self._reset()
        for pair in pairs:
            name = pair[0]
            value = pair[1]
            self._append(name, value)
        if self._mode == EAGER_DICT:
            self._materialize()

    cdef void _materialize(self):
        cdef Py_ssize_t i
        cdef RawHeader* header
        cdef object key
        cdef object value
        cdef object existing
        cdef uint32_t name_hash
        if self._materialized:
            return
        for i in range(self._count):
            header = &self._headers[i]
            name_hash = header.name_hash
            if name_hash == 0:
                name_hash = _hash_bytes(
                    self._arena + header.name_offset,
                    header.name_length,
                )
            key = _intern_name(
                self._arena + header.name_offset,
                header.name_length,
                name_hash,
            )
            value = PyBytes_FromStringAndSize(
                self._arena + header.value_offset,
                header.value_length,
            )
            existing = self._data.get(key)
            if existing is None:
                self._data[key] = value
            elif type(existing) is list:
                existing.append(value)
            else:
                self._data[key] = [existing, value]
        self._materialized = True
        self.materializations += 1

    cdef bint _use_dict(self):
        if self._mode == EAGER_DICT or self._mode == LAZY_DICT:
            self._materialize()
            return True
        if self._mode == ADAPTIVE:
            self._accesses += 1
            if self._accesses >= self._promote_after:
                self._materialize()
                return True
        return False

    cdef object _scan_get(self, bytes name):
        cdef const char* query = name
        cdef Py_ssize_t query_len = len(name)
        cdef uint32_t query_hash = _hash_bytes(query, query_len)
        cdef Py_ssize_t i
        cdef RawHeader* header
        for i in range(self._count):
            header = &self._headers[i]
            if (
                header.name_hash == query_hash
                and header.name_length == <uint32_t>query_len
                and memcmp(
                    self._arena + header.name_offset,
                    query,
                    <size_t>query_len,
                ) == 0
            ):
                return PyBytes_FromStringAndSize(
                    self._arena + header.value_offset,
                    header.value_length,
                )
        return None

    cdef list _scan_getlist(self, bytes name):
        cdef const char* query = name
        cdef Py_ssize_t query_len = len(name)
        cdef uint32_t query_hash = _hash_bytes(query, query_len)
        cdef Py_ssize_t i
        cdef RawHeader* header
        cdef list result = []
        for i in range(self._count):
            header = &self._headers[i]
            if (
                header.name_hash == query_hash
                and header.name_length == <uint32_t>query_len
                and memcmp(
                    self._arena + header.name_offset,
                    query,
                    <size_t>query_len,
                ) == 0
            ):
                result.append(
                    PyBytes_FromStringAndSize(
                        self._arena + header.value_offset,
                        header.value_length,
                    )
                )
        return result

    cpdef object get(self, bytes name):
        cdef object value
        if self._use_dict():
            value = self._data.get(name)
            if type(value) is list:
                return value[0]
            return value
        return self._scan_get(name)

    cpdef list getlist(self, bytes name):
        cdef object value
        if self._use_dict():
            value = self._data.get(name)
            if value is None:
                return []
            if type(value) is bytes:
                return [value]
            return list(value)
        return self._scan_getlist(name)

    cpdef list items(self):
        cdef list result = []
        cdef object key
        cdef object value
        cdef object item
        # items() exposes all fields, so there is no allocation advantage in
        # retaining arena mode. Materialize to preserve dict grouping/order.
        self._materialize()
        for key, value in self._data.items():
            if type(value) is list:
                for item in value:
                    result.append((key, item))
            else:
                result.append((key, value))
        return result

    cpdef int run_batch(
        self,
        tuple pairs,
        tuple gets,
        tuple getlists,
        int iterations,
    ):
        cdef int iteration
        cdef bytes name
        cdef object value
        cdef list values
        cdef int checksum = 0
        for iteration in range(iterations):
            self._load(pairs)
            for name in gets:
                value = self.get(name)
                if value is not None:
                    checksum += len(value)
            for name in getlists:
                values = self.getlist(name)
                checksum += len(values)
                for value in values:
                    checksum += len(value)
        return checksum

    cpdef void load(self, tuple pairs):
        self._load(pairs)


MODE_EAGER_DICT = EAGER_DICT
MODE_LAZY_DICT = LAZY_DICT
MODE_ARENA_SCAN = ARENA_SCAN
MODE_ADAPTIVE = ADAPTIVE
