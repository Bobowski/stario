#include "compression_buf.h"

#include <brotli/encode.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <zlib.h>

#ifndef STARIO_BROTLI_HTTP_WINDOW
#define STARIO_BROTLI_HTTP_WINDOW 18
#endif

struct StarioBrotli {
    BrotliEncoderState* state;
    unsigned char* out;
    size_t out_cap;
    int finished;
};

struct StarioGzip {
    z_stream strm;
    unsigned char* out;
    size_t out_cap;
    int finished;
};

static int grow_buffer(unsigned char** buf, size_t* cap, size_t used) {
    size_t next_cap;
    unsigned char* next;

    if (*cap > SIZE_MAX / 2) {
        return -1;
    }
    next_cap = *cap < 256 ? 256 : *cap * 2;
    if (next_cap <= used) {
        if (used == SIZE_MAX) {
            return -1;
        }
        next_cap = used + 1;
    }
    next = (unsigned char*)realloc(*buf, next_cap);
    if (next == NULL) {
        return -1;
    }
    *buf = next;
    *cap = next_cap;
    return 0;
}

static int brotli_append(
    StarioBrotli* brotli,
    BrotliEncoderOperation operation,
    const unsigned char* in,
    size_t in_len,
    size_t* used
) {
    size_t available_in = in_len;
    size_t available_out;
    size_t previous_in;
    size_t previous_used;
    const unsigned char* next_in = in;
    unsigned char* next_out;

    for (;;) {
        if (
            *used == brotli->out_cap &&
            grow_buffer(&brotli->out, &brotli->out_cap, *used) != 0
        ) {
            return -1;
        }
        available_out = brotli->out_cap - *used;
        next_out = brotli->out + *used;
        previous_in = available_in;
        previous_used = *used;
        if (!BrotliEncoderCompressStream(
            brotli->state,
            operation,
            &available_in,
            &next_in,
            &available_out,
            &next_out,
            NULL
        )) {
            return -1;
        }
        *used = (size_t)(next_out - brotli->out);

        if (operation == BROTLI_OPERATION_FINISH) {
            if (BrotliEncoderIsFinished(brotli->state)) {
                brotli->finished = 1;
                break;
            }
        } else if (
            available_in == 0 &&
            !BrotliEncoderHasMoreOutput(brotli->state)
        ) {
            break;
        }

        if (available_in == previous_in && *used == previous_used) {
            return -1;
        }
    }
    return 0;
}

static int brotli_emit(
    StarioBrotli* brotli,
    BrotliEncoderOperation first,
    BrotliEncoderOperation flush,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    size_t used = 0;

    if (
        brotli == NULL || brotli->state == NULL || brotli->finished ||
        out == NULL || out_len == NULL ||
        (in == NULL && in_len != 0)
    ) {
        return -1;
    }
    *out = NULL;
    *out_len = 0;
    if (brotli_append(brotli, first, in, in_len, &used) != 0) {
        return -1;
    }
    if (flush != first && brotli_append(brotli, flush, NULL, 0, &used) != 0) {
        return -1;
    }
    *out = brotli->out;
    *out_len = used;
    return 0;
}

StarioBrotli* stario_brotli_new(int level, int window_log) {
    StarioBrotli* brotli = (StarioBrotli*)malloc(sizeof(StarioBrotli));
    uint32_t lgwin;

    if (brotli == NULL) {
        return NULL;
    }
    brotli->state = BrotliEncoderCreateInstance(NULL, NULL, NULL);
    brotli->out = NULL;
    brotli->out_cap = 0;
    brotli->finished = 0;
    if (brotli->state == NULL) {
        free(brotli);
        return NULL;
    }
    lgwin = window_log > 0 ? (uint32_t)window_log : STARIO_BROTLI_HTTP_WINDOW;
    if (
        !BrotliEncoderSetParameter(
            brotli->state, BROTLI_PARAM_QUALITY, (uint32_t)level
        ) ||
        !BrotliEncoderSetParameter(brotli->state, BROTLI_PARAM_LGWIN, lgwin)
    ) {
        BrotliEncoderDestroyInstance(brotli->state);
        free(brotli);
        return NULL;
    }
    return brotli;
}

int stario_brotli_block_borrowed(
    StarioBrotli* brotli,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    return brotli_emit(
        brotli,
        BROTLI_OPERATION_PROCESS,
        BROTLI_OPERATION_FLUSH,
        in,
        in_len,
        out,
        out_len
    );
}

int stario_brotli_finish_borrowed(
    StarioBrotli* brotli,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    return brotli_emit(
        brotli,
        BROTLI_OPERATION_FINISH,
        BROTLI_OPERATION_FINISH,
        in,
        in_len,
        out,
        out_len
    );
}

void stario_brotli_free(StarioBrotli* brotli) {
    if (brotli == NULL) {
        return;
    }
    if (brotli->state != NULL) {
        BrotliEncoderDestroyInstance(brotli->state);
    }
    free(brotli->out);
    free(brotli);
}

static int gzip_deflate(
    StarioGzip* gzip,
    int flush,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    size_t used = 0;
    int rc;

    if (
        gzip == NULL || gzip->finished ||
        out == NULL || out_len == NULL ||
        (in == NULL && in_len != 0)
    ) {
        return -1;
    }
    *out = NULL;
    *out_len = 0;
    gzip->strm.next_in = (Bytef*)in;
    gzip->strm.avail_in = (uInt)in_len;
    for (;;) {
        if (
            used == gzip->out_cap &&
            grow_buffer(&gzip->out, &gzip->out_cap, used) != 0
        ) {
            return -1;
        }
        gzip->strm.next_out = gzip->out + used;
        gzip->strm.avail_out = (uInt)(gzip->out_cap - used);
        rc = deflate(&gzip->strm, flush);
        used = (size_t)(gzip->strm.next_out - gzip->out);
        if (rc == Z_STREAM_END) {
            gzip->finished = 1;
            break;
        }
        if (rc != Z_OK) {
            return -1;
        }
        if (gzip->strm.avail_in == 0 && gzip->strm.avail_out > 0) {
            break;
        }
    }
    *out = gzip->out;
    *out_len = used;
    return 0;
}

StarioGzip* stario_gzip_new(int level) {
    StarioGzip* gzip = (StarioGzip*)malloc(sizeof(StarioGzip));

    if (gzip == NULL) {
        return NULL;
    }
    memset(&gzip->strm, 0, sizeof(gzip->strm));
    if (deflateInit2(
        &gzip->strm, level, Z_DEFLATED, 31, 8, Z_DEFAULT_STRATEGY
    ) != Z_OK) {
        free(gzip);
        return NULL;
    }
    gzip->out = NULL;
    gzip->out_cap = 0;
    gzip->finished = 0;
    return gzip;
}

int stario_gzip_block_borrowed(
    StarioGzip* gzip,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    return gzip_deflate(gzip, Z_SYNC_FLUSH, in, in_len, out, out_len);
}

int stario_gzip_finish_borrowed(
    StarioGzip* gzip,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    return gzip_deflate(gzip, Z_FINISH, in, in_len, out, out_len);
}

void stario_gzip_free(StarioGzip* gzip) {
    if (gzip == NULL) {
        return;
    }
    deflateEnd(&gzip->strm);
    free(gzip->out);
    free(gzip);
}
