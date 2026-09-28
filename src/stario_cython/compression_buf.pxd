from libc.stddef cimport size_t

cdef extern from "compression_buf.h":
    ctypedef struct StarioBrotli:
        pass

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
    void stario_brotli_release(StarioBrotli* brotli)

    ctypedef struct StarioGzip:
        pass

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
    void stario_gzip_release(StarioGzip* gzip)

    ctypedef struct StarioZstd:
        pass

    StarioZstd* stario_zstd_acquire(int level, int window_log)
    int stario_zstd_block_borrowed(
        StarioZstd* zstd,
        const unsigned char* data,
        size_t in_len,
        const unsigned char** out,
        size_t* out_len,
    )
    int stario_zstd_finish_borrowed(
        StarioZstd* zstd,
        const unsigned char* data,
        size_t in_len,
        const unsigned char** out,
        size_t* out_len,
    )
    void stario_zstd_release(StarioZstd* zstd)

    int stario_brotli_decompress_borrowed(
        const unsigned char* data,
        size_t in_len,
        const unsigned char** out,
        size_t* out_len,
    )
    int stario_zstd_decompress_borrowed(
        const unsigned char* data,
        size_t in_len,
        const unsigned char** out,
        size_t* out_len,
    )

    ctypedef struct StarioBrotliDecoder:
        pass

    StarioBrotliDecoder* stario_brotli_decoder_new()
    int stario_brotli_decoder_push_borrowed(
        StarioBrotliDecoder* decoder,
        const unsigned char* data,
        size_t in_len,
        const unsigned char** out,
        size_t* out_len,
    )
    int stario_brotli_decoder_finished(const StarioBrotliDecoder* decoder)
    void stario_brotli_decoder_free(StarioBrotliDecoder* decoder)

    ctypedef struct StarioZstdDecoder:
        pass

    StarioZstdDecoder* stario_zstd_decoder_new()
    int stario_zstd_decoder_push_borrowed(
        StarioZstdDecoder* decoder,
        const unsigned char* data,
        size_t in_len,
        const unsigned char** out,
        size_t* out_len,
    )
    int stario_zstd_decoder_finished(const StarioZstdDecoder* decoder)
    void stario_zstd_decoder_free(StarioZstdDecoder* decoder)
