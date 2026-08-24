cdef class Request:
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

    cdef void reset(
        self,
        object method,
        object path,
        object query_bytes,
        object protocol_version,
        bint keep_alive,
        object headers,
        object body,
    )
