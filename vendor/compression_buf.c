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
    int level;
    int window_log;
};

struct StarioGzip {
    z_stream strm;
    unsigned char* out;
    size_t out_cap;
    int finished;
    int level;
    int window_bits;
};

#define STARIO_CODEC_POOL_MAX 32
#define STARIO_RETAINED_OUTPUT_MAX (64 * 1024)

static StarioBrotli* brotli_pool[STARIO_CODEC_POOL_MAX];
static size_t brotli_pool_count = 0;
static StarioGzip* gzip_pool[STARIO_CODEC_POOL_MAX];
static size_t gzip_pool_count = 0;

static void trim_output(unsigned char** out, size_t* cap) {
    if (*cap > STARIO_RETAINED_OUTPUT_MAX) {
        free(*out);
        *out = NULL;
        *cap = 0;
    }
}

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

static int brotli_start(StarioBrotli* brotli, int level, int window_log) {
    uint32_t lgwin;
    brotli->state = BrotliEncoderCreateInstance(NULL, NULL, NULL);
    brotli->finished = 0;
    if (brotli->state == NULL) {
        return -1;
    }
    lgwin = window_log > 0 ? (uint32_t)window_log : STARIO_BROTLI_HTTP_WINDOW;
    if (
        !BrotliEncoderSetParameter(
            brotli->state, BROTLI_PARAM_QUALITY, (uint32_t)level
        ) ||
        !BrotliEncoderSetParameter(brotli->state, BROTLI_PARAM_LGWIN, lgwin)
    ) {
        BrotliEncoderDestroyInstance(brotli->state);
        brotli->state = NULL;
        return -1;
    }
    brotli->level = level;
    brotli->window_log = (int)lgwin;
    return 0;
}

StarioBrotli* stario_brotli_new(int level, int window_log) {
    StarioBrotli* brotli = (StarioBrotli*)calloc(1, sizeof(StarioBrotli));
    if (brotli == NULL || brotli_start(brotli, level, window_log) != 0) {
        free(brotli);
        return NULL;
    }
    return brotli;
}

StarioBrotli* stario_brotli_acquire(int level, int window_log) {
    StarioBrotli* brotli;
    int resolved_window = window_log > 0 ? window_log : STARIO_BROTLI_HTTP_WINDOW;
    size_t i;
    for (i = 0; i < brotli_pool_count; i++) {
        brotli = brotli_pool[i];
        if (brotli->level == level && brotli->window_log == resolved_window) {
            brotli_pool[i] = brotli_pool[--brotli_pool_count];
            if (brotli_start(brotli, level, resolved_window) == 0) {
                return brotli;
            }
            stario_brotli_free(brotli);
            return NULL;
        }
    }
    return stario_brotli_new(level, resolved_window);
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

void stario_brotli_release(StarioBrotli* brotli) {
    if (brotli == NULL) {
        return;
    }
    if (brotli->state != NULL) {
        BrotliEncoderDestroyInstance(brotli->state);
        brotli->state = NULL;
    }
    brotli->finished = 0;
    trim_output(&brotli->out, &brotli->out_cap);
    if (brotli_pool_count < STARIO_CODEC_POOL_MAX) {
        brotli_pool[brotli_pool_count++] = brotli;
    } else {
        stario_brotli_free(brotli);
    }
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

StarioGzip* stario_gzip_new(int level, int window_bits) {
    StarioGzip* gzip = (StarioGzip*)malloc(sizeof(StarioGzip));

    if (gzip == NULL) {
        return NULL;
    }
    memset(&gzip->strm, 0, sizeof(gzip->strm));
    if (deflateInit2(
        &gzip->strm, level, Z_DEFLATED, 16 + window_bits, 8, Z_DEFAULT_STRATEGY
    ) != Z_OK) {
        free(gzip);
        return NULL;
    }
    gzip->out = NULL;
    gzip->out_cap = 0;
    gzip->finished = 0;
    gzip->level = level;
    gzip->window_bits = window_bits;
    return gzip;
}

StarioGzip* stario_gzip_acquire(int level, int window_bits) {
    StarioGzip* gzip;
    size_t i;
    for (i = 0; i < gzip_pool_count; i++) {
        gzip = gzip_pool[i];
        if (gzip->level == level && gzip->window_bits == window_bits) {
            gzip_pool[i] = gzip_pool[--gzip_pool_count];
            if (deflateReset(&gzip->strm) != Z_OK) {
                stario_gzip_free(gzip);
                return NULL;
            }
            gzip->finished = 0;
            return gzip;
        }
    }
    return stario_gzip_new(level, window_bits);
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

void stario_gzip_release(StarioGzip* gzip) {
    if (gzip == NULL) {
        return;
    }
    trim_output(&gzip->out, &gzip->out_cap);
    if (gzip_pool_count < STARIO_CODEC_POOL_MAX) {
        gzip_pool[gzip_pool_count++] = gzip;
    } else {
        stario_gzip_free(gzip);
    }
}
