from libc.stddef cimport size_t

cdef extern from "compression_buf.h":
    ctypedef struct StarioBrotli:
        pass

    StarioBrotli* stario_brotli_new(int level, int window_log)
    StarioBrotli* stario_brotli_acquire(int level, int window_log)
    int stario_brotli_block_borrowed(
        StarioBrotli* brotli,
        const unsigned char* data,
        size_t in_len,
        const unsigned char** out,
        size_t* out_len,
    )
    int stario_brotli_finish_borrowed(
        StarioBrotli* brotli,
        const unsigned char* data,
        size_t in_len,
        const unsigned char** out,
        size_t* out_len,
    )
    void stario_brotli_free(StarioBrotli* brotli)
    void stario_brotli_release(StarioBrotli* brotli)

    ctypedef struct StarioGzip:
        pass

    StarioGzip* stario_gzip_new(int level, int window_bits)
    StarioGzip* stario_gzip_acquire(int level, int window_bits)
    int stario_gzip_block_borrowed(
        StarioGzip* gzip,
        const unsigned char* data,
        size_t in_len,
        const unsigned char** out,
        size_t* out_len,
    )
    int stario_gzip_finish_borrowed(
        StarioGzip* gzip,
        const unsigned char* data,
        size_t in_len,
        const unsigned char** out,
        size_t* out_len,
    )
    void stario_gzip_free(StarioGzip* gzip)
    void stario_gzip_release(StarioGzip* gzip)
