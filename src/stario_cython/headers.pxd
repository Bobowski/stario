cdef class Headers:
    cdef dict _data
    cdef bint _request_connection_close
    cdef bint _request_expect_continue
    cdef bint _request_accept_present
    cdef int _request_br_q
    cdef int _request_gzip_q
    cdef int _request_wildcard_q
    cdef int _request_identity_q

    cdef void add_raw(self, const char* name, size_t nlen, const char* value, size_t vlen)
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
