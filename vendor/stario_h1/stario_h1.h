#ifndef STARIO_H1_H
#define STARIO_H1_H

#include <stdint.h>

#include "llhttp.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Complete-message HTTP/1.1 request parser. Same callbacks and llhttp_t
 * fields as llhttp_execute, but only for a request that is already fully
 * present in the buffer (headers + Content-Length body).
 *
 * Returns:
 *   > 0  bytes consumed (one complete request)
 *   STARIO_H1_INCOMPLETE (-2)  not a complete identity-body request;
 *                              caller should feed the same bytes to llhttp
 *   STARIO_H1_ERROR (-1)       definite protocol error
 *
 * Callbacks are not invoked on INCOMPLETE. On ERROR, some callbacks may
 * already have run (same as a failing llhttp_execute).
 *
 * Chunked, Expect: 100-continue without a complete body, unknown methods,
 * and messages larger than max_message are INCOMPLETE so llhttp stays the
 * streaming / edge-case engine.
 */
#define STARIO_H1_ERROR ((int64_t)-1)
#define STARIO_H1_INCOMPLETE ((int64_t)-2)

int64_t stario_h1_try(
    llhttp_t* parser,
    const llhttp_settings_t* settings,
    const char* data,
    size_t len,
    size_t max_message
);

#ifdef __cplusplus
}
#endif

#endif
