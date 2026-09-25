# Stario `STARIO_THREADS` 1 vs 2 vs 3 — 3.14t (2026-09-25)

Same checkout as the Granian / BlackSheep / FastAPI capture
([`baseline-20260925-frameworks.md`](baseline-20260925-frameworks.md)),
same wrk knobs as [`baseline-threads-reuseport-20260921.md`](baseline-threads-reuseport-20260921.md).
This pass fills in **N=3** (the 2026-09-21 tables were 1 / 2 / 4) after
the request-handle, RFC path, drain, and hot-path work.

`STARIO_THREADS=N` is N asyncio/uvloop loops in one 3.14t process. Thread
0 bootstraps and listens; threads `1..N-1` are the same kind of server.
`N>1` binds with `SO_REUSEPORT`. `PYTHON_GIL=0` keeps the GIL off
(python-brotli is not marked free-threaded; bench compression is
disabled and the native protocol talks to libbrotli directly).

Relative signal on one 4-vCPU host. wrk (`-t2`) and the server share the
box, so three worker loops plus wrk is five runnable threads on four
cores.

## Reproduce

```bash
# 3.14t: python-brotli is not marked free-threaded and would turn the GIL
# back on. PYTHON_GIL=0 keeps it off. Bench compression is disabled.
uv python install cpython-3.14.7+freethreaded

for n in 1 2 3; do
  PYTHON_GIL=0 STARIO_THREADS=$n \
    PYTHON=3.14t DURATION=10s RUNS=5 WARMUP=1 \
    THREADS=2 CONNECTIONS=128 UPLOAD_CONNECTIONS=32 KEEP_RAW=1 \
    RESULT_NAME=threads-314t-20260925/stario-$n \
    benchmarks/server/run.sh stario
done
```

Interpreter: CPython **3.14.7 free-threading** (`python3.14t`),
`STARIO_LOOP=uvloop`. Smoke-checked `STARIO_THREADS=3` before the suite:
`stario-worker-1` and `stario-worker-2` plus the main loop, GIL off,
`/plaintext` 200.

Raw wrk under `benchmarks/server/results/threads-314t-20260925/`
(gitignored). Server commit: `140a796` (branch commit `2ebf3cc` is this
docs-only follow-up). Order: N=1 → N=2 → N=3, 18:07–18:28 UTC.

## Environment

| | |
|---|---|
| CPU | 4 vCPUs, Intel Xeon, 1 thread/core |
| RAM | 16 GiB |
| OS | Linux 6.12.94+ |
| Python | 3.14.7 free-threading (`PYTHON_GIL=0`) |
| Client | wrk 4.1.0, `-t2 -c128` (GET / small JSON), `-c32` (64KB / 2MB) |
| Topology | wrk and server on the same host, `127.0.0.1` |
| Stario | 4.3.0 (this checkout, Cython protocol, `stario serve`, uvloop, compression off) |
| Listen | `N=1` single `create_server`; `N>1` `SO_REUSEPORT` |

## Methodology

- One process. `STARIO_THREADS=N` is N asyncio/uvloop loops, one per OS thread.
- Endpoints: `plaintext`, `request`, `json-small`, `post-octet-64k`,
  `post-octet-2m`, `post-stream-2m`, `multipart-2m`.
- **5 measured + 1 warmup**, `DURATION=10s`, IQR trimming via
  `benchmarks/server/stats.py`.
- No Non-2xx or socket timeouts in any wrk run.

## Median req/s

Bold is the highest cell in that row. Ratio is vs `STARIO_THREADS=1`.

### GET

| Endpoint | 1 thread | 2 threads | 3 threads | 2 / 1 | 3 / 1 | 3 / 2 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Plaintext | 138,842 ± 5,682 | 240,962 ± 4,918 | **298,346 ± 6,478** | 1.74× | 2.15× | 1.24× |
| Request fields | 116,506 ± 4,696 | 207,728 ± 9,630 | **240,543 ± 6,092** | 1.78× | 2.06× | 1.16× |

### Upload / async

| Endpoint | 1 thread | 2 threads | 3 threads | 2 / 1 | 3 / 1 | 3 / 2 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| JSON small | 89,130 ± 2,541 | 167,065 ± 3,248 | **222,740 ± 2,833** | 1.87× | 2.50× | 1.33× |
| Octet 64KB | 32,726 ± 279 | 72,519 ± 2,876 | **99,409 ± 2,683** | 2.22× | 3.04× | 1.37× |
| Octet 2MB (buffer) | 1,747 ± 59 | 3,495 ± 40 | **5,228 ± 53** | 2.00× | 2.99× | 1.50× |
| Octet 2MB (stream) | 1,910 ± 85 | 3,745 ± 119 | **5,057 ± 142** | 1.96× | 2.65× | 1.35× |
| Multipart 2MB | 1,728 ± 63 | 3,526 ± 107 | **5,098 ± 30** | 2.04× | 2.95× | 1.45× |

