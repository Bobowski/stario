cdef class Headers:
    cdef dict _data

    cdef object c_get(self, object name)
    cdef void c_set(self, object name, object value)
    cdef void c_add(self, object name, object value)
    cdef void c_remove(self, object name)
    cdef void c_clear(self)
    cdef bint c_empty(self)
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

cdef object _intern_name(const char* src, size_t n)
cdef object _encode_name(str name)
