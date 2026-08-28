from libc.stddef cimport size_t
from libc.stdint cimport uint8_t

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

cdef enum:
    HEADER_NAME_STACK = 256

cdef int _fold_header_name(object name, char* buf, Py_ssize_t* out_n) except -1
cdef object _intern_name(const char* src, size_t n)
cdef object _encode_name(str name)

cdef class Headers:
    cdef list _names
    cdef list _values
    cdef Py_ssize_t _n

    cdef Py_ssize_t _find_n(self, const char* name, Py_ssize_t n) noexcept
    cdef void _store_at(self, Py_ssize_t index, object wire, object line)
    cdef object c_get(self, object name)
    cdef void c_set(self, object name, object value)
    cdef void c_add(self, object name, object value)
    cdef void c_remove(self, object name)
    cdef void c_clear(self)
    cdef bint c_empty(self)
    cdef bint c_vary_contains(self, object token)
    cdef void c_merge_vary(self, object token)
    cdef int _add_ba(self, object buf, Py_ssize_t* length, const char* src, Py_ssize_t n) except -1
    cdef int _write_pairs(self, object buf, Py_ssize_t* length, int skip_mode) except -1
    cdef int c_write_wire_ba(self, object buf, Py_ssize_t* length) except -1
    cdef int c_write_response_wire_ba(
        self,
        object buf,
        Py_ssize_t* length,
    ) except -1
    cdef int c_write_compressed_response_wire_ba(
        self,
        object buf,
        Py_ssize_t* length,
    ) except -1
