from libc.stdint cimport uint8_t, uint16_t, uint64_t


cdef extern from "llhttp.h":
    ctypedef struct llhttp_t:
        pass

    ctypedef int (*llhttp_data_cb)(llhttp_t*, const char* at, size_t length)
    ctypedef int (*llhttp_cb)(llhttp_t*)

    ctypedef struct llhttp_settings_t:
        llhttp_cb on_message_begin
        llhttp_data_cb on_url
        llhttp_data_cb on_status
        llhttp_data_cb on_method
        llhttp_data_cb on_version
        llhttp_data_cb on_header_field
        llhttp_data_cb on_header_value
        llhttp_data_cb on_chunk_extension_name
        llhttp_data_cb on_chunk_extension_value
        llhttp_cb on_headers_complete
        llhttp_data_cb on_body
        llhttp_cb on_message_complete
        llhttp_cb on_url_complete
        llhttp_cb on_status_complete
        llhttp_cb on_method_complete
        llhttp_cb on_version_complete
        llhttp_cb on_header_field_complete
        llhttp_cb on_header_value_complete
        llhttp_cb on_chunk_extension_name_complete
        llhttp_cb on_chunk_extension_value_complete
        llhttp_cb on_chunk_header
        llhttp_cb on_chunk_complete
        llhttp_cb on_reset

    enum llhttp_type:
        HTTP_BOTH
        HTTP_REQUEST
        HTTP_RESPONSE

    enum llhttp_errno:
        HPE_OK
        HPE_PAUSED
        HPE_PAUSED_UPGRADE
        HPE_USER

    void llhttp_init(llhttp_t* parser, llhttp_type type, const llhttp_settings_t* settings)
    int llhttp_execute(llhttp_t* parser, const char* data, size_t len)
    int llhttp_should_keep_alive(const llhttp_t* parser)
    uint8_t llhttp_get_method(llhttp_t* parser)
    uint8_t llhttp_get_http_major(llhttp_t* parser)
    uint8_t llhttp_get_http_minor(llhttp_t* parser)
    uint8_t llhttp_get_upgrade(llhttp_t* parser)
    const char* llhttp_method_name(int method)
    const char* llhttp_get_error_reason(const llhttp_t* parser)


cdef extern from "stario_alloc.h":
    llhttp_t* stario_parser_new()
    void stario_parser_del(llhttp_t* parser)
    llhttp_settings_t* stario_settings_new()
    void stario_parser_set_data(llhttp_t* parser, void* data)
    void* stario_parser_get_data(const llhttp_t* parser)
    uint16_t stario_parser_flags(const llhttp_t* parser)
    uint64_t stario_parser_content_length(const llhttp_t* parser)
