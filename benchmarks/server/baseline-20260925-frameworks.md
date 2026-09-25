# Server benchmarks — 2026-09-25 (Stario vs Granian / BlackSheep / FastAPI)

Head-to-head on the official `benchmarks/server` suite after the
request-handle, RFC path, drain, and hot-path work on
`cursor/drain-and-hotpath-e9c3` (`140a796`). Same knobs as
[`baseline-20260921-core.md`](baseline-20260921-core.md).

Relative signal on one machine — not lab-grade absolutes. **Do not
compare req/s** to `baseline-20260827.md` / `baseline-20260828.md`
(different host). Same-host class as the 2026-09-21 capture.

## Reproduce

```bash
DURATION=10s RUNS=5 WARMUP=1 THREADS=2 \
  CONNECTIONS=128 UPLOAD_CONNECTIONS=32 \
  PYTHON=3.14 WRK=wrk KEEP_RAW=1 \
  RESULT_NAME=frameworks-20260925 \
  benchmarks/server/run.sh \
    stario granian-rsgi blacksheep-granian blacksheep-uvicorn fastapi
```

Run directory: `benchmarks/server/results/frameworks-20260925` (gitignored).
Order: Stario → Granian RSGI → BlackSheep+Granian → BlackSheep+Uvicorn →
FastAPI. One pass, 17:15–17:50 UTC.

## Environment

| | |
|---|---|
| CPU | 4 vCPUs, Intel Xeon, x86_64 |
| RAM | 16 GiB |
| OS | Linux 6.12.94+ |
| Python | 3.14.7 |
| Client | wrk 4.1.0, `-t2 -c128` (GET / small JSON), `-c32` (64KB / 2MB) |
| Topology | wrk and servers on the same host, `127.0.0.1` |
| Commit | `140a796` (`cursor/drain-and-hotpath-e9c3`) |
| Stario | 4.3.0 (this checkout, Cython protocol, `stario serve`, uvloop, compression off) |
| Packages | Granian 2.8.3, BlackSheep 2.6.3, FastAPI 0.141.1, Starlette 1.7.0, Uvicorn 0.54.0, uvloop 0.22.1, ujson 6.0.0 |

## Methodology

- One worker/process per target. Granian `--workers 1 --runtime-threads 1`.
- Endpoints: `plaintext`, `request`, `json-small`, `post-octet-64k`,
  `post-octet-2m`, `post-stream-2m`, `multipart-2m`.
- **5 measured + 1 warmup**, `DURATION=10s`, IQR trimming via
  `benchmarks/server/stats.py`.
- Static `/plaintext` returns a prebuilt body. `request` interpolates path
  param `user_id`, query `q`, and header `x-request-id` into a plain line
  (wrk working set **4096**). Every upload handler reads the body then
  `await asyncio.sleep(0)`.
- Responses are **plain text** so JSON serializers cannot win a case.
- `wrk` HTTP/1 only. All raw outputs were scanned: no `Non-2xx`, no
  socket errors.

## Ranking (median req/s)

Bold is the highest cell in that column.

### GET

Sorted by plaintext.

| Server | Plaintext | Request fields |
| --- | ---: | ---: |
| Granian RSGI (Rust, no framework) | **150,043 ± 5,853** | **147,906 ± 1,545** |
| Stario (Cython, CLI) | 146,750 ± 7,874 | 118,743 ± 3,297 |
| BlackSheep + Granian ASGI | 110,865 ± 1,300 | 61,978 ± 768 |
| BlackSheep + Uvicorn ASGI | 60,550 ± 1,209 | 45,466 ± 599 |
| FastAPI + Uvicorn ASGI | 43,770 ± 342 | 32,915 ± 536 |

Plaintext is a ~150k band. Stario sits at **0.98×** Granian — the leftover
is Rust RSGI vs Cython llhttp + `create_task` + `respond()`, not routing.
`/plaintext` never reads query or headers.

Request fields drop everyone except Granian. Granian has no router
(`path.startswith` + `rsplit` + raw query string) and stays in the
plaintext band (148k). Stario pays `_lookup`, `c.match.params`, lazy
`query.get`, and a header read — **0.80×** Granian, still the fastest
frameworked server (**1.9×** BlackSheep+Granian, **3.6×** FastAPI).

### Upload / async

Sorted by JSON small.

| Server | JSON small | 64KB | 2MB buffer | 2MB stream | Multipart 2MB |
| --- | ---: | ---: | ---: | ---: | ---: |
| Granian RSGI | **107,281 ± 4,467** | 30,294 ± 289 | 1,442 ± 39 | 3,099 ± 4 | 1,454 ± 50 |
| Stario (Cython, CLI) | 94,848 ± 476 | **33,194 ± 328** | **1,895 ± 47** | 2,279 ± 28 | **1,890 ± 28** |
| BlackSheep + Uvicorn | 45,924 ± 771 | **15,183 ± 409** | 775 ± 8 | 1,888 ± 17 | 746 ± 13 |
| BlackSheep + Granian | 39,175 ± 190 | 14,915 ± 83 | 988 ± 10 | **3,154 ± 35** | 941 ± 4 |
| FastAPI + Uvicorn | 28,095 ± 259 | 13,421 ± 41 | 1,282 ± 12 | 954 ± 21 | 1,334 ± 30 |

