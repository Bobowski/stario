# Go vs Stario Cython / Python (2026-08-25)

Same 10-route app as `apps/stario_app.py`, measured with the existing `wrk`
runner. Answers two questions:

1. How do the Go ports compare to the committed one-worker baselines?
2. Should we pin Go to one core, or run Go normally and scale Python?

## How to compare Go to Python

A Go process is not the same unit of work as a CPython process.

| | Go | CPython / Cython / Granian |
| --- | --- | --- |
| Normal deploy | **One process**, goroutines on `GOMAXPROCS` OS threads (default: all CPUs) | **N processes** (one worker ≈ one core for bytecode; native extensions can use more) |
| Fair “code path” test | One process, `GOMAXPROCS=1` | One worker (the existing suite) |
| Fair “fill the box” test | One process, `GOMAXPROCS=nCPU` | N workers (`granian-rsgi-n`, uvicorn `--workers N`, …) |

**Do not** compare default Go (`GOMAXPROCS=4` on this box) to one Stario
worker and call that a language result. That is four cores vs one.

**Do not** start extra Go processes to “match” Python. Go is already
multi-threaded inside one process. The knob is `GOMAXPROCS`, not process
count.

**Do** scale Python to N processes when you want the deploy-shaped
comparison. Stario has no worker pool today; Granian does, which is why
`granian-rsgi-n` exists.

```bash
# same 1-core budget as baseline-20260824.md
benchmarks/server/run.sh stario stario-cython go-nethttp go-fasthttp granian-rsgi

# fill a 4-vCPU box
BENCH_PROCS=4 benchmarks/server/run.sh go-nethttp-n granian-rsgi-n
```

## This run

| | |
|---|---|
| Host | Intel Xeon (KVM), 4 vCPUs, 15 GiB — same class as `baseline-20260824.md` |
| Client | wrk 4.1.0, `-t2 -c128` (read/small upload), `-c32` (64KB/2MB) |
| Samples | 10s × 3 measured + 1 warmup, IQR trim |
| Topology | wrk + server on `127.0.0.1` |
| Commit | `6a1c075` (`cursor/go-benchmark-apps-f638`) |
| Raw | `benchmarks/server/results/20260825T175710Z/` (gitignored) |

Handlers compute JSON and validation **per request** (`encoding/json` in Go,
`ujson` in Python). No cached response bytes.

## 1-core / 1-worker (language comparison)

This is the row that belongs next to the published Cython baseline.

| Server | Plaintext | JSON | Params | Validate |
| --- | ---: | ---: | ---: | ---: |
| Go fasthttp (`GOMAXPROCS=1`) | 177,185 ± 219 | 170,949 ± 5,180 | 154,793 ± 5,753 | 123,353 ± 2,135 |
| Granian RSGI (1 worker) | 147,367 ± 2,543 | 135,630 ± 6,862 | 145,077 ± 3,100 | 107,832 ± 1,288 |
| Stario Cython (this run) | 96,506 ± 3,149 | 104,528 ± 8,428 | 101,712 ± 21,262 | 81,124 ± 3,552 |
| Stario Cython (committed baseline, 10s × 5) | 120,378 ± 24,710 | 117,556 ± 10,384 | 104,519 ± 7,598 | 82,402 ± 1,389 |
| Go net/http (`GOMAXPROCS=1`) | 85,768 ± 8,554 | 85,219 ± 3,482 | 81,707 ± 2,271 | 65,837 ± 7,883 |
| Stario Python httptools | 71,343 ± 294 | 69,287 ± 433 | 63,607 ± 1,911 | 53,098 ± 1,202 |

vs **this-run Cython** (same machine, same wrk settings):

| | net/http P=1 | fasthttp P=1 | Granian 1w |
| --- | ---: | ---: | ---: |
| Plaintext | 0.89× | **1.84×** | 1.53× |
| JSON | 0.82× | **1.64×** | 1.30× |
| Params | 0.80× | **1.52×** | 1.43× |
| Validate | 0.81× | **1.52×** | 1.33× |

