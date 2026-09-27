#ifndef STARIO_COMPRESSION_BUF_H
#define STARIO_COMPRESSION_BUF_H

#include <stddef.h>

typedef struct StarioBrotli StarioBrotli;
typedef struct StarioGzip StarioGzip;
typedef struct StarioZstd StarioZstd;

/* Borrowed output is valid until the next call on the same encoder, or free. */

StarioBrotli* stario_brotli_new(int level, int window_log);
StarioBrotli* stario_brotli_acquire(int level, int window_log);
int stario_brotli_block_borrowed(
    StarioBrotli* brotli,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
);
int stario_brotli_finish_borrowed(
    StarioBrotli* brotli,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
);
void stario_brotli_free(StarioBrotli* brotli);
void stario_brotli_release(StarioBrotli* brotli);

StarioGzip* stario_gzip_new(int level, int window_bits);
StarioGzip* stario_gzip_acquire(int level, int window_bits);
int stario_gzip_block_borrowed(
    StarioGzip* gzip,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
);
int stario_gzip_finish_borrowed(
    StarioGzip* gzip,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
);
void stario_gzip_free(StarioGzip* gzip);
void stario_gzip_release(StarioGzip* gzip);

StarioZstd* stario_zstd_new(int level, int window_log);
StarioZstd* stario_zstd_acquire(int level, int window_log);
int stario_zstd_block_borrowed(
    StarioZstd* zstd,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
);
int stario_zstd_finish_borrowed(
    StarioZstd* zstd,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
);
void stario_zstd_free(StarioZstd* zstd);
void stario_zstd_release(StarioZstd* zstd);

typedef struct StarioBrotliDecoder StarioBrotliDecoder;
typedef struct StarioZstdDecoder StarioZstdDecoder;

int stario_brotli_decompress_borrowed(
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
);
int stario_zstd_decompress_borrowed(
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
);

StarioBrotliDecoder* stario_brotli_decoder_new(void);
int stario_brotli_decoder_push_borrowed(
    StarioBrotliDecoder* decoder,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
);
int stario_brotli_decoder_finished(const StarioBrotliDecoder* decoder);
void stario_brotli_decoder_free(StarioBrotliDecoder* decoder);

StarioZstdDecoder* stario_zstd_decoder_new(void);
int stario_zstd_decoder_push_borrowed(
    StarioZstdDecoder* decoder,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
);
int stario_zstd_decoder_finished(const StarioZstdDecoder* decoder);
void stario_zstd_decoder_free(StarioZstdDecoder* decoder);

#endif
