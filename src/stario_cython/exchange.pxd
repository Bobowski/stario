from stario_cython.compression_buf cimport StarioBrotli, StarioGzip
from stario_cython.headers cimport Headers
from stario_cython.request cimport Request

cdef class RequestExchange:
    cdef object _transport
    cdef list _date_box
    cdef object _compression
    cdef object _accept
    cdef public Headers headers
    cdef StarioBrotli* _brotli
    cdef StarioGzip* _gzip
    cdef object _out_buf
    cdef object _out_hold
    cdef Py_ssize_t _out_len
    cdef int _status_code
    cdef Py_ssize_t _declared_length
    cdef Py_ssize_t _bytes_written
    cdef object _available
    cdef int _brotli_level
    cdef int _brotli_window
    cdef int _gzip_level
    cdef bint _completed

    cdef public object app
    cdef public object span
    cdef public object route
    cdef object _connection
    cdef object _state
    cdef public Headers request_headers
    cdef public Request req
    cdef bint handler_done
    cdef bint handler_started
    cdef bint in_pool

    cdef object _chunks
    cdef object _cached
    cdef object _data_ready
    cdef object _body_fut
    cdef int _buffered
    cdef int _total_read
    cdef int _max_size
    cdef double _timeout
    cdef int _consumed_as
    cdef int _abort_reason
    cdef bint _body_active
    cdef bint _body_complete
    cdef bint _expect_continue
    cdef bint _waiting
    cdef char* _acc
    cdef Py_ssize_t _acc_len
    cdef Py_ssize_t _acc_cap
    cdef Py_ssize_t _expected_body
    cdef Py_ssize_t _stream_max_chunk

    cdef void reset(
        self,
        object connection,
        object app,
        object transport,
        list date_box,
        object compression,
        int max_body_size,
    )
    cdef void start_response(self)
    cdef void handler_finished(self)
    cdef void cancel_before_start(self)
    cdef void _maybe_recycle(self)
    cdef void park(self)
    cdef void release_global(self)
    cdef void reset_body(self, bint expect_continue)
    cdef void prepare_body_capacity(self, Py_ssize_t expected_size)
    cdef void c_feed(self, const char* at, size_t length)
    cdef void c_complete(self)
    cdef void c_abort(self)
    cdef int _acc_add(self, const char* at, size_t length) except -1
    cdef int _acc_reserve(self, Py_ssize_t need) except -1
    cdef object _acc_to_bytes(self)
    cdef void _emit_from_acc(self, Py_ssize_t n)
    cdef void reset_response(self, object accept_encoding)
    cdef void _apply_compression(self, object compression)
    cdef int _buf_add(self, const char* src, Py_ssize_t n) except -1
    cdef int _buf_bytes(self, object data) except -1
    cdef int _buf_uint(self, size_t n, int base) except -1
    cdef void _flush(self)
    cdef bint _may_compress(self, object data, object content_type, bint streaming)
    cdef int _frame(self, object data, object encoding, const unsigned char** out, size_t* out_len) except -1
    cdef int _block(self, object data, const unsigned char** out, size_t* out_len) except -1
    cdef int _finish(self, const unsigned char** out, size_t* out_len) except -1
    cdef int _write_native_chunk(self, const unsigned char* data, size_t n) except -1
    cdef int _ensure_brotli(self) except -1
    cdef int _ensure_gzip(self) except -1
    cdef void _free_compressors(self)
    cdef object _select(self, object data, object content_type, bint streaming)
    cdef void _raise_abort(self)
    cdef void _wake(self)
    cdef void _resolve_body_fut(self, object value)
    cdef object _take_chunk(self, int index)
    cdef void _maybe_continue(self)
    cdef void _done(self)
    cdef void _maybe_pause(self)

cdef RequestExchange acquire_exchange(
    object connection,
    object app,
    object transport,
    list date_box,
    object compression,
    int max_body_size,
)