stdlib Go sits in the Stario Cython band (a bit under on this run). fasthttp
on **one core** is the fastest 1-worker row — above 1-worker Granian and
well above Cython. Stario Python remains ~1.2× behind 1-core `net/http` and
~2.5× behind 1-core fasthttp on plaintext.

This-run Cython plaintext (97k) is below the committed primary (120k ± 25k)
and closer to the confirmatory refresh (125k). Treat ratios against
**this-run** Cython; the committed table is the historical anchor, not a
second clock.

## Large-body (1-core)

| Server | Form | JSON 1KB | 64KB | 2MB buf | 2MB stream | Multipart |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Go fasthttp P=1 | 162,327 | 145,474 | **42,652** | **3,736** | **6,094** | **3,694** |
| Granian 1w | 117,754 | 112,913 | 30,194 | 1,180 | 2,868 | 3,139 |
| Stario Cython | 94,432 | 83,782 | 31,403 | 2,566 | 2,467 | 2,260 |
| Go net/http P=1 | 97,476 | 80,365 | 14,945 | 536 | 3,928 | 542 |
| Stario Python | 55,311 | 54,114 | 28,303 | 2,023 | 2,898 | 1,980 |

`net/http` `io.ReadAll` (the honest analog of `await req.body()`) is weak on
64KB+ buffered uploads. The streaming path is fine — faster than Cython.
fasthttp is ahead on every body size.

## Fill the box (4 vCPUs)

Go still uses **one process**. Granian uses **4 worker processes**.

| Server | Plaintext | JSON | Params | Validate |
| --- | ---: | ---: | ---: | ---: |
| Granian RSGI (4 workers) | **221,079 ± 2,600** | **214,319 ± 2,980** | **208,552 ± 4,399** | 153,364 ± 6,225 |
| Go net/http (`GOMAXPROCS=4`) | 198,547 ± 458 | 187,731 ± 2,253 | 187,846 ± 5,123 | 144,093 ± 174 |
| Go fasthttp (`GOMAXPROCS=1`) | 177,185 ± 219 | 170,949 ± 5,180 | 154,793 ± 5,753 | 123,353 ± 2,135 |
| Granian RSGI (1 worker) | 147,367 ± 2,543 | 135,630 ± 6,862 | 145,077 ± 3,100 | 107,832 ± 1,288 |
| Stario Cython (1 worker) | 96,506 ± 3,149 | 104,528 ± 8,428 | 101,712 ± 21,262 | 81,124 ± 3,552 |

Scaling:

| | 1 unit → 4 units | Notes |
| --- | ---: | --- |
| Go net/http plaintext | 86k → 199k (**2.31×**) | one process, `GOMAXPROCS` 1→4 |
| Granian plaintext | 147k → 221k (**1.50×**) | 1→4 processes |
| fasthttp | 177k on **1** core | already near 4-worker Granian |

On uploads, 4-worker Granian wins 64KB (67k vs fasthttp 43k / nethttp-n 12k).
4-core `net/http` still trails on buffered 2MB (1.3k vs Granian-n 3.5k /
fasthttp 3.7k) and leads on the stream path (7.9k).

If you only compared `go-nethttp-n` (199k) to 1-worker Cython (97k) you would
report “Go is 2×.” That 2× is mostly the extra cores. The 1-core table is
the language comparison; this table is the deploy comparison.

## Recommendation

- Keep the default suite at **1 worker / `GOMAXPROCS=1`** so new Go rows stay
  comparable to `baseline-20260824.md`.
- Use `go-nethttp-n` + `stario-n` / `stario-cython-n` + `granian-rsgi-n` when
  you want hardware-saturation numbers. Stario `-n` starts N processes on one
  port with `STARIO_REUSE_PORT=1` (`SO_REUSEPORT`).
- Prefer **fasthttp** as the “how fast is a Go HTTP stack” target. Prefer
  **net/http** as the “idiomatic Go stdlib” target. They are different
  questions; both apps implement the same routes.
