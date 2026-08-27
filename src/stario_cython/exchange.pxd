from libc.stddef cimport size_t
from libc.stdint cimport uint32_t

from stario_cython.compression_buf cimport StarioBrotli, StarioGzip
from stario_cython.headers cimport Headers

ctypedef struct RawHeader:
    uint32_t name_offset
    uint32_t name_length
    uint32_t value_offset
    uint32_t value_length

cdef object _status_line(int status)

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

cdef class RequestExchange:
    cdef object _transport
    cdef list _date_box
    cdef object _compression
    cdef int _req_encoding
    cdef bint _req_expect_continue
    cdef bint _req_connection_close
    cdef char* _req_arena
    cdef Py_ssize_t _req_arena_len
    cdef Py_ssize_t _req_arena_cap
    cdef RawHeader* _req_raw_headers
    cdef Py_ssize_t _req_raw_count
    cdef Py_ssize_t _req_raw_headers_cap
    cdef Py_ssize_t _req_pending_name_offset
    cdef Py_ssize_t _req_pending_name_length
    cdef Py_ssize_t _req_pending_value_offset
    cdef Py_ssize_t _req_pending_value_length
    cdef bint _req_pending_header
    cdef Py_ssize_t _req_host_index
    cdef Py_ssize_t _req_url_offset
    cdef Py_ssize_t _req_url_length
    cdef bint _req_accept_present
    cdef int _req_br_q
    cdef int _req_gzip_q
    cdef int _req_wildcard_q
    cdef int _req_identity_q
    cdef public Headers headers
    cdef StarioBrotli* _brotli
    cdef StarioGzip* _gzip
    cdef object _out_buf
    cdef object _out_hold
    cdef Py_ssize_t _out_len
    cdef int _status_code
    cdef Py_ssize_t _declared_length
    cdef Py_ssize_t _bytes_written
    cdef bint _brotli_enabled
    cdef bint _gzip_enabled
    cdef int _brotli_level
    cdef int _brotli_window
    cdef int _gzip_level
    cdef int _gzip_window
    cdef Py_ssize_t _compress_min_size
    cdef bint _completed

    cdef public object app
    cdef public object span
    cdef public object route
    cdef object _connection
    cdef object _state
    cdef public object request_headers
    cdef public Request req
    cdef bint handler_done
    cdef bint handler_started
    cdef bint in_pool

    cdef object _chunks
    cdef object _cached
    cdef object _data_ready
    cdef object _stall_handle
    cdef int _buffered
    cdef int _total_read
    cdef int _max_size
    cdef Py_ssize_t _read_max_size
    cdef double _timeout
    cdef int _consumed_as
    cdef int _abort_reason
    cdef bint _body_active
    cdef bint _body_complete
    cdef bint _expect_continue
    cdef bint _waiting
    cdef bint _discard_body
    cdef object _body_tail
    cdef Py_ssize_t _tail_used
    cdef Py_ssize_t _tail_cap
    cdef Py_ssize_t _expected_size
    cdef Py_ssize_t _stream_max_chunk

    cdef void reset(
        self,
        object connection,
        object app,
        object transport,
        list date_box,
        object compression,
        int max_body_size,
    ) noexcept
    cdef void start_response(self)
    cdef void handler_finished(self)
    cdef void cancel_before_start(self)
    cdef void _maybe_recycle(self)
    cdef void park(self)
    cdef void release_global(self)
    cdef void reset_body(self, bint expect_continue, Py_ssize_t expected_size) noexcept
    cdef int _reserve_request_arena(self, Py_ssize_t bytes_needed) noexcept
    cdef int _reserve_request_headers(self) noexcept
    cdef int append_request_url(self, const char* data, size_t length) noexcept
    cdef int append_request_header_name(self, const char* data, size_t length) noexcept
    cdef int append_request_header_value(self, const char* data, size_t length) noexcept
    cdef int finish_request_header(self) noexcept
    cdef void _scan_request_accept_encoding(
        self,
        const char* value,
        size_t length,
    ) noexcept
    cdef void _clear_request_headers(self) noexcept
    cdef void _clear_hot_request_headers(self) noexcept
    cdef void cache_hot_request_headers(self) noexcept
    cdef int c_feed(self, const char* at, size_t length) noexcept
    cdef int c_complete(self) noexcept
    cdef void c_abort(self)
    cdef void _clear_body_storage(self) noexcept
    cdef int _ensure_body_tail(self, Py_ssize_t received_before) noexcept
    cdef int _adopt_expected_body_buffer(self) noexcept
    cdef int _seal_body_tail(self) noexcept
    cdef object _body_to_bytes(self)
    cdef void reset_response(self, int encoding)
    cdef void _apply_compression(self, object compression)
    cdef int _buf_add(self, const char* src, Py_ssize_t n) except -1
    cdef int _buf_bytes(self, object data) except -1
    cdef int _buf_body(self, object body) except -1
    cdef int _buf_uint(self, size_t n, int base) except -1
    cdef void _flush(self)
    cdef Py_ssize_t _body_nbytes(self, object body) except -2
    cdef object _body_as_bytes(self, object body)
    cdef bint _may_compress(
        self,
        object data,
        object content_type,
        bint streaming,
        Py_ssize_t nbytes,
    )
    cdef int _frame(self, object data, object encoding, const unsigned char** out, size_t* out_len) except -1
    cdef int _block(self, object data, const unsigned char** out, size_t* out_len) except -1
    cdef int _finish(self, const unsigned char** out, size_t* out_len) except -1
    cdef int _write_native_chunk(self, const unsigned char* data, size_t n) except -1
    cdef int _ensure_brotli(self) except -1
    cdef int _ensure_gzip(self) except -1
    cdef void _free_compressors(self)
    cdef void _raise_abort(self)
    cdef void _wake(self)
    cdef void _cancel_stall_timer(self) noexcept
    cdef void _reset_stall_timer(self) noexcept
    cdef void _maybe_continue(self)
    cdef void _done(self)
    cdef void _maybe_pause(self)

cdef class RequestHeaders(Headers):
    cdef object _owner
    cdef bint _request_materialized

    cdef void _materialize(self)
    cdef object c_get(self, object name)
    cdef object c_get_n(self, const char* query, Py_ssize_t query_length)
    cdef object c_getlist_n(self, const char* query, Py_ssize_t query_length)
    cdef void c_set(self, object name, object value)
    cdef void c_add(self, object name, object value)
    cdef void c_remove(self, object name)
    cdef void c_clear(self)
    cdef void c_reset(self) noexcept
    cdef object c_request_host(self)

cdef RequestExchange acquire_exchange(
    object connection,
    object app,
    object transport,
    list date_box,
    object compression,
    int max_body_size,
)