Small JSON POST: Granian 107k, Stario **0.88×**, then a gap to the ASGI
stacks. BlackSheep+Uvicorn beats BlackSheep+Granian on this case (same
pattern as 2026-09-21).

Buffered large bodies: Stario leads 64KB / 2MB buffer / multipart.
The 2MB **stream** case goes to Granian / BlackSheep+Granian (~3.1k);
Stario is **0.74×** Granian there.

## Versus Granian RSGI (native, no framework)

| Endpoint | Stario | Granian | Stario / Granian |
| --- | ---: | ---: | ---: |
| Plaintext | 146,750 | 150,043 | 0.98× |
| Request fields | 118,743 | 147,906 | 0.80× |
| JSON small | 94,848 | 107,281 | 0.88× |
| Octet 64KB | 33,194 | 30,294 | **1.10×** |
| Octet 2MB (buffer) | 1,895 | 1,442 | **1.31×** |
| Octet 2MB (stream) | 2,279 | 3,099 | 0.74× |
| Multipart 2MB | 1,890 | 1,454 | **1.30×** |

## Versus BlackSheep

Same app for both BlackSheep rows; the server is the variable.

| Endpoint | Stario | BS+Granian | BS+Uvicorn | vs Granian ASGI | vs Uvicorn ASGI |
| --- | ---: | ---: | ---: | ---: | ---: |
| Plaintext | 146,750 | 110,865 | 60,550 | **1.32×** | **2.42×** |
| Request fields | 118,743 | 61,978 | 45,466 | **1.92×** | **2.61×** |
| JSON small | 94,848 | 39,175 | 45,924 | **2.42×** | **2.07×** |
| Octet 64KB | 33,194 | 14,915 | 15,183 | **2.23×** | **2.19×** |
| Octet 2MB (buffer) | 1,895 | 988 | 775 | **1.92×** | **2.45×** |
| Octet 2MB (stream) | 2,279 | 3,154 | 1,888 | 0.72× | **1.21×** |
| Multipart 2MB | 1,890 | 941 | 746 | **2.01×** | **2.53×** |

BlackSheep+Granian vs BlackSheep+Uvicorn on static plaintext is **1.83×**
(111k vs 61k) — Granian the server, same framework.

## Versus FastAPI + Uvicorn

| Endpoint | Stario | FastAPI | Stario / FastAPI |
| --- | ---: | ---: | ---: |
| Plaintext | 146,750 | 43,770 | **3.35×** |
| Request fields | 118,743 | 32,915 | **3.61×** |
| JSON small | 94,848 | 28,095 | **3.38×** |
| Octet 64KB | 33,194 | 13,421 | **2.47×** |
| Octet 2MB (buffer) | 1,895 | 1,282 | **1.48×** |
| Octet 2MB (stream) | 2,279 | 954 | **2.39×** |
| Multipart 2MB | 1,890 | 1,334 | **1.42×** |

## Versus 2026-09-21 same-host capture

[`baseline-20260921-core.md`](baseline-20260921-core.md) used the same
machine class, knobs, and apps. Stario CLI then vs now:

| Endpoint | 2026-09-21 | 2026-09-25 | Ratio |
| --- | ---: | ---: | ---: |
| Plaintext | 137,565 | 146,750 | **1.07×** |
| Request fields | 101,591 | 118,743 | **1.17×** |
| JSON small | 103,106 | 94,848 | 0.92× |
| Octet 64KB | 32,793 | 33,194 | 1.01× |
| Octet 2MB (buffer) | 1,809 | 1,895 | 1.05× |
| Octet 2MB (stream) | 3,449 | 2,279 | 0.66× |
| Multipart 2MB | 1,993 | 1,890 | 0.95× |

Granian GET held still (plaintext 149k → 150k, request 150k → 148k), so
the request-fields gain is Stario, not a quieter host. The 2MB stream
drop is also Stario-side: Granian stream stayed ~3.1k (3,209 then, 3,099
now). Raw Stario stream samples this run were tightly clustered
(2.08–2.42k), not a single outlier.

## Notes

- High plaintext stdev on Stario and Granian is the usual 4-vCPU
  contention pattern on this machine class (±6–8k).
- HTTP/2 is implemented (nghttp2, ALPN) and is **not** in this wrk suite.
- Socketify, Robyn, Sanic, Django-Bolt, and Falcon were not re-run.
