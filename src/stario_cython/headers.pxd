from libc.stdint cimport uint32_t

ctypedef struct RawHeader:
    uint32_t name_offset
    uint32_t name_length
    uint32_t value_offset
    uint32_t value_length

cdef class Headers:
    cdef dict _data
    cdef char _raw_inline[2048]
    cdef char* _raw_arena
    cdef Py_ssize_t _raw_len
    cdef Py_ssize_t _raw_cap
    cdef RawHeader _raw_headers_inline[16]
    cdef RawHeader* _raw_headers
    cdef Py_ssize_t _raw_count
    cdef Py_ssize_t _raw_headers_cap
    cdef Py_ssize_t _pending_name_offset
    cdef Py_ssize_t _pending_name_length
    cdef Py_ssize_t _pending_value_offset
    cdef Py_ssize_t _pending_value_length
    cdef bint _pending_header
    cdef Py_ssize_t _request_host_index
    cdef bint _materialized
    cdef bint _request_connection_close
    cdef bint _request_expect_continue
    cdef bint _request_accept_present
    cdef int _request_br_q
    cdef int _request_gzip_q
    cdef int _request_wildcard_q
    cdef int _request_identity_q

    cdef void add_raw(self, const char* name, size_t nlen, const char* value, size_t vlen)
    cdef void start_raw_header(self)
    cdef void append_raw_name(self, const char* data, size_t length)
    cdef void append_raw_value(self, const char* data, size_t length)
    cdef void finish_raw_header(self)
    cdef int _reserve_raw(self, Py_ssize_t bytes_needed) except -1
    cdef int _reserve_raw_headers(self) except -1
    cdef void _materialize(self)
    cdef object c_request_host(self)
    cdef void _scan_request_accept_encoding(
        self,
        const char* value,
        size_t length,
    ) noexcept
    cdef bint c_request_connection_close(self) noexcept
    cdef bint c_request_expect_continue(self) noexcept
    cdef int c_select_request_encoding(
        self,
        bint brotli_enabled,
        bint gzip_enabled,
    ) noexcept
    cdef object c_get(self, object name)
    cdef void c_set(self, object name, object value)
    cdef void c_add(self, object name, object value)
    cdef void c_remove(self, object name)
    cdef void c_clear(self)
    cdef bint c_empty(self)
    cdef void c_merge_vary(self, object token)
    cdef int _add_ba(self, object buf, Py_ssize_t* length, const char* src, Py_ssize_t n) except -1
    cdef int c_write_wire_ba(self, object buf, Py_ssize_t* length) except -1
    cdef int c_write_response_wire_ba(
        self,
        object buf,
        Py_ssize_t* length,
    ) except -1
