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
| Fair “fill the box” test | One process, `GOMAXPROCS=nCPU` | N processes on one port (`stario-n` / `stario-cython-n` with `STARIO_REUSE_PORT=1`, or `granian-rsgi-n`) |

**Do not** compare default Go (`GOMAXPROCS=4` on this box) to one Stario
worker and call that a language result. That is four cores vs one.

**Do not** start extra Go processes to “match” Python. Go is already
multi-threaded inside one process. The knob is `GOMAXPROCS`, not process
count.

**Do** scale Python to N processes when you want the deploy-shaped
comparison. Set `STARIO_REUSE_PORT=1` and start several `stario serve`
processes on the same host/port (`stario-n` / `stario-cython-n` in the
runner). Granian’s `--workers` is the same idea.

```bash
# same 1-core budget as baseline-20260824.md
benchmarks/server/run.sh stario stario-cython go-nethttp go-fasthttp granian-rsgi

# fill a 4-vCPU box — Stario starts N processes on one SO_REUSEPORT socket
BENCH_PROCS=4 benchmarks/server/run.sh stario-n stario-cython-n go-nethttp-n granian-rsgi-n
```

## This run

| | |
|---|---|
| Host | Intel Xeon (KVM), 4 vCPUs, 15 GiB — same class as `baseline-20260824.md` |
| Client | wrk 4.1.0, `-t2 -c128` (read/small upload), `-c32` (64KB/2MB) |
| Samples | 10s × 3 measured + 1 warmup, IQR trim |
| Topology | wrk + server on `127.0.0.1` |
| Commit | `df931cb` (`cursor/go-benchmark-apps-f638`) |
| Raw | `20260825T175710Z` (1-worker), `20260825T185107Z` (`*-n`, gitignored) |

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

The long-suite Cython plaintext median (97k) is **not** a different case
and is **not** a real drop from the published ~120–125k. Same app, same
`GET /plaintext`, same `wrk -t2 -c128`, one worker. That 97k window was
three noisy 10s samples (93k / 101k / 97k) in a busy suite — params in
the same run swung 74k–116k. An isolated re-run on this box
(`20260825T191938Z`, 10s × 5 + 2 warmup) is **118,189 ± 3,338** (samples
118–125k), matching the committed primary **120,378 ± 24,710** and the
5s refresh **125,436 ± 4,971** (the ~130k figure). Use 118–125k as the
1-worker Cython plaintext anchor; keep the 97k row only as “what that
noisy suite printed.”

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

Go still uses **one process**. Stario and Granian use **4 processes** sharing
one port (`STARIO_REUSE_PORT=1` / Granian `--workers`). Run
`20260825T185107Z` for the `-n` rows; 1-worker rows are from the earlier
same-machine suite.

| Server | Plaintext | JSON | Params | Validate |
| --- | ---: | ---: | ---: | ---: |
| Stario Cython (4 processes, `SO_REUSEPORT`) | **293,998 ± 7,025** | **283,780 ± 7,127** | **272,658 ± 5,467** | **252,150 ± 8,106** |
| Granian RSGI (4 workers) | 223,691 ± 1,873 | 212,601 ± 27,200 | 208,766 ± 1,719 | 144,139 ± 2,036 |
| Stario Python (4 processes, `SO_REUSEPORT`) | 211,540 ± 1,382 | 207,714 ± 803 | 211,574 ± 2,007 | 170,263 ± 4,424 |
| Go net/http (`GOMAXPROCS=4`) | 199,980 ± 1,407 | 195,180 ± 315 | 189,863 ± 529 | 145,315 ± 143 |
| Go fasthttp (`GOMAXPROCS=1`) | 177,185 ± 219 | 170,949 ± 5,180 | 154,793 ± 5,753 | 123,353 ± 2,135 |
| Granian RSGI (1 worker) | 147,367 ± 2,543 | 135,630 ± 6,862 | 145,077 ± 3,100 | 107,832 ± 1,288 |
| Stario Cython (1 worker) | 96,506 ± 3,149 | 104,528 ± 8,428 | 101,712 ± 21,262 | 81,124 ± 3,552 |
| Stario Python (1 worker) | 71,343 ± 294 | 69,287 ± 433 | 63,607 ± 1,911 | 53,098 ± 1,202 |

Scaling (plaintext, 1 unit → 4 units):

| | 1 → 4 | Notes |
| --- | ---: | --- |
| Stario Cython | 97k suite / 118k isolated → 294k (**~2.5–3.0×**) | 4 processes, `STARIO_REUSE_PORT=1` |
| Stario Python | 71k → 212k (**2.97×**) | same |
| Go net/http | 86k → 200k (**2.33×**) | one process, `GOMAXPROCS` 1→4 |
| Granian | 147k → 224k (**1.52×**) | 1→4 workers |

Uploads on the 4-unit rows:

| Server | Form | JSON 1KB | 64KB | 2MB buf | 2MB stream | Multipart |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Stario Cython ×4 | **287,489** | **255,842** | **92,789** | **5,433** | 6,232 | **6,382** |
| Stario Python ×4 | 182,492 | 172,915 | 64,172 | 3,112 | 4,126 | 3,197 |
| Granian ×4 | 149,775 | 119,738 | 69,120 | 3,964 | 4,627 | 3,740 |
| Go net/http P=4 | 195,733 | 143,730 | 12,292 | 1,334 | **7,795** | 1,321 |

`net/http` still lags on buffered 64KB+; it keeps the stream-path lead.
Shared-socket Stario Cython is the fastest fill-the-box stack on this box.

If you only compared `go-nethttp-n` (200k) to 1-worker Cython (97k) you would
report “Go is 2×.” That 2× is mostly the extra cores. Four Cython processes
on one `SO_REUSEPORT` socket are **1.47×** 4-core `net/http` on plaintext.

## Recommendation

- Keep the default suite at **1 worker / `GOMAXPROCS=1`** so new Go rows stay
  comparable to `baseline-20260824.md`.
- Use `go-nethttp-n` + `stario-n` / `stario-cython-n` + `granian-rsgi-n` when
  you want hardware-saturation numbers. Stario `-n` starts N processes on one
  port with `STARIO_REUSE_PORT=1` (`SO_REUSEPORT`).
- Prefer **fasthttp** as the “how fast is a Go HTTP stack” target. Prefer
  **net/http** as the “idiomatic Go stdlib” target. They are different
  questions; both apps implement the same routes.
