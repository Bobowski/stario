# Security audit: request body handling, memory, timeouts, DoS

Scope: `src/stario_cython/exchange.pyx`, `src/stario_cython/protocol.pyx`,
`src/stario_cython/timeouts.py`, `src/stario/http/config.py`,
`src/stario/http/request.py`, `src/stario/http/compression.py`,
`vendor/compression_buf.c`, `tests/cython/test_hardening.py`.

Commit audited: `48f8726` ("HTTP/2 ingest: no-CL bodies, header budget 431, ingest
opts (#44)"). Everything below was reproduced in-process against a build of that
tree using the repository's own `tests/cython/http.py` and `tests/cython/h2wire.py`
harnesses; the throwaway probe files were deleted and are not part of this change.

Everything is availability-impacting. No confidentiality or integrity break was
found in the body path itself; the one confidentiality item (F7) needs application
cooperation.

| # | Finding | Severity |
|---|---|---|
| F1 | No deadline covers an HTTP/1 request body the handler never finishes reading | High |
| F2 | Declared `Content-Length` is allocated eagerly, so `max_body_bytes` doubles as an unauthenticated allocation primitive | High |
| F3 | `max_body_bytes` / `max_header_bytes` are validated as Python ints but stored as C `int` | Medium |
| F4 | `STARIO_CYTHON_TIMEOUTS=off` silently disables every header, idle, and body-stall deadline | Medium |
| F5 | No cap on concurrent connections | Medium |
| F6 | 32-bit `Py_ssize_t` truncation in `_parse_content_length`; `uInt` truncation in the gzip wrapper | Low |
| F7 | `Request` / `RequestHeaders` alias a recycled arena and are not invalidated at park time | Low |

Checked and found **not** vulnerable: HTTP/2 `Content-Length` versus `DATA` length
mismatch (nghttp2 rejects both directions with `PROTOCOL_ERROR`); recycled
exchanges leaking a previous request's body; the keep-alive 413 drain path (the
header deadline survives trickles there); chunked and no-`Content-Length` bodies
buffering past `max_body_bytes`; request-side decompression (there is none, so no
request compression bomb); the HTTP/2 rapid-reset and `MAX_CONCURRENT_STREAMS`
budgets; the pipeline cap.

---

## F1 — No deadline covers a request body the handler never finishes reading

**Severity: High.** Availability. Remote, unauthenticated, no valid route needed.

### What happens

Three connection deadlines exist and all three miss this state:

- The header deadline is armed once per message in `_on_message_begin`
  (`src/stario_cython/protocol.pyx:1329`) and consumed by the first
  `_after_pump` (`src/stario_cython/protocol.pyx:1093`). It is re-armed only while
  the request is still undispatched or is an oversize-header drain
  (`src/stario_cython/protocol.pyx:1099-1101`).
- The idle deadline is armed by `response_completed`
  (`src/stario_cython/protocol.pyx:1709`) and cleared by the *next* byte that
  arrives (`src/stario_cython/protocol.pyx:926-930`). `_after_pump` re-arms it only
  when `reading_exchange is None` (`src/stario_cython/protocol.pyx:1108`), which is
  false for as long as a body is still being parsed.
- The body-stall deadline requires a consumer to be parked inside
  `_wait_for_body_data`: `_check_body_stall` returns early unless
  `exchange._waiting` (`src/stario_cython/protocol.pyx:1017`), and
  `_reset_stall_timer` cancels itself under the same condition
  (`src/stario_cython/exchange.pyx:3155-3157`).

When a handler returns without draining the body, `handler_finished` switches the
exchange to discard mode and explicitly cancels the stall timer
(`src/stario_cython/exchange.pyx:2521-2529`):

```2521:2529:src/stario_cython/exchange.pyx
    cdef void handler_finished(self):
        self.handler_done = True
        if self._body_active and not self._body_complete:
            self._discard_body = True
            self._cached = None
            self._clear_body_storage()
            self._cancel_stall_timer()
            self._connection.set_body_paused(self, False)
```

So the connection sits with `timeout_kind == TIMEOUT_NONE`, `reading_exchange`
still set, and no consumer waiting. One body byte is enough to clear the idle
deadline that `response_completed` armed, and nothing ever re-arms it. The
connection is then held until the client goes away or `max_body_bytes` worth of
body finally arrives.

### Reachability

The window is exactly the set of requests that dispatch at headers-complete
rather than at message-complete (`src/stario_cython/protocol.pyx:1443-1448`):

- `Content-Length` strictly greater than `SMALL_BODY_COMPLETE_DISPATCH`
  (262144, `src/stario_cython/protocol.pyx:131`)
- `Transfer-Encoding: chunked`, any size
- `Expect: 100-continue`, any size

Bodies at or below 256 KiB are safe because the request has not dispatched when
`_after_pump` runs, so the header deadline is re-armed. Measured against a build
of this tree with all three timeouts set to 150 ms and the sweeper at 50 ms, then
waiting 8x the longest timeout:

| Request shape | Connection closed? |
|---|---|
| `Transfer-Encoding: chunked` | no |
| `Content-Length: 300000` | no |
| `Content-Length: 262144` | yes |
| `Content-Length: 262143` | yes |
| `Expect: 100-continue` + `Content-Length: 1048576` | no |
| keep-alive 413 drain (`Content-Length` over the cap) | yes |

Over real sockets, 5 of 5 connections were still open after 10x every configured
timeout. Note that "a handler that returns without reading the body" is the
common case, not an exotic one: it includes every 404, every 405, every
authorization rejection, and every route that simply does not care about the
payload. The attacker does not need to know a single valid route on the target.

Cost to the attacker is one socket plus ~60 bytes of headers plus one body byte;
cost to the server is a file descriptor and a `RequestExchange` that can never be
recycled, because `_maybe_recycle` requires `_body_complete`
(`src/stario_cython/exchange.pyx:2539-2546`). Combined with F5 this exhausts the
process fd limit.

The same hole applies while a handler is still running and has not yet awaited
the body, so an application that does slow work before reading the payload has no
protection during that period either.

### Suggested fix

Give the "body still incoming" state a deadline of its own instead of relying on
a consumer being parked. The smallest change that fits the existing sweeper design
is a fourth `timeout_kind`:

1. Add `TIMEOUT_BODY = 3` next to the existing kinds
   (`src/stario_cython/protocol.pyx:371-373`).
2. In `_after_pump`, when the connection is HTTP/1, not rejected,
   `reading_exchange is not None`, the request has been dispatched, and
   `timeout_kind == TIMEOUT_NONE`, arm `TIMEOUT_BODY` with
   `self.body_timeout`.
3. In `data_received` (`src/stario_cython/protocol.pyx:926`), clear
   `TIMEOUT_BODY` as well as `TIMEOUT_IDLE` so that a client making real progress
   keeps resetting it; `_after_pump` re-arms it on the same pass. That turns it
   into a per-read inactivity deadline, which is the right semantics for an
   upload.
4. Teach `check_timeouts` to pick `self.body_timeout` for the new kind
   (`src/stario_cython/protocol.pyx:1037-1041`).

Also consider capping the total drain time for a discarded body, not just the
inactivity gap, so a client trickling one byte per `body_timeout - epsilon` cannot
hold a connection for `max_body_bytes * body_timeout` seconds. A simple absolute
deadline stored on the exchange when `_discard_body` is set covers it.

Regression tests to add to `tests/cython/test_hardening.py`, mirroring the
existing `test_stalled_chunked_body_aborts_without_hanging`: for each of chunked,
`Content-Length` over 256 KiB, and `Expect: 100-continue`, dispatch a handler that
responds without reading the body, feed one body byte, and assert the transport
closes.

---

## F2 — Declared `Content-Length` is allocated eagerly

**Severity: High.** Availability. Remote, unauthenticated.

### What happens

Two places size a buffer from the client-declared `Content-Length` before that
many bytes have arrived.

`_ensure_body_tail` uses the declared length as the first tail's capacity
(`src/stario_cython/exchange.pyx:3009-3027`):

```3009:3027:src/stario_cython/exchange.pyx
        cap = self._stream_max_chunk
        # Buffered body() wants one Content-Length-sized object so complete
        # does not b"".join ~32x64KiB tails. ...
        if (
            self._consumed_as != CONSUMED_STREAM
            and self._expected_size > 0
            and received_before == 0
        ):
            cap = self._expected_size
```

`_adopt_expected_body_buffer` allocates the whole declared length the moment
`read()` is called, even with zero body bytes received
(`src/stario_cython/exchange.pyx:3060`):

```3060:3063:src/stario_cython/exchange.pyx
        dest = PyBytes_FromStringAndSize(NULL, self._expected_size)
        if dest is None:
            PyErr_Clear()
            return -1
```

`_expected_size` is the declared `Content-Length`, checked only against
`max_body_bytes` (`src/stario_cython/protocol.pyx:1407` for HTTP/1,
`src/stario_cython/protocol.pyx:2265` for HTTP/2). That check is a *ceiling on
what may be stored*, but here it becomes *how much an unauthenticated peer can
make the server commit up front*. Nothing forces the peer to ever send the bytes.

The per-call `max_size` argument does not help. `read()` records it into
`_read_max_size` and then immediately adopts a buffer sized from the declared
`Content-Length` (`src/stario_cython/exchange.pyx:3445-3447`), so a handler that
defensively writes `await c.req.body(max_size=4096)` still triggers the full
allocation.

### Measured amplification

Against a build of this tree, using tracemalloc deltas on the server process:

| Shape | Attacker bytes on the wire | Server heap | Ratio |
|---|---|---|---|
| HTTP/1, `body(max_size=4096)`, declared 8 MiB, **zero** body bytes | ~200 | 8.0 MiB | ~42,000x |
| HTTP/1, 8 connections, declared 10 MiB each, 1 body byte each | 440 | 80.1 MiB | ~190,000x |
| HTTP/2, 64 streams on one connection, declared 10 MiB each, 1 DATA byte each | 2,816 | 640.3 MiB | ~230,000x |
| HTTP/2, 256 streams (the `H2_MAX_CONCURRENT` cap) on one connection | 11,264 | 2,560.7 MiB | ~238,000x |

HTTP/2 is the worse case because `H2_MAX_CONCURRENT = 256`
(`src/stario_cython/protocol.pyx:143`) multiplies the per-stream allocation onto
a single TCP connection. At stock settings that is 2.5 GiB of resident heap for
11 KB of traffic and one file descriptor.

The memory is reclaimed when the body-stall deadline fires, but only if a consumer
is parked; the default `body_timeout` is 30 s
(`src/stario/http/request.py:18`), so the peak is sustained for 30 s and can be
renewed immediately on a fresh connection. If the handler does not read the body,
F1 applies instead and there is no deadline at all.

### Suggested fix

Decouple "how much will you buffer in total" from "how much will you allocate
before seeing the bytes". Concretely, in both `_ensure_body_tail`
(`src/stario_cython/exchange.pyx:3003`) and `_adopt_expected_body_buffer`
(`src/stario_cython/exchange.pyx:3036`), clamp the initial capacity to a modest
ceiling and grow geometrically toward `_expected_size` as data actually lands:

```
cdef Py_ssize_t eager_cap = self._expected_size
if eager_cap > EAGER_BODY_ALLOC_MAX:          # e.g. 256 KiB, reuse SMALL_BODY_DRAIN
    eager_cap = received_before * 2
    if eager_cap < EAGER_BODY_ALLOC_MAX:
        eager_cap = EAGER_BODY_ALLOC_MAX
    if eager_cap > self._expected_size:
        eager_cap = self._expected_size
```

This keeps the optimisation the comment is after (one `Content-Length`-sized
object, no `b"".join`) for the sub-256 KiB bodies that dominate, while making a
declared 10 MiB body cost 256 KiB until the client has actually paid for the rest.
The doubling means at most one extra copy per power of two, and the final join is
still cheap.

While in `read()`, also fold `_read_max_size` into the sizing decision: if the
handler asked for at most N bytes, never allocate more than
`min(_expected_size, N) + 1`.

Independently, consider making the effective HTTP/2 per-connection body budget
explicit rather than `H2_MAX_CONCURRENT * max_body_bytes`. Either lower
`H2_MAX_CONCURRENT` (an application server is not a CDN — the same argument the
code already makes for the rapid-reset bucket at
`src/stario_cython/protocol.pyx:144-147`), or track a per-connection sum of
in-flight body bytes and refuse `HEADERS` past it with `REFUSED_STREAM`.

---

## F3 — Size limits are validated as Python ints but stored as C `int`

**Severity: Medium.** Availability (crash loop on a plausible configuration).

`RequestPolicy` accepts any `max_body_bytes >= 1` and any
`max_header_bytes >= 256` (`src/stario/http/config.py:66-75`), and
`STARIO_REQUESTS_MAX_BODY_BYTES` feeds it directly
(`src/stario/http/config.py:114-116`). The protocol and exchange store them in C
`int` fields:

```723:725:src/stario_cython/protocol.pyx
    cdef int head_bytes
    cdef int max_header_bytes
    cdef int max_body_bytes
```

```171:173:src/stario_cython/exchange.pxd
    cdef int _buffered
    cdef int _total_read
    cdef int _max_size
```

`RequestExchange.reset` also takes `int max_body_size`
(`src/stario_cython/exchange.pxd:214`, `src/stario_cython/exchange.pyx:2420`).

Confirmed behaviour: `RequestPolicy(max_body_bytes=3 * 1024**3)` constructs
happily and reports `3221225472`, and then constructing an `HttpProtocol` with it
raises `OverflowError: value too large to convert to int`. Since the protocol is
constructed per connection inside the listener factory, a deployment that
configures a >2 GiB upload limit accepts and then immediately fails every
connection, with an error that points nowhere near the setting that caused it.

Two secondary hazards in the same family:

- `c_feed` computes `new_total` as `Py_ssize_t` and then narrows it back into the
  `int` field (`src/stario_cython/exchange.pyx:3203`, `3215`, `3234`, `3256`).
  With `_max_size` bounded by `INT_MAX` the ordinary path cannot overflow, but the
  discard branch at `src/stario_cython/exchange.pyx:3207-3216` returns *before*
  the `_max_size` check and keeps accumulating, so the narrowing there is
  unguarded. It is not currently reachable to 2 GiB (`response_completed` removes
  the HTTP/2 stream from `h2_streams` before discard can run for long, and HTTP/1
  discard falls through to the cap), but it is one refactor away from signed
  overflow, which is undefined behaviour under `-O3`.
- `RawHeader` stores arena offsets as `uint32_t`
  (`src/stario_cython/exchange.pxd:58-62`). Safe only because `max_header_bytes`
  is itself capped by the `int` field above.

**Fix:** widen `_buffered`, `_total_read`, `_max_size`, `max_body_bytes`,
`max_header_bytes`, `head_bytes`, and the `max_body_size` parameters to
`Py_ssize_t`; and/or add an explicit upper bound in `RequestPolicy.__init__` with
a clear error, so the failure happens at configuration time rather than per
connection. Move the discard-mode counter increment to after the `_max_size`
check, or saturate it.

---

## F4 — `STARIO_CYTHON_TIMEOUTS=off` is an unguarded production footgun

**Severity: Medium.** Availability.

`parse_timeout_mode` reads the variable once at import
(`src/stario_cython/timeouts.py:33-42`) and accepts `0`, `off`, `none`, `false`,
`no`. `MODE_OFF` then short-circuits every deadline in the stack:
`_arm_timeout` returns immediately (`src/stario_cython/protocol.pyx:1002-1003`),
the HTTP/2 header deadline is skipped (`src/stario_cython/protocol.pyx:1071-1073`),
the fallback sweeper is never started (`src/stario_cython/protocol.pyx:675`), and
`_reset_stall_timer` cancels itself (`src/stario_cython/exchange.pyx:3158-3160`).
With it set, plain slowloris — a connection that sends a partial request line and
nothing else — is unbounded.

The variable is documented only in `CYTHON.md:121` and the benchmark notes as a
"profiling hatch". It is not part of the `STARIO_*` surface that
`src/stario/cli/env.py` reads, it is not surfaced in
`Server._record_startup_attrs` alongside the other timeout settings
(`src/stario/http/server.py:541-582`), and nothing logs a warning. A stray
environment variable in a container image therefore removes every DoS protection
silently.

**Fix:** record `timeout_cleanup_mode()` in the startup span next to
`server.timeout.*`, and emit a warning-level log line when it is not `sweep`.
Consider requiring a more deliberate opt-in name (for example
`STARIO_UNSAFE_DISABLE_TIMEOUTS=1`) so it cannot be reached by a plausible typo
such as `STARIO_CYTHON_TIMEOUTS=0`.

---

## F5 — No cap on concurrent connections

**Severity: Medium.** Availability.

`ServerConfig` exposes `backlog` (`src/stario/http/config.py:189-193`) but no
maximum number of accepted connections, and the listener keeps every protocol in
an unbounded `set` (`src/stario/http/server.py:474-476`). Accept is never
throttled. The only backpressure is the header and idle deadlines, which F1 and
F4 both defeat.

**Fix:** add `max_connections` to `RequestPolicy` or `ServerConfig`, and in
`connection_made` either close immediately with `503`/`Connection: close` or pause
the listening socket once `len(connections)` reaches the cap. Report the current
count in the startup and shutdown spans.

---

## F6 — Narrowing conversions in `Content-Length` parsing and the gzip wrapper

**Severity: Low.** Integrity on 32-bit builds; availability on absurd response
sizes. Both are latent rather than exploitable on the supported 64-bit CPython
3.14 target.

`_parse_content_length` accumulates into `uint64_t`, validates against
`INT64_MAX`, and then returns `<Py_ssize_t>value`
(`src/stario_cython/exchange.pyx:195-208`). On a 32-bit build `Py_ssize_t` is
32 bits, so `Content-Length: 4294967297` truncates to `1`. That is a
request-smuggling shape (the framework and any upstream proxy disagree about where
the body ends). The parser is otherwise correct: it rejects empty values,
non-digits, and therefore negatives and `+`/whitespace forms, and duplicate
conflicting values are rejected at `src/stario_cython/exchange.pyx:2294-2295`.

`vendor/compression_buf.c:293` casts the input length to zlib's `uInt`
(`gzip->strm.avail_in = (uInt)in_len;`), and line 302 does the same for
`avail_out`. A single response body write above 4 GiB would silently truncate.

**Fix:** bound the validation constant in `_parse_content_length` by
`PY_SSIZE_T_MAX` rather than `INT64_MAX`, and loop the zlib calls over
`UINT_MAX`-sized slices (or reject oversize inputs explicitly).

Related, informational: `brotli_pool` / `gzip_pool`
(`vendor/compression_buf.c:34-37`) are process-global mutable arrays with no
locking. That is fine for the single-threaded asyncio model Stario uses today, but
it would corrupt under two event loops in two threads in one process.

---

## F7 — `Request` / `RequestHeaders` alias a recycled arena

**Severity: Low.** Confidentiality, but only with application cooperation.

`RequestHeaders` reads header bytes straight out of the owning exchange's malloc'd
arena on every access (`src/stario_cython/exchange.pyx:3484-3494`, `3496-3506`,
`3532-3567`, `3581-3599`), and `Request.query` binds a span into the same arena
(`src/stario_cython/exchange.pyx:1558-1569`).

`park()` — the keep-alive recycle path — clears the body storage and the hot
header cache but does not neutralise `req`, `request_headers`, or the
`_req_host_index` / `_req_cookie_index` / `_req_authorization_index` shortcuts
(`src/stario_cython/exchange.pyx:2548-2569`). The arena itself is reused rather
than freed whenever it is at or below 8 KiB
(`src/stario_cython/exchange.pyx:2388-2395`), which is the common case.

Consequence: an application that retains `c.req`, `c.req.headers`, or
`c.req.cookies` past the end of its handler — a background `app.create_task`, a
cache, a deferred log record, an SSE closure — will read whatever request occupies
that exchange next, including `Authorization` and `Cookie`. There is no
use-after-free (the frees inside `_clear_request_headers` are followed by resetting
the count and indices in the same function, so a stale view sees an empty header
set), but there is cross-request disclosure.

Probed and clean: a second keep-alive request with no body does *not* see the
first request's body bytes.

**Fix:** neutralise the views in `park()` — point `RequestHeaders._owner` at a
detached empty object, or set `_req_raw_count = 0` and the three shortcut indices
to `-1` there rather than waiting for the next `reset()`. Failing that, document
in the handler API reference that `Request` and its header/cookie/query views are
valid only for the duration of the handler and must be copied before being
captured.

---

## Test coverage gaps in `tests/cython/test_hardening.py`

The existing timeout tests cover the undispatched cases well
(`test_partial_headers_time_out`, `test_trickle_headers_do_not_reset_header_timeout`,
`test_stalled_deferred_small_body_times_out`,
`test_keep_alive_413_idle_times_out`) and one dispatched case
(`test_stalled_chunked_body_aborts_without_hanging`, which passes only because the
handler awaits `body()` and so parks a consumer). Missing:

- Any case where the handler responds *without* reading a body that is still
  arriving. This is the F1 blind spot.
- Any assertion about how much memory a declared `Content-Length` commits. This is
  the F2 blind spot.
- `Expect: 100-continue` where the handler never reads the body.
- A `max_body_bytes` value above `INT_MAX` reaching `HttpProtocol` (F3).
- HTTP/2 aggregate body accounting across concurrent streams.
