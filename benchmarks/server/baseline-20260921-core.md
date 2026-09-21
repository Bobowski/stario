# Server benchmarks — 2026-09-21 (`cython-core`)

Official `benchmarks/server` suite on **this Cython line**: 4.2 `Route` /
`c.match`, a fresh `Request` per dispatch, lazy query, and the 1024-entry
Router LRU. Python httptools is not on this branch. Both Stario targets are
the same llhttp + nghttp2 protocol (`stario serve` and
`python -m stario_cython`).

Headline number is **Stario CLI**. Native entry is the same protocol; it was
a few percent slower on GET in this capture (run order / two venvs).

Relative signal on one machine — not lab-grade absolutes. **Do not compare
req/s** to `baseline-20260827.md` / `baseline-20260828.md` (different host)
or to `baseline-20260919.md` (old endpoints: cached `/user/42`, JSON GET,
sync-style POST without `sleep(0)`). `baseline-20260921.md` is the dual
Python/Cython capture on the tree *before* this merge.

## Reproduce

```bash
DURATION=10s RUNS=5 WARMUP=1 THREADS=2 \
  CONNECTIONS=128 UPLOAD_CONNECTIONS=32 \
  PYTHON=3.14 WRK=wrk KEEP_RAW=1 \
  benchmarks/server/run.sh
```

Run directory: `benchmarks/server/results/20260921T120138Z` (gitignored).
Falcon is from a follow-up pass (`20260921T132129Z`) after aligning its app
with the reshaped routes; same knobs, same host.

## Environment

| | |
|---|---|
| CPU | 4 vCPUs, Intel Xeon, x86_64 |
| RAM | 16 GiB |
| OS | Linux 6.12.94+ |
| Python | 3.14.7 |
| Client | wrk 4.1.0, `-t2 -c128` (GET / small JSON), `-c32` (64KB / 2MB) |
| Topology | wrk and servers on the same host, `127.0.0.1` |
| Commit | `abfe24b` (Falcon app fix `0d52ca2`) |
| Stario | 4.1.1 (this checkout, Cython protocol, uvloop, compression off) |
| Packages | Granian 2.8.3, Socketify 0.0.31, Robyn 0.88.0, Sanic 25.12.1, Django-Bolt 0.11.1 (Django 6.1.1), BlackSheep 2.6.3, FastAPI 0.141.1, Falcon 4.3.1, Uvicorn 0.53.0 |

## Methodology

- One worker/process per target.
- Endpoints: `plaintext`, `request`, `json-small`, `post-octet-64k`,
  `post-octet-2m`, `post-stream-2m`, `multipart-2m`.
- **5 measured + 1 warmup**, `DURATION=10s`, IQR trimming via
  `benchmarks/server/stats.py`.
- Static `/plaintext` returns a prebuilt body. `request` interpolates path
  param `user_id`, query `q`, and header `x-request-id` into a plain line
  (wrk working set **4096**, larger than the 1024-entry `_lookup` LRU).
  Every upload handler reads the body then `await asyncio.sleep(0)`.
- Responses are **plain text** so JSON serializers cannot win a case.
- `wrk` HTTP/1 only. wrk outputs were scanned for `Non-2xx` / socket errors:
  none on this capture.

## Ranking (median req/s)

Bold is the highest cell in that column.

### GET

Sorted by plaintext.

| Server | Plaintext | Request fields |
| --- | ---: | ---: |
| Granian RSGI (Rust, no framework) | **149,473 ± 1,295** | **150,195 ± 1,989** |
| Stario (Cython, CLI) | 137,565 ± 5,858 | 101,591 ± 1,223 |
| Stario (Cython, native entry) | 123,050 ± 4,436 | 95,564 ± 3,927 |
| BlackSheep + Granian ASGI | 113,099 ± 2,560 | 67,805 ± 474 |
| Socketify (uWebSockets/libuv) | 108,372 ± 124 | 60,630 ± 217 |
| BlackSheep + Uvicorn ASGI | 59,892 ± 1,451 | 44,521 ± 961 |
| Falcon ASGI + Uvicorn | 54,661 ± 1,004 | 50,664 ± 1,034 |
| Sanic (uvloop) | 52,785 ± 633 | 33,709 ± 415 |
| FastAPI + Uvicorn ASGI | 45,604 ± 787 | 32,538 ± 355 |
| Django-Bolt (Actix/Tokio) | 26,599 ± 124 | 25,094 ± 45 |
| Robyn (Rust/Actix) | 22,020 ± 387 | 16,699 ± 96 |

Plaintext on this host is a ~150k band for Granian. Stario CLI sits at
**0.92×**. That leftover is the Rust server vs Cython llhttp +
`create_task` + `respond()`, not routing: `/plaintext` never reads query
or headers.

Request fields drop everyone except Granian. Granian has no router
(`path.startswith` + `rsplit` + raw query string) and stays in the
plaintext band (150k). Stario pays Python `_lookup` (working set larger
than the LRU), `c.match.params`, lazy `query.get`, and a header read, then
interpolates a line — **0.68×** Granian, still the fastest frameworked
server on that case (**3.1×** FastAPI, **1.5×** BlackSheep+Granian).

The 2026-09-20 params re-run (**0.85×** Granian) used one URL (`/user/42`)
so the LRU was a permanent hit. This capture is the honest one.

### Upload / async

Sorted by JSON small.

