# Cython HTTP protocol

HTTP/1 is [llhttp](https://github.com/nodejs/llhttp); HTTP/2 is nghttp2.
Both live in one `HttpProtocol` and share `RequestExchange` in
`src/stario_cython`. `stario serve` uses this protocol. uvloop is optional
(`STARIO_LOOP=uvloop`). Current numbers, implemented surface, holes, and
lab assumptions:
[Current snapshot (2026-09-21)](#current-snapshot-2026-09-21). Current performance, what is implemented, what is
missing, and the lab assumptions:
[Current snapshot (2026-09-21)](#current-snapshot-2026-09-21).

Linux builds need `pkg-config`, the Brotli development package, and
`libnghttp2-dev`. Gzip links system zlib (`-lz`). The native protocol
offers `br` and `gzip` only; Python response helpers still negotiate zstd
for non-native writers.

```bash
uv venv --python 3.14
uv pip install --python .venv/bin/python -e ".[uvloop]" cython setuptools wheel pytest pytest-asyncio
.venv/bin/python setup.py
PYTHONPATH=src:. .venv/bin/python -m stario.cli serve examples.cython.hello:bootstrap
# or: PYTHONPATH=src:. .venv/bin/python -m stario_cython examples.cython.hello:bootstrap
```

Direct TLS: `STARIO_SSL_CERTFILE` / `STARIO_SSL_KEYFILE` (or
`ServerConfig(ssl=…)`). ALPN advertises `h2` then `http/1.1`.

`STARIO_HOST` and `STARIO_PORT` set the bind address.

Same-host check (2026-08-31): picohttpparser is ~2.5–3.3× llhttp in a
parser-only microbench, but end-to-end HTTP/1 is about even on GET
(0.98–1.07×) and behind on a tiny JSON POST (0.85×). HTTP/1 stays on
llhttp. HTTP/2 GETs on the same process are ~1.8× HTTP/1 when `h2load`
uses 100 streams per connection. TLS ALPN selects `h2` or `http/1.1`;
a self-signed cert serves both. See
[`benchmarks/server/pico-h2-20260831.md`](benchmarks/server/pico-h2-20260831.md).

`PYTHONPATH=src` is required so `stario_cython` resolves after the inplace
build.

## Current snapshot (2026-09-21)

`cython-core` after 4.2 (`Route` / `c.match` / `Assets` / `stario.json`)
and the request hotpath (fresh `Request`, lazy query, Router `_lookup`).
Python httptools stays on `main`. Production HTTP here is Cython:
`stario serve` and `python -m stario_cython` are the same protocol.

Official wrk suite, one worker, `10s` × 5 measured + 1 warmup, 4 vCPU
cloud Xeon, same-host loopback. Full tables:
[`benchmarks/server/baseline-20260921-core.md`](benchmarks/server/baseline-20260921-core.md).

### How we're doing

Headline is **Stario CLI** vs Granian RSGI 2.8.3 (Rust, no framework) and
FastAPI + Uvicorn. Median req/s.

| Endpoint | Stario | Granian | vs Granian | vs FastAPI |
| --- | ---: | ---: | ---: | ---: |
| Plaintext | 137,565 | 149,473 | 0.92× | **3.0×** |
| Request fields | 101,591 | 150,195 | 0.68× | **3.1×** |
| JSON small POST | 103,106 | 105,270 | 0.98× | **3.6×** |
| Octet 64KB | 32,793 | 30,271 | **1.08×** | **2.4×** |
| Octet 2MB (buffer) | 1,809 | 1,477 | **1.23×** | **1.3×** |
| Octet 2MB (stream) | 3,449 | 3,209 | **1.07×** | **3.0×** |
| Multipart 2MB | 1,993 | 1,415 | **1.41×** | **1.5×** |

Granian wins static GET and the request-fields case because it is not a
framework. Stario is even on a small async JSON POST and ahead once the
body is tens of kilobytes. Stario is the fastest *frameworked* server on
this suite (ahead of BlackSheep+Granian, Socketify GET, Sanic, FastAPI,
Falcon, Django-Bolt, Robyn). Socketify still leads 2MB **stream** (native
chunk callbacks): 3,624 vs Stario 3,449.

The leftover GET gap is **not** pooled-Request overhead. `/plaintext`
never builds query/cookies; dispatch allocates one `Request` and runs
`create_task(handler)`. Request-fields is the real hole: wrk uses 4096
distinct `/user/{id}?q=` URLs (larger than the 1024 LRU), then the
handler reads path param + query + header and interpolates a line.
Granian does `path.rsplit` and a raw query scan and stays at ~150k.

An earlier params re-run that hit `/user/42` every time was **0.85×**
Granian (LRU always hot). This capture is the honest one.

### Implemented

- **HTTP/1** llhttp and **HTTP/2** nghttp2 in one `HttpProtocol`, sharing
  pooled `RequestExchange` (arena, compressors, output buffers).
- **Fresh `Request` per dispatch** via `make_request()` (`__new__`, no
  Python `__init__`). Keep-alive does not reuse the Request object.
- **Lazy** `ParsedQuery` / cookies / host until first read. Plaintext and
  JSON GET never allocate them.
- Protocol binds the Python Router **LRU** (`app._lookup`, max 1024)
  directly. Trailing-slash **308** on the decoded path.
- 4.2 app APIs: `Route`, `c.match`, `Assets`, `stario.json`. Handlers are
  `async def(c, w)`.
- Bodies with `Content-Length` ≤ **256 KiB** complete before the handler
  starts (`SMALL_BODY_COMPLETE_DISPATCH`); larger bodies dispatch at
  headers-complete so `stream()` can run early. Expect: 100-continue.
- Timeouts share the Date-header tick (header 5s, idle 5s, body-stall 30s,
  pipeline cap 8). Hatch: `STARIO_CYTHON_TIMEOUTS=off`.
- Optional uvloop (`STARIO_LOOP=uvloop`). TLS ALPN `h2` then `http/1.1`.
  Native compress is **br** and **gzip** (system zlib). Official benches
  turn compression off.
- Official suite shape: static GET, one interpolating request-fields GET,
  async uploads (`await asyncio.sleep(0)`) from small JSON through 2MB
  buffer / stream / multipart. Both Stario runner targets are Cython.

### Missing / not doing (on purpose)

App and Router stay Python. Do not revive:

- a dual Cython App/Router
- a contiguous serializer
- pooled `asyncio.Event`
- static / pre-serialized handlers
- pooled / reset `Request` objects

Python httptools lives on `main`. Native zstd is not offered (Python
response helpers still negotiate it for non-native writers). picohttpparser
is not the H1 parser: parser-only it is ~2.5–3.3× llhttp, but end-to-end
GET was even and a tiny JSON POST was slower.

### Not working / known holes

- **Request-fields vs Granian (0.68×).** Framework routing + three field
  reads vs a Rust server with hand-written path parse. Closing this
  without a Cython Router means paying Python `_lookup` / `Match` /
  query / header on a working set that misses the LRU.
- **Official wrk suite is HTTP/1 only.** H2 works (`tests/cython/test_h2.py`);
  same-host `h2load` GETs were ~1.8× HTTP/1 at 100 streams/connection
  (2026-08-31). Not re-measured on this snapshot.
- **Native entry vs CLI.** `python -m stario_cython` was ~12% slower on
  plaintext here (123k vs 138k). Same protocol; treat CLI as production.
- **4 vCPU loopback ceiling / noise.** Plaintext stdev ±5–7k. ~150k is
  this machine’s wrk/Xeon band, not a portable absolute.
- Falcon’s comparison app was still on the old `/json` `/validate` suite
  and aborted the first full run; it is aligned now. Robyn/Django-Bolt
  still buffer the stream route (no streaming API).

### Main assumptions

- Same-host wrk, 4 vCPU Xeon, 16 GiB, one worker, Python 3.14, uvloop.
- Responses are plaintext; serializers cannot win a case.
- wrk working set 4096 > LRU 1024, so request-fields is not a permanent
  cache hit.
- Upload handlers always `await asyncio.sleep(0)` so the case is actually
  async.
- Granian RSGI is a **ceiling**, not a peer framework: no router, no
  `Request` object, path/query/header by hand.
- Compression off, tracer noop. Numbers are relative signal, not a claim
  about another machine.
- `stario` and `stario-cython` runner targets are both this Cython
  protocol. A Python vs Cython comparison needs `main`.

## Historical snapshot (2026-09-19)

Old endpoint set (plaintext / JSON GET / single-URL params, no `sleep(0)`).
Granian led GET (plaintext **0.87×**, JSON **0.85×**, params **0.70×**).
Params was 4.2 dropping the lookup LRU; restoring it brought params in
line with plaintext (**0.93× / 0.89× / 0.85×**) on a cached `/user/42`.
Full tables:
[`benchmarks/server/baseline-20260919.md`](benchmarks/server/baseline-20260919.md).

## Merge snapshot (2026-08-28)

Official `benchmarks/server` suite, one worker, `10s` × 5 measured + 1
warmup, IQR trimming. Full lab log:
[`benchmarks/server/baseline-20260828.md`](benchmarks/server/baseline-20260828.md).

### vs unmodified `cython-core` (current Cython)

`f80d6f0` (`20260828T153450Z`) → this branch `460114d`
(`20260828T154518Z`). Same host, sequential.

| Endpoint | Current (`cython-core`) | This branch | Ratio |
| --- | ---: | ---: | ---: |
| Plaintext | 125,495 ± 879 | 130,032 ± 602 | **1.04×** |
| JSON | 121,997 ± 2,671 | 119,338 ± 4,601 | 0.98× |
| Params | 117,005 ± 211 | 122,350 ± 1,991 | **1.05×** |
| Validate JSON | 67,425 ± 512 | 106,918 ± 1,108 | **1.59×** |
| Form POST | 75,089 ± 1,124 | 126,328 ± 3,486 | **1.68×** |
| JSON 1KB | 68,866 ± 93 | 108,776 ± 781 | **1.58×** |
| Octet 64KB | 22,013 ± 390 | 37,948 ± 406 | **1.72×** |
| Octet 2MB (buffer) | 3,004 ± 14 | 3,172 ± 71 | **1.06×** |
| Octet 2MB (stream) | 3,435 ± 48 | 3,391 ± 120 | 0.99× |
| Multipart 2MB | 2,998 ± 59 | 3,178 ± 18 | **1.06×** |

Small POST and the 64KiB fixture jump because deferred Content-Length ≤64KiB
bodies complete before the handler’s `body()` wait. GET / 2MB stream are
noise. Timeouts did not kill plaintext (+3.6%).

### vs Python httptools (current Python Stario)

Same suite `20260828T193136Z`. Python: `stario.cli serve`. Cython:
`python -m stario_cython`.

| Endpoint | Current Python | This Cython | Cython / Python |
| --- | ---: | ---: | ---: |
| Plaintext | 74,752 ± 274 | 132,903 ± 3,484 | **1.78×** |
| JSON | 72,983 ± 1,742 | 133,817 ± 7,033 | **1.83×** |
| Params | 70,804 ± 95 | 126,707 ± 5,128 | **1.79×** |
| Validate JSON | 56,566 ± 619 | 112,068 ± 4,145 | **1.98×** |
| Form POST | 60,305 ± 106 | 123,813 ± 1,677 | **2.05×** |
| JSON 1KB | 55,945 ± 431 | 110,946 ± 4,462 | **1.98×** |
| Octet 64KB | 20,065 ± 202 | 37,772 ± 487 | **1.88×** |
| Octet 2MB (buffer) | 1,981 ± 33 | 3,441 ± 20 | **1.74×** |
| Octet 2MB (stream) | 2,969 ± 7 | 3,652 ± 16 | **1.23×** |
| Multipart 2MB | 2,007 ± 80 | 3,563 ± 99 | **1.77×** |

GET ~1.8×. Small POST ~2.0× (was ~1.2× on 27 Aug). 2MB stream 1.23×.

### All servers (this host)

Median req/s. Same knobs, one worker. Stario: `20260828T193136Z`. Granian /
Socketify / Robyn / Django-Bolt: `20260828T183308Z`. BlackSheep+Granian:
`20260828T180757Z`. Bold is best in that column. Granian is RSGI, no
framework. Full ± and lab log:
[`benchmarks/server/baseline-20260828.md`](benchmarks/server/baseline-20260828.md).

| Server | Plaintext | JSON | Params | Validate | Form | JSON 1KB | 64KB | 2MB buf | 2MB stream | Multipart |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Granian RSGI | **146,875** | **144,111** | **146,201** | 106,610 | 118,928 | **114,517** | 31,212 | 1,429 | 3,108 | 3,138 |
| Stario Cython | 132,903 | 133,817 | 126,707 | **112,068** | **123,813** | 110,946 | **37,772** | **3,441** | 3,652 | **3,563** |
| Socketify | 122,497 | 103,563 | 79,977 | 13,267 | 19,556 | 13,293 | 12,365 | 674 | **4,062** | 663 |
| BlackSheep+Granian | 113,487 | 102,801 | 105,259 | 42,628 | 46,420 | 42,622 | 21,029 | 1,214 | 3,155 | 1,228 |
| Stario Python | 74,752 | 72,983 | 70,804 | 56,566 | 60,305 | 55,945 | 20,065 | 1,981 | 2,969 | 2,007 |
| Django-Bolt | 30,071 | 31,946 | 30,310 | 25,585 | 26,272 | 26,252 | 20,037 | 1,499 | 1,478 | 1,488 |
| Robyn | 25,780 | 25,206 | 19,351 | 18,382 | 4,721 | 18,270 | 15,629 | 735 | 751 | 498 |

### Timeouts that land

Header, idle, and body-stall share **one** cleanup: the Date-header tick
(1s) calls `check_timeouts(now)` on live connections. One `loop.time()`
per wake. No extra sweeper, no per-request `TimerHandle`, no stall
`call_later` per chunk.

| | |
| --- | --- |
| Header | 5s, while headers or a deferred small body are still arriving |
| Idle | 5s, only when the connection is idle |
| Body stall | 30s, generation counter on the exchange |
| Pipeline cap | 8 |
| Tests | fallback sweeper at 50ms (`STARIO_CYTHON_TIMEOUT_SWEEP`) |
| Hatch | `STARIO_CYTHON_TIMEOUTS=off` |

wrk: sweep ≈ callbacks on plaintext; timeouts-off ~+5% plaintext (keep
timeouts); 10ms sweep −7% on 2MB stream; 50ms and 1s Date tick are a wash
(129.3k / 130.8k / 128.0k). Callbacks were deleted.

App/Router stay Python. Do not revive dual Cython App/Router, a contiguous
serializer, pooled `asyncio.Event`, or static/pre-serialized handlers.
