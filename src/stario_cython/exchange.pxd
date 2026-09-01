from libc.stddef cimport size_t
from libc.stdint cimport int32_t, uint8_t, uint32_t, uint64_t

from stario_cython.compression_buf cimport StarioBrotli, StarioGzip

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
    ABORT_NONE = 0
    ABORT_TOO_LARGE = 1
    ABORT_DISCONNECTED = 2
    ABORT_TIMEOUT = 3

cdef int _fold_header_name(object name, char* buf, Py_ssize_t* out_n) except -1
cdef object _intern_name(const char* src, size_t n)
cdef object _encode_name(str name)

cdef class Headers:
    cdef list _names
    cdef list _values
    cdef Py_ssize_t _n

    cdef Py_ssize_t _find_n(self, const char* name, Py_ssize_t n) noexcept
    cdef Py_ssize_t _compact_except(self, const char* name, Py_ssize_t n) noexcept
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
    cdef int _write_pair_at(self, object buf, Py_ssize_t* length, Py_ssize_t index) except -1
    cdef int c_write_wire_ba(self, object buf, Py_ssize_t* length) except -1
    cdef object c_scan_respond(self, object content_type)
    cdef void c_require_respond_length(self, object existing_cl, object expected) except *
    cdef int c_write_respond_pairs(
        self,
        object buf,
        Py_ssize_t* length,
        bint skip_ce,
    ) except -1

ctypedef struct RawHeader:
    uint32_t name_offset
    uint32_t name_length
    uint32_t value_offset
    uint32_t value_length

cdef object _status_line(int status)

cdef class ParsedCookies:
    cdef object _headers
    cdef list _lines

    cdef void bind_request_headers(self, object headers) noexcept
    cdef void _extend_lines(self, object lines) except *
    cdef list _cookie_lines(self)
    cdef object _get_arena(self, const char* name, Py_ssize_t nlen)
    cdef object _get_lines(self, const char* name, Py_ssize_t nlen)
    cdef bint _has_any(self)

cdef class Request:
    cdef public object method
    cdef public object path
    cdef public object headers
    cdef public object protocol_version
    cdef public bint keep_alive
    cdef object _query_bytes
    cdef object _q_owner
    cdef Py_ssize_t _q_off
    cdef Py_ssize_t _q_len
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
    cdef object _materialize_query(self)
    cdef void _rebind_query(self, object query_bytes) noexcept
    cdef void bind_query_span(self, object owner, Py_ssize_t off, Py_ssize_t n) noexcept
    cdef void prefetch_host(self) noexcept

cdef class RequestExchange:
    cdef object _transport
    cdef list _date_box
    cdef object _compression
    cdef int _req_encoding
    cdef bint _req_expect_continue
    cdef bint _req_connection_close
    cdef Py_ssize_t _req_content_length
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
    cdef Py_ssize_t _req_cookie_index
    cdef Py_ssize_t _req_authorization_index
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
    cdef double _stall_deadline
    cdef uint64_t _stall_touch
    cdef uint64_t _stall_seen
    cdef int _buffered
    cdef int _total_read
    cdef int _max_size
    cdef Py_ssize_t _read_max_size
    cdef double _timeout
    cdef int _consumed_as
    cdef int _abort_reason
    cdef bint _body_active
    cdef bint _body_complete
    cdef bint _http2
    cdef int32_t _h2_stream_id
    cdef object _h2_pending
    cdef Py_ssize_t _h2_pending_off
    cdef bint _h2_body_done
    cdef object _h2_method
    cdef bint _h2_got_method
    cdef bint _h2_got_path
    cdef bint _h2_got_authority
    cdef bint _h2_dispatched
    cdef bint _h2_headers_done
    cdef bint _h2_headers_sent
    cdef bint _h2_headers_too_large
    cdef Py_ssize_t _h2_head_bytes
    cdef bint _h2_awaiting_headers
    cdef double _h2_header_deadline
    cdef object _h2_date_line
    cdef object _h2_date_bare
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
        double body_timeout,
    ) noexcept
    cdef void bind_http2(self, int32_t stream_id) noexcept
    cdef object _h2_date_value(self)
    cdef void _h2_respond(self, object body, object content_type, int status, Py_ssize_t nbytes)
    cdef void start_response(self)
    cdef void handler_finished(self)
    cdef void cancel_before_start(self)
    cdef void _maybe_recycle(self)
    cdef void park(self)
    cdef void release_global(self)
    cdef void reset_body(self, bint expect_continue, Py_ssize_t expected_size) noexcept
    cdef void mark_nobody(self) noexcept
    cdef int _reserve_request_arena(self, Py_ssize_t bytes_needed) noexcept
    cdef int _reserve_request_headers(self) noexcept
    cdef int append_request_url(self, const char* data, size_t length) noexcept
    cdef int append_request_header_name(self, const char* data, size_t length) noexcept
    cdef int append_request_header_value(self, const char* data, size_t length) noexcept
    cdef int append_request_header(
        self,
        const char* name,
        size_t name_length,
        const char* value,
        size_t value_length,
        bint names_already_lower,
    ) noexcept
    cdef bint header_value_equals(
        self,
        Py_ssize_t index,
        const char* value,
        size_t n,
    ) noexcept
    cdef int finish_request_header(self) noexcept
    cdef int _commit_request_header(self) noexcept
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
    cdef void fire_body_stall(self)
    cdef void _maybe_continue(self)
    cdef void _done(self)
    cdef void _maybe_pause(self)

cdef class RequestHeaders(Headers):
    cdef object _owner

    cdef object c_get(self, object name)
    cdef Py_ssize_t c_find_n(self, const char* query, Py_ssize_t query_length) noexcept
    cdef object c_value_str(self, Py_ssize_t index)
    cdef object c_get_n(self, const char* query, Py_ssize_t query_length)
    cdef object c_getlist_n(self, const char* query, Py_ssize_t query_length)
    cdef void c_set(self, object name, object value)
    cdef void c_add(self, object name, object value)
    cdef void c_remove(self, object name)
    cdef void c_clear(self)
    cdef object c_request_indexed(self, Py_ssize_t index)
    cdef void c_parse_cookies(self, dict out) except *

cdef RequestExchange acquire_exchange(
    object connection,
    object app,
    object transport,
    list date_box,
    object compression,
    int max_body_size,
    double body_timeout,
)
