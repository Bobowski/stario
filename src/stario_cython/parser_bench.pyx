# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True
"""Native parser sinks for ``benchmarks/parser_micro.py``."""

from libc.stdint cimport int64_t

from stario_cython.llhttp cimport *


cdef int _sink_ok(llhttp_t* parser) noexcept:
    return 0


cdef int _sink_data(llhttp_t* parser, const char* at, size_t length) noexcept:
    return 0


cdef llhttp_settings_t* _SINK = NULL


cdef void _bind_sink() noexcept:
    global _SINK
    if _SINK != NULL:
        return
    _SINK = stario_settings_new()
    _SINK.on_message_begin = _sink_ok
    _SINK.on_url = _sink_data
    _SINK.on_header_field = _sink_data
    _SINK.on_header_value = _sink_data
    _SINK.on_header_value_complete = _sink_ok
    _SINK.on_headers_complete = _sink_ok
    _SINK.on_body = _sink_data
    _SINK.on_message_complete = _sink_ok


def bench_llhttp(bytes data, int repeats):
    cdef llhttp_t* parser
    cdef const char* ptr = data
    cdef size_t n = <size_t>len(data)
    cdef int i
    cdef int err
    _bind_sink()
    parser = stario_parser_new()
    if parser == NULL:
        raise MemoryError()
    try:
        llhttp_init(parser, HTTP_REQUEST, _SINK)
        for i in range(repeats):
            err = llhttp_execute(parser, ptr, n)
            if err != HPE_OK:
                raise RuntimeError("llhttp_execute failed")
    finally:
        stario_parser_del(parser)


def bench_h1(bytes data, int repeats):
    cdef llhttp_t* parser
    cdef const char* ptr = data
    cdef size_t n = <size_t>len(data)
    cdef int i
    cdef int64_t consumed
    _bind_sink()
    parser = stario_parser_new()
    if parser == NULL:
        raise MemoryError()
    try:
        llhttp_init(parser, HTTP_REQUEST, _SINK)
        for i in range(repeats):
            consumed = stario_h1_try(parser, _SINK, ptr, n, n)
            if consumed != <int64_t>n:
                raise RuntimeError("stario_h1_try did not consume the request")
    finally:
        stario_parser_del(parser)


def parse_h1_once(bytes data):
    cdef llhttp_t* parser
    cdef int64_t consumed
    _bind_sink()
    parser = stario_parser_new()
    if parser == NULL:
        raise MemoryError()
    try:
        llhttp_init(parser, HTTP_REQUEST, _SINK)
        consumed = stario_h1_try(
            parser, _SINK, data, <size_t>len(data), <size_t>len(data)
        )
        return (
            int(consumed),
            int(llhttp_get_method(parser)),
            int(stario_parser_flags(parser)),
        )
    finally:
        stario_parser_del(parser)