| Server | JSON small | 64KB | 2MB buffer | 2MB stream | Multipart 2MB |
| --- | ---: | ---: | ---: | ---: | ---: |
| Granian RSGI | **105,270 ± 1,863** | 30,271 ± 345 | 1,477 ± 4 | 3,209 ± 24 | 1,415 ± 63 |
| Stario (Cython, CLI) | 103,106 ± 4,196 | **32,793 ± 144** | 1,809 ± 111 | 3,449 ± 35 | 1,993 ± 26 |
| Stario (Cython, native entry) | 98,141 ± 697 | 32,338 ± 590 | **1,891 ± 137** | 3,479 ± 109 | **2,000 ± 77** |
| Falcon ASGI + Uvicorn | 43,856 ± 728 | 15,218 ± 87 | 1,441 ± 19 | 1,655 ± 358 | 1,385 ± 31 |
| BlackSheep + Uvicorn | 43,358 ± 851 | 14,789 ± 110 | 722 ± 12 | 1,967 ± 31 | 730 ± 7 |
| Sanic | 43,167 ± 767 | 19,176 ± 1,693 | 1,633 ± 176 | 2,112 ± 12 | 1,491 ± 65 |
| BlackSheep + Granian | 40,309 ± 253 | 14,793 ± 137 | 1,098 ± 1 | 3,303 ± 88 | 1,068 ± 11 |
| FastAPI + Uvicorn | 28,629 ± 683 | 13,513 ± 136 | 1,371 ± 63 | 1,137 ± 501 | 1,359 ± 33 |
| Django-Bolt | 21,676 ± 16 | 14,865 ± 224 | 855 ± 23 | 776 ± 50 | 825 ± 38 |
| Robyn | 16,861 ± 147 | 14,503 ± 219 | 749 ± 40 | 776 ± 129 | 497 ± 28 |
| Socketify | 13,952 ± 467 | 11,673 ± 120 | 697 ± 19 | **3,624 ± 219** | 694 ± 23 |

Small JSON POST: Granian 105k, Stario CLI **0.98×**. Socketify falls over
here because its Python `on_data` future is a lot of work per small body.

Large bodies: Stario leads buffered 64KB / 2MB and multipart. Socketify
leads the 2MB **stream** case (native chunk callbacks). Stario stream is
close (3.45k vs 3.62k).

## Versus Granian RSGI (native, no framework)

| Endpoint | Stario CLI | Stario native | Granian | CLI / Granian | Native / Granian |
| --- | ---: | ---: | ---: | ---: | ---: |
| Plaintext | 137,565 | 123,050 | 149,473 | 0.92× | 0.82× |
| Request fields | 101,591 | 95,564 | 150,195 | 0.68× | 0.64× |
| JSON small | 103,106 | 98,141 | 105,270 | 0.98× | 0.93× |
| Octet 64KB | 32,793 | 32,338 | 30,271 | **1.08×** | **1.07×** |
| Octet 2MB (buffer) | 1,809 | 1,891 | 1,477 | **1.23×** | **1.28×** |
| Octet 2MB (stream) | 3,449 | 3,479 | 3,209 | **1.07×** | **1.08×** |
| Multipart 2MB | 1,993 | 2,000 | 1,415 | **1.41×** | **1.41×** |

## Versus FastAPI + Uvicorn

| Endpoint | Stario CLI | FastAPI | Stario / FastAPI |
| --- | ---: | ---: | ---: |
| Plaintext | 137,565 | 45,604 | **3.0×** |
| Request fields | 101,591 | 32,538 | **3.1×** |
| JSON small | 103,106 | 28,629 | **3.6×** |
| Octet 64KB | 32,793 | 13,513 | **2.4×** |
| Octet 2MB (buffer) | 1,809 | 1,371 | **1.3×** |
| Octet 2MB (stream) | 3,449 | 1,137 | **3.0×** |
| Multipart 2MB | 1,993 | 1,359 | **1.5×** |

## CLI vs native entry

Same protocol. Production is `stario serve`. Treat GET gaps of this size as
entry-path / venv / run-order noise, not two HTTP stacks.

| Endpoint | CLI | Native | CLI / Native |
| --- | ---: | ---: | ---: |
| Plaintext | 137,565 | 123,050 | 1.12× |
| Request fields | 101,591 | 95,564 | 1.06× |
| JSON small | 103,106 | 98,141 | 1.05× |
| Octet 64KB | 32,793 | 32,338 | 1.01× |
| Octet 2MB (buffer) | 1,809 | 1,891 | 0.96× |
| Octet 2MB (stream) | 3,449 | 3,479 | 0.99× |
| Multipart 2MB | 1,993 | 2,000 | 1.00× |

## Notes

- Robyn and Django-Bolt buffer the stream route (no request streaming API).
- BlackSheep+Granian vs BlackSheep+Uvicorn shows the server: Granian is ~1.9×
  Uvicorn on static plaintext (113k vs 60k) with the same app.
- High plaintext stdev on Stario is the same 4-vCPU contention pattern as
  earlier captures on this machine class (±5–7k).
- HTTP/2 is implemented (nghttp2, ALPN) and covered by `tests/cython/test_h2.py`.
  It is **not** in this wrk suite. Same-host `h2load` (2026-08-31) was ~1.8×
  HTTP/1 on GET at 100 streams/connection; see
  [`pico-h2-20260831.md`](pico-h2-20260831.md).