## Read

Two loops still pack this box: plaintext **1.74×**, request **1.78×**,
JSON **1.87×**, and the large-body cases sit at **2.0–2.2×**. That is
slightly less than the 2026-09-21 reuseport 2 / 1 on plaintext (2.14×)
because N=1 is healthier this time (139k vs a noisy 106k), not because
N=2 got worse (241k vs 226k).

Three loops keep climbing on every endpoint. CPU-bound uploads are
closest to linear (64KB **3.04×**, 2MB buffer **2.99×**, multipart
**2.95×**). GET steps down (plaintext **2.15×**, request **2.06×**) —
three loops + `wrk -t2` is five runnable threads on four cores. The
3 / 2 step is still a real gain (plaintext **1.24×**, JSON **1.33×**,
64KB **1.37×**), not noise: N=3 plaintext samples are 274–309k and N=2
is 239–267k.

Request fields is the headline change vs September. N=2 is **208k**
(was 140k). N=3 is **241k**, which is above the old N=4 cell (170k).
The Match / status-line / interned-method work is on the path that used
to scale worst.

## Versus GIL 3.14, same host, same day

[`baseline-20260925-frameworks.md`](baseline-20260925-frameworks.md)
ran Stario CLI on GIL 3.14 with one thread (146,750 plaintext). N=1
3.14t vs that GIL pass:

| Endpoint | 3.14t N=1 | GIL 3.14 N=1 | 3.14t / GIL |
| --- | ---: | ---: | ---: |
| Plaintext | 138,842 | 146,750 | 0.95× |
| Request fields | 116,506 | 118,743 | 0.98× |
| JSON small | 89,130 | 94,848 | 0.94× |
| Octet 64KB | 32,726 | 33,194 | 0.99× |
| Octet 2MB (buffer) | 1,747 | 1,895 | 0.92× |
| Octet 2MB (stream) | 1,910 | 2,279 | 0.84× |
| Multipart 2MB | 1,728 | 1,890 | 0.91× |

The free-thread single-thread tax on this tree is about **5%** on
plaintext and **2%** on request fields — inside CPython's own ~8%
pyperformance figure, not the 15–20% hole in the September N=1 cells.
Two 3.14t threads already beat GIL one-thread on every endpoint
(plaintext **1.64×** GIL, request **1.75×** GIL).

## Versus 2026-09-21 reuseport (1 / 2 / 4)

Same machine class and knobs. September had no N=3 column; N=4 is the
nearest extra-thread point (and oversubscribed this box harder).

| Endpoint | 2026-09-21 N=1 / 2 / 4 | 2026-09-25 N=1 / 2 / 3 |
| --- | ---: | ---: |
| Plaintext | 105,740 / 226,181 / 262,142 | 138,842 / 240,962 / **298,346** |
| Request fields | 94,445 / 140,104 / 169,693 | 116,506 / 207,728 / **240,543** |
| JSON small | 71,149 / 163,203 / 208,738 | 89,130 / 167,065 / **222,740** |
| Octet 64KB | 34,302 / 84,528 / 103,752 | 32,726 / 72,519 / 99,409 |
| Octet 2MB (stream) | 3,319 / **6,490** / 5,858 | 1,910 / 3,745 / 5,057 |

GET and small JSON are up at every N. Three threads on this checkout
beat September's four-thread GET (298k vs 262k plaintext, 241k vs 170k
request). Large streaming uploads are the leftover: N=1 stream is still
the 1.9k band from today's GIL pass, and N=2/3 do not recover the
September 6.5k 2-thread stream cell. Treat that as a stream-path
regression to hunt, not as a threading failure — the 3 / 1 stream ratio
is still **2.65×**.

## Takeaway

On 3.14t, two `SO_REUSEPORT` loops remain the clean packing of this
4-vCPU box (~1.7–2.2×). A third loop still pays (especially 64KB /
buffered 2MB / multipart, ~3×) and does not invert any endpoint. Request
fields finally scale with the CPU cases. The 2MB stream path is the
one number that did not come along from September.

GIL process workers and Granian `--workers` 1/2/3 were not re-run here.
The same-day one-thread framework comparison is
[`baseline-20260925-frameworks.md`](baseline-20260925-frameworks.md).
