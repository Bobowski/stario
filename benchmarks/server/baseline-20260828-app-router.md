# Server benchmark: Cython App/Router PoC (2026-08-28)

Relative numbers on one host — not lab-grade absolutes. Sequential suite
`20260828T210831Z` on commit `6616720` (`cursor/cython-app-router-poc-b193`).

Three targets only:

| Target | Stack |
| --- | --- |
| `stario-cython-pyapp` | Cython llhttp + **Python** App/Router (`STARIO_CYTHON_PYTHON_APP=1`) |
| `stario-cython` | Cython llhttp + **Cython** App/Router (this branch) |
| `granian-rsgi` | Granian RSGI, no framework |

Same protocol binary for the two Stario rows. The only variable is App/Router.

## Reproduce

```bash
WRK=wrk PYTHON=3.14 RUNS=5 WARMUP=1 DURATION=10s THREADS=2 \
  CONNECTIONS=128 UPLOAD_CONNECTIONS=32 ENDPOINT_TIER=all \
  benchmarks/server/run.sh stario-cython-pyapp stario-cython granian-rsgi
```

## Environment

| | |
|---|---|
| CPU | Intel Xeon (KVM), x86_64 |
| OS | Linux 6.12.94+ |
| Python | 3.14.7 |
| Client | wrk 4.2.0, `-t2 -c128` (read/small upload), `-c32` (64KB/2MB) |
| Topology | wrk and servers on same host, `127.0.0.1` loopback |
| Commit | `6616720` |
| Run | `20260828T210831Z` |

## Methodology

Official suite knobs: **5 measured + 1 warmup**, `DURATION=10s`, IQR trimming.
One worker. Handlers compute responses per request.

## Cython App vs Python App (same llhttp protocol)

| Endpoint | Python App | Cython App | Cython / Python |
| --- | ---: | ---: | ---: |
| Plaintext | 129,546 ± 1,234 | 136,005 ± 1,885 | **1.05×** |
| JSON | 126,669 ± 757 | 129,740 ± 2,188 | 1.02× |
| Params | 119,373 ± 2,553 | 126,230 ± 836 | **1.06×** |
| Validate JSON | 104,429 ± 1,330 | 111,633 ± 4,893 | **1.07×** |
| Form POST | 127,101 ± 3,516 | 129,556 ± 2,880 | 1.02× |
| JSON 1KB | 117,458 ± 4,112 | 113,879 ± 5,955 | 0.97× |
| Octet 64KB | 38,242 ± 241 | 39,349 ± 2,664 | 1.03× |
| Octet 2MB (buffer) | 3,495 ± 49 | 3,493 ± 56 | 1.00× |
| Octet 2MB (stream) | 3,658 ± 6 | 3,526 ± 74 | 0.96× |
| Multipart 2MB | 3,624 ± 34 | 3,469 ± 66 | 0.96× |

GET `/plaintext` and `/user/{id}` pick up **~5%**. Validate is **~7%**. Large
body paths sit in run-to-run noise (JSON 1KB stdev is 4–6k req/s). Compiling
App/Router is real on the header-dispatch path and does not move 2MB ingest.

## vs Granian RSGI

| Endpoint | Cython App | Granian RSGI | Granian / Cython App |
| --- | ---: | ---: | ---: |
| Plaintext | 136,005 ± 1,885 | **151,270 ± 4,067** | **1.11×** |
| JSON | 129,740 ± 2,188 | **150,927 ± 2,728** | **1.16×** |
| Params | 126,230 ± 836 | **150,602 ± 6,209** | **1.19×** |
| Validate JSON | **111,633 ± 4,893** | 103,908 ± 677 | 0.93× |
| Form POST | **129,556 ± 2,880** | 119,612 ± 966 | 0.92× |
| JSON 1KB | **113,879 ± 5,955** | 108,588 ± 499 | 0.95× |
| Octet 64KB | **39,349 ± 2,664** | 31,869 ± 212 | 0.81× |
| Octet 2MB (buffer) | **3,493 ± 56** | 1,470 ± 8 | 0.42× |
| Octet 2MB (stream) | **3,526 ± 74** | 3,230 ± 53 | 0.92× |
| Multipart 2MB | **3,469 ± 66** | 3,141 ± 98 | 0.91× |

Granian remains **~11–19%** ahead on GET. That gap is not App/Router: Python
App → Cython App closed about a third of a 15k plaintext delta (129.5k →
136.0k vs Granian 151.3k). Remaining GET cost is more likely the App
wrapper coroutine, `responses.text`/`ujson` from Python, and protocol
`create_task` / writer framing.

Stario already leads Granian on validate, form, and all upload/body cases
in this run (same as the 2026-08-28 Cython protocol snapshot).
